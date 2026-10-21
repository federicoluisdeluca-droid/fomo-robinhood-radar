#!/usr/bin/env python3
"""server_fomo_radar.py - Radar FOMO Solana sin Grok.

MVP corregido:
- Descubrimiento y ranking antes de limitar candidatos.
- Selección correcta del token aunque aparezca como baseToken o quoteToken.
- Score normalizado cuando no hay proveedor social.
- Holders mediante Helius DAS getTokenAccounts (opcional).
- X mediante Apify con deduplicación y validación flexible de esquema (opcional).
- Narrativa heurística y síntesis opcional con OpenAI.
- Ciclo ENTRY -> TAKE_CAPITAL -> COOLING -> EXITED -> REARMED.
- SQLite, health endpoint para Render y avisos de persistencia.

Dependencia: pip install requests
"""

import hashlib
import html
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

# ------------------------------- CONFIG -------------------------------
DB_PATH = os.getenv("DB_PATH", "radar.db")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))
RUN_ONCE = os.getenv("RUN_ONCE", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
PORT = int(os.getenv("PORT", "0"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "apidojo~tweet-scraper")
APIFY_MAX_POSTS = int(os.getenv("APIFY_MAX_POSTS", "100"))
SOCIAL_REFRESH_MINUTES = int(os.getenv("SOCIAL_REFRESH_MINUTES", "15"))

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_RPC_URL = os.getenv(
    "HELIUS_RPC_URL",
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else "",
)
HOLDERS_REFRESH_MINUTES = int(os.getenv("HOLDERS_REFRESH_MINUTES", "15"))
MAX_HOLDER_PAGES = int(os.getenv("MAX_HOLDER_PAGES", "5"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

WATCHLIST_CAS = [x.strip() for x in os.getenv("WATCHLIST_CAS", "").split(",") if x.strip()]
ENABLE_DISCOVERY = os.getenv("ENABLE_DISCOVERY", "true").lower() == "true"
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "25"))
DISCOVERY_POOL_LIMIT = int(os.getenv("DISCOVERY_POOL_LIMIT", "150"))

MIN_MC = float(os.getenv("MIN_MC", "150000"))
MAX_MC = float(os.getenv("MAX_MC", "10000000"))
MIN_LIQ = float(os.getenv("MIN_LIQ", "25000"))
MIN_LIQ_MC_RATIO = float(os.getenv("MIN_LIQ_MC_RATIO", "0.07"))
MIN_VOL_1H = float(os.getenv("MIN_VOL_1H", "100000"))
MIN_BUYS_5M = int(os.getenv("MIN_BUYS_5M", "25"))
MIN_BUY_SELL_RATIO = float(os.getenv("MIN_BUY_SELL_RATIO", "1.30"))
MIN_PRICE_1H = float(os.getenv("MIN_PRICE_1H", "25"))

ENTRY_SCORE = float(os.getenv("ENTRY_SCORE", "62"))
TAKE_CAPITAL_MULTIPLE = float(os.getenv("TAKE_CAPITAL_MULTIPLE", "2.0"))
COOLING_SCORE_RATIO = float(os.getenv("COOLING_SCORE_RATIO", "0.68"))
COOLING_SOCIAL_RATIO = float(os.getenv("COOLING_SOCIAL_RATIO", "0.45"))
COOLING_HOLDER_RATIO = float(os.getenv("COOLING_HOLDER_RATIO", "0.35"))
COOLING_MC_RATIO = float(os.getenv("COOLING_MC_RATIO", "0.72"))
EXIT_SCORE = float(os.getenv("EXIT_SCORE", "35"))
EXIT_MC_PEAK_RATIO = float(os.getenv("EXIT_MC_PEAK_RATIO", "0.50"))
REARM_SCORE = float(os.getenv("REARM_SCORE", "68"))
REARM_COOLDOWN_HOURS = float(os.getenv("REARM_COOLDOWN_HOURS", "12"))
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "25"))

DEX_BASE = "https://api.dexscreener.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "fomo-radar/2.0"})
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("fomo-radar")

# ------------------------------- MODELS -------------------------------
@dataclass
class PairData:
    ca: str
    pair_address: str
    symbol: str
    name: str
    url: str
    created_at_ms: int
    price_usd: float
    mc: float
    liquidity: float
    volume_5m: float
    volume_1h: float
    volume_24h: float
    buys_5m: int
    sells_5m: int
    buys_1h: int
    sells_1h: int
    price_change_5m: float
    price_change_1h: float
    price_change_6h: float
    price_change_24h: float

