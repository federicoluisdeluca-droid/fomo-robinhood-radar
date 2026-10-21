radar.py - MVP de radar FOMO para Solana, sin Grok.

Qué hace:
- Descubre candidatos desde DEX Screener o una watchlist de contratos.
- Guarda snapshots en SQLite.
- Calcula fuerza de mercado y deterioro del trend.
- Opcionalmente consulta X mediante un Actor de Apify.
- Envía alertas Telegram: ENTRADA, SACÁ CAPITAL, SE ENFRÍA y SALIDA.

Dependencia:
    pip install requests

Variables mínimas:
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_CHAT_ID=...

Opcionales:
    WATCHLIST_CAS=CA1,CA2
    APIFY_TOKEN=...
    APIFY_ACTOR_ID=apidojo~tweet-scraper
    ENABLE_DISCOVERY=true
    DRY_RUN=false
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
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

# --------------------------- CONFIGURACIÓN ---------------------------

DB_PATH = os.getenv("DB_PATH", "radar.db")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
RUN_ONCE = os.getenv("RUN_ONCE", "false").lower() == "true"
ENABLE_DISCOVERY = os.getenv("ENABLE_DISCOVERY", "true").lower() == "true"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "apidojo~tweet-scraper")
APIFY_MAX_POSTS = int(os.getenv("APIFY_MAX_POSTS", "80"))
SOCIAL_REFRESH_MINUTES = int(os.getenv("SOCIAL_REFRESH_MINUTES", "15"))

WATCHLIST_CAS = [x.strip() for x in os.getenv("WATCHLIST_CAS", "").split(",") if x.strip()]
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "20"))

MIN_MC = float(os.getenv("MIN_MC", "150000"))
MAX_MC = float(os.getenv("MAX_MC", "10000000"))
MIN_LIQ = float(os.getenv("MIN_LIQ", "25000"))
MIN_LIQ_MC_RATIO = float(os.getenv("MIN_LIQ_MC_RATIO", "0.07"))
MIN_VOL_1H = float(os.getenv("MIN_VOL_1H", "100000"))
MIN_BUYS_5M = int(os.getenv("MIN_BUYS_5M", "25"))
MIN_BUY_SELL_RATIO = float(os.getenv("MIN_BUY_SELL_RATIO", "1.30"))
MIN_PRICE_1H = float(os.getenv("MIN_PRICE_1H", "25"))

ENTRY_SCORE = float(os.getenv("ENTRY_SCORE", "62"))
WATCH_SCORE = float(os.getenv("WATCH_SCORE", "50"))
TAKE_CAPITAL_MULTIPLE = float(os.getenv("TAKE_CAPITAL_MULTIPLE", "2.0"))
COOLING_SOCIAL_PEAK_RATIO = float(os.getenv("COOLING_SOCIAL_PEAK_RATIO", "0.45"))
COOLING_MC_LAST_RATIO = float(os.getenv("COOLING_MC_LAST_RATIO", "0.72"))
EXIT_MC_PEAK_RATIO = float(os.getenv("EXIT_MC_PEAK_RATIO", "0.50"))
EXIT_SCORE = float(os.getenv("EXIT_SCORE", "35"))
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))

DEX_BASE = "https://api.dexscreener.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "fomo-radar-mvp/1.0"})

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("fomo-radar")

# ------------------------------ MODELOS ------------------------------

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
    query: str
    posts_15m: int = 0
    posts_prev_15m: int = 0
    posts_1h: int = 0
    unique_authors_1h: int = 0
    bot_risk: float = 0.0
    origin_url: str = ""
    origin_text: str = ""
    top_authors: str = ""
    status: str = "disabled"

# ----------------------------- UTILIDADES ----------------------------

def now_ts() -> int:
    return int(time.time())


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def inum(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value/1_000:.0f}k"
    return f"${value:.0f}"


def pct(value: float) -> str:
    return f"{value:+.1f}%"


