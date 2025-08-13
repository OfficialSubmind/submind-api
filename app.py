
import os, time, asyncio, math
from typing import Dict, Any, List
import httpx
import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

VERSION = "3.1.0-oneclick-max"
INTERVAL = int(os.getenv("WORKER_INTERVAL","20"))
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY","")

state: Dict[str, Any] = {
    "started_at": time.time(),
    "version": VERSION,
    "scores": [],       # latest rows
    "narratives": [],   # latest rows
    "incidents": [],
    "clients": set(),
}

app = FastAPI(title="SubMind OneClick (Max)", version=VERSION)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

DB_PATH = "data/submind.db"

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS scores(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, score REAL, velocity REAL, trust REAL, t REAL
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS narratives(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, source TEXT, t REAL
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS incidents(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, message TEXT, t REAL
        )""")
        await db.commit()

async def fetch_json(url: str, timeout=10, headers: dict=None):
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers or {"User-Agent":"SubMind/1.0"})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        await log_incident("fetch_error", f"{url} -> {e}")
        return None

def calc_stats(series: List[float]):
    if not series:
        return 0.0, 0.0, 0.0
    score = sum(series)/len(series)
    velocity = (series[-1]-series[0])/max(len(series)-1,1)
    mean = score
    var = sum((x-mean)**2 for x in series)/len(series)
    stdev = var**0.5
    trust = 1.0/(1.0+stdev)
    return round(score,3), round(velocity,3), round(trust,3)

async def log_incident(kind, message):
    t = time.time()
    state["incidents"].append({"kind":kind,"message":message,"t":t})
    state["incidents"] = state["incidents"][-100:]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO incidents(kind,message,t) VALUES(?,?,?)",(kind,message,t))
        await db.commit()

async def push_scores(rows):
    state["scores"] = rows
    await broadcast({"type":"scores", "scores": rows})

async def push_narratives(rows):
    state["narratives"] = rows
    await broadcast({"type":"narratives", "narratives": rows})

async def broadcast(payload):
    dead = []
    for q in list(state["clients"]):
        try:
            await q.put(payload)
        except Exception:
            dead.append(q)
    for q in dead:
        state["clients"].discard(q)

@app.on_event("startup")
async def on_start():
    await db_init()
    asyncio.create_task(worker())

async def worker():
    prices = {"bitcoin": [], "ethereum": []}
    hn_seen = set()
    reddit_seen = set()
    while True:
        try:
            now = time.time()
            # CoinGecko
            cg = await fetch_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd")
            if isinstance(cg, dict):
                for coin in ["bitcoin","ethereum"]:
                    v = float(cg.get(coin,{}).get("usd",0.0))
                    prices.setdefault(coin,[]).append(v)
                    prices[coin] = prices[coin][-120:]

            # Wikipedia edits
            wiki = await fetch_json("https://wikimedia.org/api/rest_v1/metrics/edit/aggregate/all-projects/all-editor-types/all-page-types/daily/20240101/20240131")
            wiki_vals = []
            if isinstance(wiki, dict):
                for it in wiki.get("items", [])[-10:]:
                    wiki_vals.append(it.get("results",{}).get("edits",0))

            # HackerNews
            hn = await fetch_json("https://hn.algolia.com/api/v1/search?tags=front_page")
            narratives = []
            if isinstance(hn, dict):
                for hit in hn.get("hits", [])[:15]:
                    nid = hit.get("objectID")
                    title = hit.get("title") or hit.get("story_title") or ""
                    if nid and title and nid not in hn_seen:
                        narratives.append({"title": title, "source":"HackerNews", "t": now})
                        hn_seen.add(nid)

            # Reddit r/technology (no key)
            red = await fetch_json("https://www.reddit.com/r/technology/top.json?t=day&limit=10", headers={"User-Agent":"SubMind-OneClick/1.0"})
            if isinstance(red, dict):
                for child in red.get("data",{}).get("children",[]):
                    data = child.get("data",{})
                    rid = data.get("id")
                    title = data.get("title","")
                    if rid and title and rid not in reddit_seen:
                        narratives.append({"title": title, "source":"Reddit/technology", "t": now})
                        reddit_seen.add(rid)

            # Optional: NewsAPI (if key provided)
            if NEWSAPI_KEY:
                url = f"https://newsapi.org/v2/top-headlines?language=en&pageSize=10&apiKey={NEWSAPI_KEY}"
                news = await fetch_json(url)
                if isinstance(news, dict):
                    for art in news.get("articles",[]):
                        title = art.get("title","")
                        if title:
                            narratives.append({"title": title, "source":"NewsAPI", "t": now})

            # Compute scores
            rows = []
            for name, series in prices.items():
                s, v, t = calc_stats(series)
                rows.append({"name": name, "score": s, "velocity": v, "trust": t, "ts": now})
            if wiki_vals:
                s, v, t = calc_stats([float(x) for x in wiki_vals])
                rows.append({"name":"wikipedia_edits","score":s,"velocity":v,"trust":t,"ts":now})

            # Persist
            async with aiosqlite.connect(DB_PATH) as db:
                for r in rows:
                    await db.execute("INSERT INTO scores(name,score,velocity,trust,t) VALUES(?,?,?,?,?)",
                                     (r["name"], r["score"], r["velocity"], r["trust"], r["ts"]))
                for n in narratives:
                    await db.execute("INSERT INTO narratives(title,source,t) VALUES(?,?,?)",
                                     (n["title"], n["source"], n["t"]))
                await db.commit()

            # Keep latest view
            await push_scores(sorted(rows, key=lambda x: x["score"], reverse=True))
            if narratives:
                # keep last 50
                state["narratives"] = (state["narratives"] + narratives)[-50:]
                await push_narratives(state["narratives"])

        except Exception as e:
            await log_incident("loop_error", str(e))

        await asyncio.sleep(INTERVAL)

# Routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "version": VERSION})

@app.get("/health")
async def health():
    return {"status":"ok","version":VERSION,"uptime_sec": int(time.time()-state["started_at"])}

@app.get("/api/scores")
async def api_scores():
    return {"data": state["scores"]}

@app.get("/api/narratives")
async def api_narratives():
    return {"data": state["narratives"][-50:]}

@app.get("/api/incidents")
async def api_incidents():
    return {"data": state["incidents"][-50:]}

@app.get("/stream")
async def stream():
    async def gen(q):
        try:
            while True:
                payload = await q.get()
                yield {"event":"update","data":payload}
        except asyncio.CancelledError:
            pass
    q: asyncio.Queue = asyncio.Queue()
    state["clients"].add(q)
    return EventSourceResponse(gen(q))