@dataclass
class SocialData:
    query: str = ""
    posts_15m: int = 0
    posts_prev_15m: int = 0
    posts_1h: int = 0
    unique_authors_1h: int = 0
    bot_risk: float = 0.0
    earliest_retrieved_url: str = ""
    earliest_retrieved_text: str = ""
    top_authors: str = ""
    narrative: str = ""
    narrative_confidence: str = "unknown"
    status: str = "disabled"

@dataclass
class HolderData:
    holders: int = 0
    top10_pct: float = 0.0
    complete: bool = False
    status: str = "disabled"

# ------------------------------- HELPERS ------------------------------
def now_ts() -> int:
    return int(time.time())

def fnum(v: Any, default: float = 0.0) -> float:
    try: return float(v or 0)
    except (TypeError, ValueError): return default

def inum(v: Any, default: int = 0) -> int:
    try: return int(v or 0)
    except (TypeError, ValueError): return default

def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))

def money(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.2f}M"
    if v >= 1e3: return f"${v/1e3:.0f}k"
    return f"${v:.0f}"

def pct(v: float) -> str:
    return f"{v:+.1f}%"

def age_text(ms: int) -> str:
    if not ms: return "N/D"
    hours = max(0, (time.time() * 1000 - ms) / 3_600_000)
    return f"{hours:.0f}h" if hours < 48 else f"{hours/24:.1f}d"

def safe_json(resp: requests.Response) -> Any:
    resp.raise_for_status()
    return resp.json()

def change_pct(current: float, previous: float) -> float:
    return ((current - previous) / max(previous, 1)) * 100 if previous > 0 else 0.0

# -------------------------------- DB ---------------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(parent, exist_ok=True)
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            ca TEXT PRIMARY KEY, symbol TEXT, name TEXT, pair_address TEXT, dex_url TEXT,
            created_at_ms INTEGER, status TEXT DEFAULT 'NEW', entry_mc REAL,
            peak_mc REAL DEFAULT 0, last_alert_mc REAL DEFAULT 0,
            peak_score REAL DEFAULT 0, peak_posts_1h INTEGER DEFAULT 0,
            peak_holder_growth REAL DEFAULT 0, last_score REAL DEFAULT 0,
            last_alert_type TEXT, last_alert_ts INTEGER DEFAULT 0,
            take_capital_sent INTEGER DEFAULT 0, exited_ts INTEGER DEFAULT 0,
            created_ts INTEGER, updated_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, ca TEXT NOT NULL,
            mc REAL, liquidity REAL, volume_5m REAL, volume_1h REAL,
            buys_5m INTEGER, sells_5m INTEGER, price_change_5m REAL,
            price_change_1h REAL, score REAL, posts_15m INTEGER DEFAULT 0,
            posts_1h INTEGER DEFAULT 0, unique_authors_1h INTEGER DEFAULT 0,
            bot_risk REAL DEFAULT 0, holders INTEGER DEFAULT 0,
            holders_complete INTEGER DEFAULT 0, top10_pct REAL DEFAULT 0,
            UNIQUE(ts, ca)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_ca_ts ON snapshots(ca, ts DESC);
        CREATE TABLE IF NOT EXISTS cache (
            cache_key TEXT PRIMARY KEY, fetched_ts INTEGER, payload TEXT
        );
        """)
        # Migraciones seguras para bases previas.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tokens)")}
        for name, ddl in {
            "peak_holder_growth": "REAL DEFAULT 0",
            "exited_ts": "INTEGER DEFAULT 0",
        }.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE tokens ADD COLUMN {name} {ddl}")
        scols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
        for name, ddl in {
            "holders": "INTEGER DEFAULT 0",
            "holders_complete": "INTEGER DEFAULT 0",
            "top10_pct": "REAL DEFAULT 0",
        }.items():
            if name not in scols:
                conn.execute(f"ALTER TABLE snapshots ADD COLUMN {name} {ddl}")

def cache_get(key: str, ttl_minutes: int) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM cache WHERE cache_key=?", (key,)).fetchone()
    if not row or now_ts() - row["fetched_ts"] > ttl_minutes * 60:
        return None
    return json.loads(row["payload"])

def cache_set(key: str, payload: Dict[str, Any]) -> None:
    with db() as conn:
        conn.execute("""INSERT INTO cache(cache_key,fetched_ts,payload) VALUES(?,?,?)
        ON CONFLICT(cache_key) DO UPDATE SET fetched_ts=excluded.fetched_ts,payload=excluded.payload""",
        (key, now_ts(), json.dumps(payload, ensure_ascii=False)))

def upsert_token(p: PairData) -> sqlite3.Row:
    ts = now_ts()
    with db() as conn:
        conn.execute("""INSERT INTO tokens
        (ca,symbol,name,pair_address,dex_url,created_at_ms,created_ts,updated_ts)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(ca) DO UPDATE SET symbol=excluded.symbol,name=excluded.name,
        pair_address=excluded.pair_address,dex_url=excluded.dex_url,
        created_at_ms=excluded.created_at_ms,updated_ts=excluded.updated_ts""",
        (p.ca,p.symbol,p.name,p.pair_address,p.url,p.created_at_ms,ts,ts))
        return conn.execute("SELECT * FROM tokens WHERE ca=?", (p.ca,)).fetchone()

def update_state(ca: str, **kwargs: Any) -> None:
    allowed = {"status","entry_mc","peak_mc","last_alert_mc","peak_score",
               "peak_posts_1h","peak_holder_growth","last_score","last_alert_type",
               "last_alert_ts","take_capital_sent","exited_ts","updated_ts"}
    kwargs["updated_ts"] = now_ts()
    clean = {k:v for k,v in kwargs.items() if k in allowed}
    if not clean: return
    sql = ",".join(f"{k}=?" for k in clean)
    with db() as conn:
        conn.execute(f"UPDATE tokens SET {sql} WHERE ca=?", list(clean.values())+[ca])

def previous_snapshot(ca: str, seconds_ago: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("""SELECT * FROM snapshots WHERE ca=? AND ts<=?
        ORDER BY ts DESC LIMIT 1""", (ca, now_ts()-seconds_ago)).fetchone()

def save_snapshot(p: PairData, s: SocialData, h: HolderData, score: float) -> None:
    with db() as conn:
        conn.execute("""INSERT OR IGNORE INTO snapshots
        (ts,ca,mc,liquidity,volume_5m,volume_1h,buys_5m,sells_5m,
         price_change_5m,price_change_1h,score,posts_15m,posts_1h,
         unique_authors_1h,bot_risk,holders,holders_complete,top10_pct)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now_ts(),p.ca,p.mc,p.liquidity,p.volume_5m,p.volume_1h,p.buys_5m,p.sells_5m,
         p.price_change_5m,p.price_change_1h,score,s.posts_15m,s.posts_1h,
         s.unique_authors_1h,s.bot_risk,h.holders,int(h.complete),h.top10_pct))