def age_text(created_at_ms: int) -> str:
    if not created_at_ms:
        return "N/D"
    hours = max(0, (time.time() * 1000 - created_at_ms) / 3_600_000)
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours/24:.1f}d"


def safe_json(resp: requests.Response) -> Any:
    resp.raise_for_status()
    return resp.json()

# ------------------------------ SQLITE -------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                ca TEXT PRIMARY KEY,
                symbol TEXT,
                name TEXT,
                pair_address TEXT,
                dex_url TEXT,
                created_at_ms INTEGER,
                status TEXT DEFAULT 'NEW',
                entry_mc REAL,
                peak_mc REAL DEFAULT 0,
                last_alert_mc REAL DEFAULT 0,
                peak_score REAL DEFAULT 0,
                peak_posts_1h INTEGER DEFAULT 0,
                last_score REAL DEFAULT 0,
                last_alert_type TEXT,
                last_alert_ts INTEGER DEFAULT 0,
                take_capital_sent INTEGER DEFAULT 0,
                created_ts INTEGER,
                updated_ts INTEGER
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ca TEXT NOT NULL,
                mc REAL,
                liquidity REAL,
                volume_5m REAL,
                volume_1h REAL,
                buys_5m INTEGER,
                sells_5m INTEGER,
                price_change_5m REAL,
                price_change_1h REAL,
                score REAL,
                posts_15m INTEGER DEFAULT 0,
                posts_1h INTEGER DEFAULT 0,
                unique_authors_1h INTEGER DEFAULT 0,
                bot_risk REAL DEFAULT 0,
                UNIQUE(ts, ca)
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_ca_ts
            ON snapshots(ca, ts DESC);

            CREATE TABLE IF NOT EXISTS social_cache (
                cache_key TEXT PRIMARY KEY,
                fetched_ts INTEGER,
                payload TEXT
            );
            """
        )


def upsert_token(p: PairData) -> sqlite3.Row:
    ts = now_ts()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO tokens (
                ca, symbol, name, pair_address, dex_url, created_at_ms,
                created_ts, updated_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ca) DO UPDATE SET
                symbol=excluded.symbol,
                name=excluded.name,
                pair_address=excluded.pair_address,
                dex_url=excluded.dex_url,
                created_at_ms=excluded.created_at_ms,
                updated_ts=excluded.updated_ts
            """,
            (p.ca, p.symbol, p.name, p.pair_address, p.url, p.created_at_ms, ts, ts),
        )
        return conn.execute("SELECT * FROM tokens WHERE ca=?", (p.ca,)).fetchone()


def save_snapshot(p: PairData, s: SocialData, score: float) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO snapshots (
                ts, ca, mc, liquidity, volume_5m, volume_1h, buys_5m,
                sells_5m, price_change_5m, price_change_1h, score,
                posts_15m, posts_1h, unique_authors_1h, bot_risk
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_ts(), p.ca, p.mc, p.liquidity, p.volume_5m, p.volume_1h,
                p.buys_5m, p.sells_5m, p.price_change_5m, p.price_change_1h,
                score, s.posts_15m, s.posts_1h, s.unique_authors_1h, s.bot_risk,
            ),
        )


def recent_snapshots(ca: str, limit: int = 24) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM snapshots WHERE ca=? ORDER BY ts DESC LIMIT ?",
            (ca, limit),
        ).fetchall()


def update_state(ca: str, **kwargs: Any) -> None:
    if not kwargs:
        return
    kwargs["updated_ts"] = now_ts()
    allowed = {
        "status", "entry_mc", "peak_mc", "last_alert_mc", "peak_score",
        "peak_posts_1h", "last_score", "last_alert_type", "last_alert_ts",
        "take_capital_sent", "updated_ts",
    }
    clean = {k: v for k, v in kwargs.items() if k in allowed}
    sql = ", ".join(f"{k}=?" for k in clean)
    values = list(clean.values()) + [ca]
    with db() as conn:
        conn.execute(f"UPDATE tokens SET {sql} WHERE ca=?", values)

# --------------------------- DEX SCREENER ----------------------------

def pair_from_raw(raw: Dict[str, Any]) -> Optional[PairData]:
    if raw.get("chainId") != "solana":
        return None
    base = raw.get("baseToken") or {}
    liq = raw.get("liquidity") or {}
    vol = raw.get("volume") or {}
    tx = raw.get("txns") or {}
    pc = raw.get("priceChange") or {}
    ca = base.get("address") or ""
    if not ca:
        return None
    mc = fnum(raw.get("marketCap")) or fnum(raw.get("fdv"))
    return PairData(
        ca=ca,
        pair_address=raw.get("pairAddress") or "",
        symbol=base.get("symbol") or "?",
        name=base.get("name") or "Unknown",
        url=raw.get("url") or "",
        created_at_ms=inum(raw.get("pairCreatedAt")),
        price_usd=fnum(raw.get("priceUsd")),
        mc=mc,
        liquidity=fnum(liq.get("usd")),
        volume_5m=fnum(vol.get("m5")),
        volume_1h=fnum(vol.get("h1")),
        volume_24h=fnum(vol.get("h24")),
        buys_5m=inum((tx.get("m5") or {}).get("buys")),
        sells_5m=inum((tx.get("m5") or {}).get("sells")),
        buys_1h=inum((tx.get("h1") or {}).get("buys")),
        sells_1h=inum((tx.get("h1") or {}).get("sells")),
        price_change_5m=fnum(pc.get("m5")),
        price_change_1h=fnum(pc.get("h1")),
        price_change_6h=fnum(pc.get("h6")),
        price_change_24h=fnum(pc.get("h24")),
    )


def get_pairs_for_token(ca: str) -> List[PairData]:
    url = f"{DEX_BASE}/token-pairs/v1/solana/{ca}"
    data = safe_json(SESSION.get(url, timeout=HTTP_TIMEOUT))
    pairs = [p for x in (data or []) if (p := pair_from_raw(x))]
    return sorted(pairs, key=lambda x: x.liquidity, reverse=True)


def discover_addresses() -> List[str]:
    addresses = list(WATCHLIST_CAS)
    if not ENABLE_DISCOVERY:
        return addresses[:MAX_CANDIDATES]

    endpoints = [
        f"{DEX_BASE}/token-profiles/latest/v1",
        f"{DEX_BASE}/token-boosts/top/v1",
        f"{DEX_BASE}/token-boosts/latest/v1",
        f"{DEX_BASE}/community-takeovers/latest/v1",
    ]
    for url in endpoints:
        try:
            data = safe_json(SESSION.get(url, timeout=HTTP_TIMEOUT))
            for item in data or []:
                if item.get("chainId") == "solana" and item.get("tokenAddress"):
                    addresses.append(item["tokenAddress"])
        except Exception as exc:
            log.warning("Falló discovery %s: %s", url, exc)

    deduped = []
    seen = set()
    for ca in addresses:
        if ca not in seen:
            seen.add(ca)
            deduped.append(ca)
    return deduped[:MAX_CANDIDATES]

# ----------------------- PROVEEDOR SOCIAL APIFY ----------------------