# ---------------------------- DEXSCREENER -----------------------------
def pair_from_raw(raw: Dict[str, Any], requested_ca: str) -> Optional[PairData]:
    if raw.get("chainId") != "solana": return None
    base, quote = raw.get("baseToken") or {}, raw.get("quoteToken") or {}
    if base.get("address") == requested_ca:
        token = base
    elif quote.get("address") == requested_ca:
        token = quote
    else:
        return None
    liq, vol, tx, pc = raw.get("liquidity") or {}, raw.get("volume") or {}, raw.get("txns") or {}, raw.get("priceChange") or {}
    # marketCap/fdv suele referirse al base. Si buscamos quote, no fingimos MC.
    mc = (fnum(raw.get("marketCap")) or fnum(raw.get("fdv"))) if token is base else 0.0
    return PairData(
        requested_ca, raw.get("pairAddress") or "", token.get("symbol") or "?",
        token.get("name") or "Unknown", raw.get("url") or "", inum(raw.get("pairCreatedAt")),
        fnum(raw.get("priceUsd")) if token is base else 0.0, mc, fnum(liq.get("usd")),
        fnum(vol.get("m5")), fnum(vol.get("h1")), fnum(vol.get("h24")),
        inum((tx.get("m5") or {}).get("buys")), inum((tx.get("m5") or {}).get("sells")),
        inum((tx.get("h1") or {}).get("buys")), inum((tx.get("h1") or {}).get("sells")),
        fnum(pc.get("m5")), fnum(pc.get("h1")), fnum(pc.get("h6")), fnum(pc.get("h24")))

def get_best_pair(ca: str) -> Optional[PairData]:
    data = safe_json(SESSION.get(f"{DEX_BASE}/token-pairs/v1/solana/{ca}", timeout=HTTP_TIMEOUT))
    pairs = [p for raw in (data or []) if (p := pair_from_raw(raw, ca))]
    # Preferimos pares donde el CA es base y existe MC; después, liquidez.
    pairs.sort(key=lambda p: (p.mc > 0, p.liquidity), reverse=True)
    return pairs[0] if pairs else None