def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            value /= 1000
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalize_post(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    text = item.get("text") or item.get("full_text") or item.get("tweetText") or ""
    created = item.get("createdAt") or item.get("created_at") or item.get("date")
    dt = parse_dt(created)
    if not text or not dt:
        return None
    author = item.get("author") or item.get("user") or {}
    if isinstance(author, str):
        username = author
        followers = 0
    else:
        username = author.get("userName") or author.get("username") or author.get("screen_name") or ""
        followers = inum(author.get("followers") or author.get("followersCount"))
    pid = item.get("id") or item.get("tweetId") or item.get("id_str") or ""
    url = item.get("url") or item.get("tweetUrl") or (f"https://x.com/{username}/status/{pid}" if username and pid else "")
    return {
        "id": str(pid),
        "text": text,
        "dt": dt.astimezone(timezone.utc),
        "username": username,
        "followers": followers,
        "url": url,
    }


def bot_risk(posts: Sequence[Dict[str, Any]]) -> float:
    if not posts:
        return 0.0
    spam_terms = re.compile(r"\b(100x|gem|ape|send it|ca:|contract|caller|alpha call|moon)\b", re.I)
    normalized = []
    spam = 0
    low_followers = 0
    for p in posts:
        text = re.sub(r"https?://\S+", "", p["text"].lower())
        text = re.sub(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", "CA", text)
        text = re.sub(r"\s+", " ", text).strip()
        normalized.append(text)
        spam += bool(spam_terms.search(text))
        low_followers += p.get("followers", 0) < 100
    duplicate_ratio = 1 - (len(set(normalized)) / max(len(normalized), 1))
    spam_ratio = spam / len(posts)
    low_ratio = low_followers / len(posts)
    return round(clamp(duplicate_ratio * 50 + spam_ratio * 30 + low_ratio * 20), 1)


def social_cache_get(key: str) -> Optional[SocialData]:
    with db() as conn:
        row = conn.execute("SELECT * FROM social_cache WHERE cache_key=?", (key,)).fetchone()
    if not row or now_ts() - row["fetched_ts"] > SOCIAL_REFRESH_MINUTES * 60:
        return None
    return SocialData(**json.loads(row["payload"]))


def social_cache_set(key: str, data: SocialData) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO social_cache(cache_key, fetched_ts, payload) VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                fetched_ts=excluded.fetched_ts, payload=excluded.payload
            """,
            (key, now_ts(), json.dumps(asdict(data), ensure_ascii=False)),
        )


def apify_social(p: PairData) -> SocialData:
    if not APIFY_TOKEN:
        return SocialData(query=p.ca, status="disabled")

    key = hashlib.sha256(f"{p.ca}:{p.symbol}:{p.name}".encode()).hexdigest()
    cached = social_cache_get(key)
    if cached:
        return cached

    # El CA reduce falsos positivos. El ticker/nombre captura narrativa sin CA.
    terms = [p.ca, f'"${p.symbol}"', f'"{p.name}"']
    endpoint = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    payload = {
        "searchTerms": terms,
        "maxItems": APIFY_MAX_POSTS,
        "sort": "Latest",
    }
    resp = SESSION.post(
        endpoint,
        params={"token": APIFY_TOKEN},
        json=payload,
        timeout=max(HTTP_TIMEOUT, 90),
    )
    items = safe_json(resp)
    posts = [x for item in (items or []) if (x := normalize_post(item))]

    now = datetime.now(timezone.utc)
    p15 = [x for x in posts if x["dt"] >= now - timedelta(minutes=15)]
    prev15 = [x for x in posts if now - timedelta(minutes=30) <= x["dt"] < now - timedelta(minutes=15)]
    p1h = [x for x in posts if x["dt"] >= now - timedelta(hours=1)]
    ordered = sorted(posts, key=lambda x: x["dt"])
    origin = ordered[0] if ordered else {}

    authors = {}
    for x in p1h:
        authors[x["username"]] = max(authors.get(x["username"], 0), x["followers"])
    top_authors = ", ".join(
        f"@{u}" for u, _ in sorted(authors.items(), key=lambda kv: kv[1], reverse=True)[:3] if u
    )

    result = SocialData(
        query=" | ".join(terms),
        posts_15m=len(p15),
        posts_prev_15m=len(prev15),
        posts_1h=len(p1h),
        unique_authors_1h=len({x["username"] for x in p1h if x["username"]}),
        bot_risk=bot_risk(p1h),
        origin_url=origin.get("url", ""),
        origin_text=origin.get("text", "")[:260],
        top_authors=top_authors,
        status="ok",
    )
    social_cache_set(key, result)
    return result

# ------------------------- FILTRO Y SCORING --------------------------

def hard_filter(p: PairData) -> bool:
    if not (MIN_MC <= p.mc <= MAX_MC):
        return False
    if p.liquidity < MIN_LIQ:
        return False
    if p.mc <= 0 or p.liquidity / p.mc < MIN_LIQ_MC_RATIO:
        return False
    conditions = [
        p.volume_1h >= MIN_VOL_1H,
        p.buys_5m >= MIN_BUYS_5M,
        p.buys_5m / max(p.sells_5m, 1) >= MIN_BUY_SELL_RATIO,
        p.price_change_1h >= MIN_PRICE_1H,
    ]
    return sum(conditions) >= 2


def previous_hour_snapshot(ca: str) -> Optional[sqlite3.Row]:
    cutoff = now_ts() - 3600
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM snapshots
            WHERE ca=? AND ts<=?
            ORDER BY ts DESC LIMIT 1
            """,
            (ca, cutoff),
        ).fetchone()


def calculate_score(p: PairData, s: SocialData) -> Tuple[float, Dict[str, float]]:
    liq_ratio = p.liquidity / max(p.mc, 1)
    buy_ratio = p.buys_5m / max(p.sells_5m, 1)

    volume_score = clamp(math.log10(max(p.volume_1h, 1) / 50_000 + 1) * 18, 0, 20)
    tx_score = clamp((p.buys_5m / 100) * 10 + (buy_ratio - 1) * 5, 0, 15)
    liquidity_score = clamp((liq_ratio / 0.15) * 15, 0, 15)
    price_score = clamp(p.price_change_1h / 200 * 10, 0, 10)

    if s.status == "ok":
        social_volume = clamp(s.posts_1h / 50 * 15, 0, 15)
        accel_ratio = s.posts_15m / max(s.posts_prev_15m, 1)
        social_accel = clamp((accel_ratio - 1) * 10, 0, 15)
        quality = clamp((100 - s.bot_risk) / 100 * 10, 0, 10)
    else:
        # Sin fuente social, el máximo real queda limitado.
        social_volume = social_accel = quality = 0

    parts = {
        "volume": volume_score,
        "transactions": tx_score,
        "liquidity": liquidity_score,
        "price": price_score,
        "social_volume": social_volume,
        "social_acceleration": social_accel,
        "social_quality": quality,
    }
    return round(sum(parts.values()), 1), parts


def market_label(score: float, p: PairData, s: SocialData) -> str:
    if score >= ENTRY_SCORE and p.liquidity / max(p.mc, 1) >= 0.08:
        return "✅ ÓPTIMO"
    if score >= WATCH_SCORE:
        return "🟡 OBSERVAR"
    return "⚪ FILTRADO"

# ----------------------------- ALERTAS -------------------------------

def send_telegram(text: str) -> None:
    if DRY_RUN or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n" + text + "\n")
        return
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = SESSION.post(
        endpoint,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()


def cooldown_ok(row: sqlite3.Row, alert_type: str) -> bool:
    if row["last_alert_type"] != alert_type:
        return True
    return now_ts() - inum(row["last_alert_ts"]) >= ALERT_COOLDOWN_MINUTES * 60


def build_entry_message(p: PairData, s: SocialData, score: float) -> str:
    social = (
        f"🐦 X recuperado: {s.posts_prev_15m}→{s.posts_15m}/15m · "
        f"{s.posts_1h}/h · autores {s.unique_authors_1h} · riesgo calls {s.bot_risk:.0f}/100"
        if s.status == "ok" else
        "🐦 X: fuente social no configurada"
    )
    return (
        f"🚀 <b>${html.escape(p.symbol)} · {html.escape(p.name)} · {market_label(score, p, s)}</b>\n"
        f"⛓️ Solana · ⏱️ {age_text(p.created_at_ms)}\n"
        f"💪 Fuerza {score:.0f}/100\n\n"
        f"{social}\n\n"
        f"💰 MC {money(p.mc)} · 💧 Liq {money(p.liquidity)} "
        f"({p.liquidity/max(p.mc,1)*100:.1f}% MC)\n"
        f"📊 Vol 1h {money(p.volume_1h)} · 24h {money(p.volume_24h)}\n"
        f"📈 Precio 5m {pct(p.price_change_5m)} · 1h {pct(p.price_change_1h)}\n"
        f"🛒 Buys/Sells 5m {p.buys_5m}/{p.sells_5m}\n\n"
        f"📄 CA: <code>{html.escape(p.ca)}</code>\n"
        f"🔍 <a href=\"{html.escape(p.url)}\">DexScreener</a>"
        + (f"\n🐦 <a href=\"{html.escape(s.origin_url)}\">Primer post recuperado</a>" if s.origin_url else "")
    )


def build_take_message(p: PairData, row: sqlite3.Row) -> str:
    gain = (p.mc / max(fnum(row["entry_mc"]), 1) - 1) * 100
    return (
        f"💰 <b>SACÁ CAPITAL · ${html.escape(p.symbol)}</b>\n"
        f"MC {money(p.mc)} ({gain:+.0f}% vs entrada)\n"
        f"📄 CA: <code>{html.escape(p.ca)}</code>"
    )


def build_cooling_message(p: PairData, s: SocialData, row: sqlite3.Row, score: float) -> str:
    last_mc = fnum(row["last_alert_mc"]) or p.mc
    diff = (p.mc / max(last_mc, 1) - 1) * 100
    social = (
        f"🐦 X: {s.posts_1h}/h ahora vs pico {inum(row['peak_posts_1h'])}/h"
        if s.status == "ok" else "🐦 X: sin fuente social"
    )
    return (
        f"⚠️ <b>SE ENFRÍA · ${html.escape(p.symbol)} · SALIDA</b>\n"
        f"💰 MC {money(p.mc)} ({diff:+.0f}% vs último aviso)\n"
        f"💪 Fuerza {score:.0f}/100 vs pico {fnum(row['peak_score']):.0f}/100\n"
        f"{social}\n"
        f"📄 CA: <code>{html.escape(p.ca)}</code>"
    )


def build_exit_message(p: PairData, s: SocialData, row: sqlite3.Row, score: float) -> str:
    from_peak = (p.mc / max(fnum(row["peak_mc"]), 1) - 1) * 100
    return (
        f"🛑 <b>SALIDA CONFIRMADA · ${html.escape(p.symbol)}</b>\n"
        f"MC {money(p.mc)} · {from_peak:+.0f}% desde pico\n"
        f"Fuerza {score:.0f}/100\n"
        f"X {s.posts_1h}/h" + (f" vs pico {inum(row['peak_posts_1h'])}/h" if s.status == "ok" else "") + "\n"
        f"📄 CA: <code>{html.escape(p.ca)}</code>"
    )


def evaluate_alerts(p: PairData, s: SocialData, score: float, row: sqlite3.Row) -> None:
    peak_mc = max(fnum(row["peak_mc"]), p.mc)
    peak_score = max(fnum(row["peak_score"]), score)
    peak_posts = max(inum(row["peak_posts_1h"]), s.posts_1h)
    update_state(
        p.ca,
        peak_mc=peak_mc,
        peak_score=peak_score,
        peak_posts_1h=peak_posts,
        last_score=score,
    )

    status = row["status"] or "NEW"
    entry_mc = fnum(row["entry_mc"])

    if status == "NEW" and score >= ENTRY_SCORE and hard_filter(p):
        msg = build_entry_message(p, s, score)
        send_telegram(msg)
        update_state(
            p.ca,
            status="ACTIVE",
            entry_mc=p.mc,
            peak_mc=p.mc,
            last_alert_mc=p.mc,
            peak_score=score,
            peak_posts_1h=s.posts_1h,
            last_alert_type="ENTRY",
            last_alert_ts=now_ts(),
        )
        return

    if status not in ("ACTIVE", "COOLING") or entry_mc <= 0:
        return

    if not inum(row["take_capital_sent"]) and p.mc >= entry_mc * TAKE_CAPITAL_MULTIPLE:
        send_telegram(build_take_message(p, row))
        update_state(
            p.ca,
            take_capital_sent=1,
            last_alert_mc=p.mc,
            last_alert_type="TAKE_CAPITAL",
            last_alert_ts=now_ts(),
        )
        return

    social_cooling = (
        s.status == "ok" and peak_posts >= 8 and
        s.posts_1h <= peak_posts * COOLING_SOCIAL_PEAK_RATIO
    )
    mc_cooling = p.mc <= max(fnum(row["last_alert_mc"]), entry_mc) * COOLING_MC_LAST_RATIO
    score_cooling = peak_score >= ENTRY_SCORE and score <= peak_score * 0.68

    if status == "ACTIVE" and sum([social_cooling, mc_cooling, score_cooling]) >= 2:
        if cooldown_ok(row, "COOLING"):
            send_telegram(build_cooling_message(p, s, row, score))
            update_state(
                p.ca,
                status="COOLING",
                last_alert_mc=p.mc,
                last_alert_type="COOLING",
                last_alert_ts=now_ts(),
            )
        return

    exit_market = p.mc <= peak_mc * EXIT_MC_PEAK_RATIO
    exit_score = score <= EXIT_SCORE
    social_dead = s.status == "ok" and peak_posts >= 8 and s.posts_1h <= max(2, peak_posts * 0.20)

    if status == "COOLING" and sum([exit_market, exit_score, social_dead]) >= 2:
        if cooldown_ok(row, "EXIT"):
            send_telegram(build_exit_message(p, s, row, score))
            update_state(
                p.ca,
                status="EXITED",
                last_alert_mc=p.mc,
                last_alert_type="EXIT",
                last_alert_ts=now_ts(),
            )

# --------------------------- CICLO PRINCIPAL -------------------------

def process_token(ca: str) -> None:
    pairs = get_pairs_for_token(ca)
    if not pairs:
        return
    p = pairs[0]
    row = upsert_token(p)

    # Tokens nuevos que no pasan filtro no consumen Apify.
    should_get_social = hard_filter(p) or row["status"] in ("ACTIVE", "COOLING")
    try:
        social = apify_social(p) if should_get_social else SocialData(query=p.ca, status="skipped")
    except Exception as exc:
        log.warning("Social falló para %s: %s", p.symbol, exc)
        social = SocialData(query=p.ca, status="error")

    score, parts = calculate_score(p, social)
    save_snapshot(p, social, score)
    log.info(
        "$%s | MC=%s | Liq=%s | Vol1h=%s | score=%.1f | social=%s",
        p.symbol, money(p.mc), money(p.liquidity), money(p.volume_1h), score, social.status,
    )
    row = upsert_token(p)
    evaluate_alerts(p, social, score, row)


def run_cycle() -> None:
    addresses = discover_addresses()
    log.info("Candidatos: %d", len(addresses))
    for ca in addresses:
        try:
            process_token(ca)
        except requests.HTTPError as exc:
            log.warning("HTTP %s: %s", ca, exc)
        except Exception:
            log.exception("Error procesando %s", ca)
        time.sleep(0.35)


def main() -> None:
    init_db()
    log.info("Radar FOMO iniciado | DB=%s | DRY_RUN=%s", DB_PATH, DRY_RUN)
    while True:
        started = time.time()
        run_cycle()
        if RUN_ONCE:
            break
        elapsed = time.time() - started
        time.sleep(max(5, POLL_SECONDS - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Radar detenido")
        sys.exit(0)