def raw_discovery_addresses() -> List[str]:
    found = list(WATCHLIST_CAS)
    if ENABLE_DISCOVERY:
        for path in ("token-profiles/latest/v1","token-boosts/top/v1","token-boosts/latest/v1","community-takeovers/latest/v1"):
            try:
                data = safe_json(SESSION.get(f"{DEX_BASE}/{path}", timeout=HTTP_TIMEOUT))
                found += [x["tokenAddress"] for x in (data or []) if x.get("chainId")=="solana" and x.get("tokenAddress")]
            except Exception as exc:
                log.warning("Discovery %s falló: %s", path, exc)
    with db() as conn:
        found += [r[0] for r in conn.execute("SELECT ca FROM tokens WHERE status IN ('ACTIVE','COOLING')")]
    return list(dict.fromkeys(found))[:DISCOVERY_POOL_LIMIT]

def hard_filter(p: PairData) -> bool:
    if not (MIN_MC <= p.mc <= MAX_MC): return False
    if p.liquidity < MIN_LIQ or p.liquidity/max(p.mc,1) < MIN_LIQ_MC_RATIO: return False
    checks = [p.volume_1h >= MIN_VOL_1H, p.buys_5m >= MIN_BUYS_5M,
              p.buys_5m/max(p.sells_5m,1) >= MIN_BUY_SELL_RATIO,
              p.price_change_1h >= MIN_PRICE_1H]
    return sum(checks) >= 2

def discovery_rank(p: PairData) -> float:
    if p.mc <= 0: return -1
    return (min(p.volume_1h/max(p.mc,1), 5)*25 + min(p.liquidity/max(p.mc,1), .3)*100 +
            min(p.buys_5m, 200)/10 + clamp(p.price_change_1h, -100, 500)/20)

def discover_candidates() -> List[PairData]:
    pairs: List[PairData] = []
    for ca in raw_discovery_addresses():
        try:
            p = get_best_pair(ca)
            if p: pairs.append(p)
        except Exception as exc:
            log.debug("Pair %s falló: %s", ca, exc)
        time.sleep(.12)
    with db() as conn:
        active = {r[0] for r in conn.execute("SELECT ca FROM tokens WHERE status IN ('ACTIVE','COOLING')")}
    pairs.sort(key=lambda p: (p.ca in active, hard_filter(p), discovery_rank(p)), reverse=True)
    selected, seen = [], set()
    for p in pairs:
        if p.ca not in seen:
            seen.add(p.ca); selected.append(p)
        if len(selected) >= MAX_CANDIDATES: break
    return selected

# ------------------------------- HELIUS -------------------------------
def helius_holders(ca: str) -> HolderData:
    if not HELIUS_RPC_URL: return HolderData(status="disabled")
    key = f"holders:{ca}"
    if cached := cache_get(key, HOLDERS_REFRESH_MINUTES): return HolderData(**cached)
    balances: List[float] = []
    page, complete = 1, False
    try:
        while page <= MAX_HOLDER_PAGES:
            payload = {"jsonrpc":"2.0","id":"holders","method":"getTokenAccounts",
                       "params":{"mint":ca,"page":page,"limit":1000,"displayOptions":{}}}
            data = safe_json(SESSION.post(HELIUS_RPC_URL, json=payload, timeout=max(HTTP_TIMEOUT,45)))
            if data.get("error"): raise RuntimeError(data["error"])
            result = data.get("result") or {}
            items = result.get("token_accounts") or result.get("tokenAccounts") or []
            for x in items:
                amount = fnum(x.get("amount") or x.get("tokenAmount"))
                if amount > 0: balances.append(amount)
            if len(items) < 1000:
                complete = True; break
            page += 1
        total = sum(balances)
        top10 = sum(sorted(balances, reverse=True)[:10]) / total * 100 if total else 0
        out = HolderData(len(balances), round(top10,2), complete, "ok" if complete else "partial")
        cache_set(key, asdict(out)); return out
    except Exception as exc:
        log.warning("Helius holders %s: %s", ca, exc)
        return HolderData(status="error")

# -------------------------------- X ----------------------------------
def parse_dt(v: Any) -> Optional[datetime]:
    if not v: return None
    if isinstance(v,(int,float)):
        if v > 1e10: v /= 1000
        return datetime.fromtimestamp(v, timezone.utc)
    text = str(v).strip().replace("Z","+00:00")
    try:
        d = datetime.fromisoformat(text); return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError: pass
    for fmt in ("%a %b %d %H:%M:%S %z %Y","%Y-%m-%d %H:%M:%S%z"):
        try: return datetime.strptime(text,fmt)
        except ValueError: pass
    return None

def normalize_post(item: Dict[str,Any]) -> Optional[Dict[str,Any]]:
    text = item.get("text") or item.get("full_text") or item.get("tweetText") or item.get("content") or ""
    dt = parse_dt(item.get("createdAt") or item.get("created_at") or item.get("date") or item.get("timestamp"))
    if not text or not dt: return None
    author = item.get("author") or item.get("user") or {}
    if isinstance(author,str): username, followers = author, None
    else:
        username = author.get("userName") or author.get("username") or author.get("screen_name") or ""
        raw_followers = author.get("followers") if "followers" in author else author.get("followersCount")
        followers = inum(raw_followers) if raw_followers is not None else None
    pid = str(item.get("id") or item.get("tweetId") or item.get("id_str") or "")
    url = item.get("url") or item.get("tweetUrl") or (f"https://x.com/{username}/status/{pid}" if username and pid else "")
    unique = pid or url or hashlib.sha1((username+text+dt.isoformat()).encode()).hexdigest()
    return {"uid":unique,"text":text,"dt":dt.astimezone(timezone.utc),"username":username,"followers":followers,"url":url}

def estimate_bot_risk(posts: Sequence[Dict[str,Any]]) -> float:
    if not posts: return 0
    spam_re = re.compile(r"\b(100x|gem|ape|send it|ca:|contract|caller|alpha call|moon|buy now)\b",re.I)
    cleaned, spam, known_followers, tiny = [], 0, 0, 0
    for p in posts:
        t = re.sub(r"https?://\S+","",p["text"].lower())
        t = re.sub(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b","CA",t)
        cleaned.append(re.sub(r"\s+"," ",t).strip())
        spam += bool(spam_re.search(t))
        if p["followers"] is not None:
            known_followers += 1; tiny += p["followers"] < 100
    duplicate = 1-len(set(cleaned))/max(len(cleaned),1)
    tiny_ratio = tiny/known_followers if known_followers else 0
    return round(clamp(duplicate*55 + spam/len(posts)*30 + tiny_ratio*15),1)

def heuristic_narrative(posts: Sequence[Dict[str,Any]], p: PairData) -> str:
    stop = {"the","and","for","this","that","with","from","you","your","are","was","has","have","http","https",
            "solana","token","coin","pump","buy","ca","de","la","el","en","que","un","una","por","para"}
    words=[]
    for x in posts:
        text=re.sub(r"https?://\S+|\$\w+|[1-9A-HJ-NP-Za-km-z]{32,44}"," ",x["text"].lower())
        words += [w for w in re.findall(r"[a-záéíóúñ]{4,}",text) if w not in stop and w not in {p.symbol.lower(),p.name.lower()}]
    common=[w for w,_ in Counter(words).most_common(4)]
    return " / ".join(common) if common else "Sin narrativa identificable"

def llm_narrative(posts: Sequence[Dict[str,Any]], p: PairData, fallback: str) -> Tuple[str,str]:
    if not OPENAI_API_KEY or not posts: return fallback, "heuristic"
    excerpts = "\n".join(f"- {x['text'][:350]}" for x in posts[:40])
    prompt = ("Analiza estos posts recuperados de X sobre un memecoin. Devuelve JSON con narrative y confidence "
              "(high/medium/low). No afirmes que un hecho ocurrió si los posts solo lo atribuyen. "
              f"Token: ${p.symbol} {p.name}\n{excerpts}")
    try:
        resp=SESSION.post("https://api.openai.com/v1/responses",
            headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"},
            json={"model":OPENAI_MODEL,"input":prompt},timeout=60)
        data=safe_json(resp)
        text=data.get("output_text") or ""
        if not text:
            chunks=[]
            for o in data.get("output",[]):
                for c in o.get("content",[]):
                    if c.get("text"): chunks.append(c["text"])
            text="".join(chunks)
        parsed=json.loads(re.search(r"\{.*\}",text,re.S).group(0))
        return str(parsed.get("narrative") or fallback)[:250], str(parsed.get("confidence") or "low")
    except Exception as exc:
        log.warning("Narrativa LLM falló: %s",exc); return fallback,"heuristic"

def apify_social(p: PairData) -> SocialData:
    if not APIFY_TOKEN: return SocialData(query=p.ca,status="disabled")
    key=f"social:{p.ca}"
    if cached:=cache_get(key,SOCIAL_REFRESH_MINUTES): return SocialData(**cached)
    terms=[p.ca,f'"${p.symbol}"',f'"{p.name}"']
    endpoint=f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    payload={"searchTerms":terms,"maxItems":APIFY_MAX_POSTS,"sort":"Latest"}
    try:
        items=safe_json(SESSION.post(endpoint,params={"token":APIFY_TOKEN},json=payload,timeout=120))
        if not isinstance(items,list): raise RuntimeError("Apify no devolvió una lista; revisa actor y esquema")
        dedup: Dict[str,Dict[str,Any]]={}
        invalid=0
        for item in items:
            post=normalize_post(item) if isinstance(item,dict) else None
            if post: dedup[post["uid"]]=post
            else: invalid+=1
        posts=list(dedup.values())
        if items and not posts: raise RuntimeError("El esquema del Actor no coincide con normalize_post")
        now=datetime.now(timezone.utc)
        p15=[x for x in posts if x["dt"]>=now-timedelta(minutes=15)]
        prev=[x for x in posts if now-timedelta(minutes=30)<=x["dt"]<now-timedelta(minutes=15)]
        p1h=[x for x in posts if x["dt"]>=now-timedelta(hours=1)]
        oldest=min(posts,key=lambda x:x["dt"]) if posts else {}
        authors={}
        for x in p1h:
            if x["username"]: authors[x["username"]]=max(authors.get(x["username"],0),x["followers"] or 0)
        fallback=heuristic_narrative(p1h or posts,p)
        narrative,confidence=llm_narrative(p1h or posts,p,fallback)
        out=SocialData(" | ".join(terms),len(p15),len(prev),len(p1h),len(authors),
            estimate_bot_risk(p1h),oldest.get("url",""),oldest.get("text","")[:260],
            ", ".join(f"@{u}" for u,_ in sorted(authors.items(),key=lambda kv:kv[1],reverse=True)[:3]),
            narrative,confidence,"ok")
        cache_set(key,asdict(out)); return out
    except Exception as exc:
        log.warning("Apify %s: %s",p.symbol,exc)
        return SocialData(query=" | ".join(terms),status="error")

# ------------------------- SCORE AND LIFECYCLE ------------------------
def holder_growth(ca: str, h: HolderData) -> float:
    old=previous_snapshot(ca,3600)
    return change_pct(h.holders,inum(old["holders"])) if old and h.holders > 0 else 0.0

def calculate_score(p: PairData,s: SocialData,h: HolderData) -> Tuple[float,Dict[str,float]]:
    components: List[Tuple[str,float,float,bool]]=[]
    components += [
        ("volume",clamp(math.log10(max(p.volume_1h,1)/50000+1)*18,0,20),20,True),
        ("transactions",clamp(p.buys_5m/10 + (p.buys_5m/max(p.sells_5m,1)-1)*5,0,15),15,True),
        ("liquidity",clamp((p.liquidity/max(p.mc,1))/.15*15,0,15),15,True),
        ("price",clamp(p.price_change_1h/200*10,0,10),10,True),
    ]
    if s.status=="ok":
        acceleration=s.posts_15m/max(s.posts_prev_15m,1)
        components += [("social_volume",clamp(s.posts_1h/50*15,0,15),15,True),
                       ("social_acceleration",clamp((acceleration-1)*10,0,15),15,True),
                       ("social_quality",clamp((100-s.bot_risk)/10,0,10),10,True)]
    if h.status in ("ok","partial"):
        hg=holder_growth(p.ca,h)
        components += [("holders",clamp(hg/100*15,0,15),15,True),
                       ("distribution",clamp((45-h.top10_pct)/35*10,0,10),10,True)]
    earned=sum(v for _,v,_,enabled in components if enabled)
    possible=sum(mx for _,_,mx,enabled in components if enabled)
    normalized=earned/max(possible,1)*100
    # Con solo mercado exigimos más evidencia; el score no queda matemáticamente bloqueado.
    evidence_penalty = 0 if s.status=="ok" else 8
    if h.status not in ("ok","partial"): evidence_penalty += 5
    score=clamp(normalized-evidence_penalty)
    return round(score,1),{name:round(v,1) for name,v,_,_ in components}

def send_telegram(text:str)->None:
    if DRY_RUN or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n"+text+"\n"); return
    r=SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id":TELEGRAM_CHAT_ID,"text":text,"parse_mode":"HTML","disable_web_page_preview":True},timeout=HTTP_TIMEOUT)
    r.raise_for_status()

def cooling_signals(p:PairData,s:SocialData,h:HolderData,row:sqlite3.Row,score:float)->Tuple[int,List[str]]:
    reasons=[]
    if fnum(row["peak_score"])>=ENTRY_SCORE and score<=fnum(row["peak_score"])*COOLING_SCORE_RATIO: reasons.append("score")
    if p.mc<=max(fnum(row["last_alert_mc"]),fnum(row["entry_mc"]),1)*COOLING_MC_RATIO: reasons.append("market cap")
    if s.status=="ok" and inum(row["peak_posts_1h"])>=8 and s.posts_1h<=inum(row["peak_posts_1h"])*COOLING_SOCIAL_RATIO: reasons.append("X")
    hg=holder_growth(p.ca,h)
    if h.status in ("ok","partial") and fnum(row["peak_holder_growth"])>5 and hg<=fnum(row["peak_holder_growth"])*COOLING_HOLDER_RATIO: reasons.append("holders")
    return len(reasons),reasons

def alert_message(kind:str,p:PairData,s:SocialData,h:HolderData,row:sqlite3.Row,score:float,reasons:Sequence[str]=())->str:
    hg=holder_growth(p.ca,h)
    social=(f"X {s.posts_1h}/h, pico {max(inum(row['peak_posts_1h']),s.posts_1h)}/h" if s.status=="ok" else "X no disponible")
    holders=(f"Holders {h.holders}{'+' if not h.complete else ''}, 1h {hg:+.1f}%, Top10 {h.top10_pct:.1f}%" if h.status in ('ok','partial') else "Holders no disponibles")
    if kind=="ENTRY": title=f"🚀 ${p.symbol} · {p.name} · ✅ ÓPTIMO"
    elif kind=="TAKE_CAPITAL": title=f"💰 SACÁ CAPITAL · ${p.symbol}"
    elif kind=="COOLING": title=f"⚠️ SE ENFRÍA · ${p.symbol} · SALIDA"
    elif kind=="EXIT": title=f"🛑 SALIDA CONFIRMADA · ${p.symbol}"
    else: title=f"♻️ TREND REACTIVADO · ${p.symbol}"
    narrative=f"\n🔥 Trend: {html.escape(s.narrative)} ({s.narrative_confidence})" if s.narrative else ""
    reason_text=f"\nMotivos: {', '.join(reasons)}" if reasons else ""
    return (f"<b>{html.escape(title)}</b>\n💪 Fuerza {score:.0f}/100{narrative}\n"
            f"💰 MC {money(p.mc)} · 💧 Liq {money(p.liquidity)} ({p.liquidity/max(p.mc,1)*100:.1f}% MC)\n"
            f"📊 Vol 1h {money(p.volume_1h)} · 📈 1h {pct(p.price_change_1h)}\n"
            f"🐦 {html.escape(social)}\n👥 {html.escape(holders)}{reason_text}\n"
            f"📄 CA: <code>{html.escape(p.ca)}</code>\n"
            f"🔍 <a href=\"{html.escape(p.url)}\">DexScreener</a>"
            +(f"\n🐦 <a href=\"{html.escape(s.earliest_retrieved_url)}\">Post más antiguo recuperado</a>" if s.earliest_retrieved_url else ""))

def evaluate(p:PairData,s:SocialData,h:HolderData,score:float,row:sqlite3.Row)->None:
    hg=holder_growth(p.ca,h)
    peaks={"peak_mc":max(fnum(row["peak_mc"]),p.mc),"peak_score":max(fnum(row["peak_score"]),score),
           "peak_posts_1h":max(inum(row["peak_posts_1h"]),s.posts_1h),
           "peak_holder_growth":max(fnum(row["peak_holder_growth"]),hg),"last_score":score}
    update_state(p.ca,**peaks)
    status=row["status"] or "NEW"
    if status=="EXITED" and now_ts()-inum(row["exited_ts"])>=REARM_COOLDOWN_HOURS*3600 and score>=REARM_SCORE and hard_filter(p):
        send_telegram(alert_message("REARM",p,s,h,row,score))
        update_state(p.ca,status="ACTIVE",entry_mc=p.mc,peak_mc=p.mc,last_alert_mc=p.mc,peak_score=score,
                     peak_posts_1h=s.posts_1h,peak_holder_growth=hg,take_capital_sent=0,last_alert_type="REARM",last_alert_ts=now_ts())
        return
    if status=="NEW" and score>=ENTRY_SCORE and hard_filter(p):
        send_telegram(alert_message("ENTRY",p,s,h,row,score))
        update_state(p.ca,status="ACTIVE",entry_mc=p.mc,peak_mc=p.mc,last_alert_mc=p.mc,peak_score=score,
                     peak_posts_1h=s.posts_1h,peak_holder_growth=hg,last_alert_type="ENTRY",last_alert_ts=now_ts())
        return
    if status not in ("ACTIVE","COOLING") or not fnum(row["entry_mc"]): return
    if not inum(row["take_capital_sent"]) and p.mc>=fnum(row["entry_mc"])*TAKE_CAPITAL_MULTIPLE:
        send_telegram(alert_message("TAKE_CAPITAL",p,s,h,row,score))
        update_state(p.ca,take_capital_sent=1,last_alert_mc=p.mc,last_alert_type="TAKE_CAPITAL",last_alert_ts=now_ts()); return
    count,reasons=cooling_signals(p,s,h,row,score)
    if status=="ACTIVE" and count>=2:
        send_telegram(alert_message("COOLING",p,s,h,row,score,reasons))
        update_state(p.ca,status="COOLING",last_alert_mc=p.mc,last_alert_type="COOLING",last_alert_ts=now_ts()); return
    exit_reasons=[]
    if p.mc<=max(fnum(row["peak_mc"]),1)*EXIT_MC_PEAK_RATIO: exit_reasons.append("MC desde pico")
    if score<=EXIT_SCORE: exit_reasons.append("score")
    if s.status=="ok" and inum(row["peak_posts_1h"])>=8 and s.posts_1h<=max(2,inum(row["peak_posts_1h"])*.20): exit_reasons.append("X agotado")
    if status=="COOLING" and len(exit_reasons)>=2:
        send_telegram(alert_message("EXIT",p,s,h,row,score,exit_reasons))
        update_state(p.ca,status="EXITED",last_alert_mc=p.mc,last_alert_type="EXIT",last_alert_ts=now_ts(),exited_ts=now_ts())

# ---------------------------- HEALTH/MAIN -----------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/","/health"):
            self.send_response(404); self.end_headers(); return
        body=json.dumps({"status":"ok","time":datetime.now(timezone.utc).isoformat()}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): return

def start_health_server()->None:
    if PORT<=0: return
    threading.Thread(target=HTTPServer(("0.0.0.0",PORT),HealthHandler).serve_forever,daemon=True).start()
    log.info("Health endpoint activo en :%s/health",PORT)

def process_pair(p:PairData)->None:
    row=upsert_token(p)
    active=row["status"] in ("ACTIVE","COOLING")
    enrich=hard_filter(p) or active
    s=apify_social(p) if enrich else SocialData(status="skipped")
    h=helius_holders(p.ca) if enrich else HolderData(status="skipped")
    score,parts=calculate_score(p,s,h)
    save_snapshot(p,s,h,score)
    row=upsert_token(p)
    evaluate(p,s,h,score,row)
    log.info("$%s MC=%s score=%.1f social=%s holders=%s parts=%s",p.symbol,money(p.mc),score,s.status,h.status,parts)

def run_cycle()->None:
    candidates=discover_candidates()
    log.info("Candidatos seleccionados: %s",len(candidates))
    for p in candidates:
        try: process_pair(p)
        except Exception: log.exception("Error procesando %s",p.ca)
        time.sleep(.25)

def validate_runtime()->None:
    if not APIFY_TOKEN: log.warning("APIFY_TOKEN no configurado: score cuantitativo, sin narrativa social")
    if not HELIUS_RPC_URL: log.warning("HELIUS no configurado: sin holders")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: log.warning("Telegram no configurado: alertas impresas en stdout")
    if DB_PATH=="radar.db": log.warning("DB_PATH usa almacenamiento local. En Render configura un Persistent Disk o una base externa")

def main()->None:
    init_db(); validate_runtime(); start_health_server()
    log.info("Radar FOMO v2 iniciado DB=%s DRY_RUN=%s",DB_PATH,DRY_RUN)
    while True:
        started=time.time(); run_cycle()
        if RUN_ONCE: break
        time.sleep(max(5,POLL_SECONDS-(time.time()-started)))

if __name__=="__main__":
    try: main()
    except KeyboardInterrupt: sys.exit(0)
