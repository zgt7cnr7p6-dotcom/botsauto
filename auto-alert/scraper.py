#!/usr/bin/env python3
"""
Auto-Alert Scraper
Scraped mobile.de, AutoScout24 en Audi Gebrauchtwagenboerse voor Audi Q3 45 TFSI e (hybrid) in Duitsland.
Stuurt Telegram alerts met feature-checklist en prijsscore.
Anti-detectie: random delays, user-agent rotatie, menselijk browse-gedrag.
"""

import os
import re
import sys
import json
import random
import sqlite3
import asyncio
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from urllib.parse import urlparse, quote, parse_qs

import requests as req_lib
from bs4 import BeautifulSoup

# Optioneel: Camoufox (Firefox anti-detect) > Patchright > Playwright
# Camoufox: Firefox met C++ level fingerprint spoofing — beste tegen DataDome
# Patchright: Chromium met CDP leak fix
# Playwright: standaard (wordt gedetecteerd door DataDome)
HAS_CAMOUFOX = False
HAS_PLAYWRIGHT = False
PLAYWRIGHT_ENGINE = None

try:
    from camoufox.async_api import AsyncCamoufox
    HAS_CAMOUFOX = True
    PLAYWRIGHT_ENGINE = "camoufox"
except ImportError:
    pass

if not HAS_CAMOUFOX:
    try:
        from patchright.async_api import async_playwright
        HAS_PLAYWRIGHT = True
        PLAYWRIGHT_ENGINE = "patchright"
    except ImportError:
        try:
            from playwright.async_api import async_playwright
            HAS_PLAYWRIGHT = True
            PLAYWRIGHT_ENGINE = "playwright"
        except ImportError:
            pass

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = "--dry-run" in sys.argv or not TELEGRAM_BOT_TOKEN
PROXY_URL = os.environ.get("PROXY_URL", "")  # Residentiële proxy voor mobile.de
# Meerdere proxy URLs (komma-gescheiden) voor rotatie
# Bijv: "http://user:pass@gate.smartproxy.com:7000,http://user:pass@brd.superproxy.io:22225"
PROXY_URLS = [u.strip() for u in os.environ.get("PROXY_URLS", "").split(",") if u.strip()]
if PROXY_URL and PROXY_URL not in PROXY_URLS:
    PROXY_URLS.insert(0, PROXY_URL)

# Scrape.do API token — bypasses DataDome, anti-bot, CAPTCHAs
# Primaire methode voor mobile.de (betrouwbaarder dan TLS spoofing)
SCRAPE_DO_TOKEN = os.environ.get("SCRAPE_DO_TOKEN", "")

# ScraperAPI — alternatief voor Scrape.do (5000 gratis credits bij aanmelding)
# Signup: https://www.scraperapi.com/signup — gratis plan: 5000 credits, daarna 1000/maand
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "")

# ScrapFly — beste DataDome bypass (96% success rate)
# Signup: https://scrapfly.io/register — gratis plan: 1000 credits/maand
# ASP (anti-scraping protection) kost ~25 credits per request
SCRAPFLY_KEY = os.environ.get("SCRAPFLY_KEY", "")

# Max aantal detail pagina's per run — beperkt bandwidth bij proxy-only mode
# Elke detail pagina kost ~300KB. Bij 10 max = ~3MB per run, ~400MB/maand bij 5min interval
MAX_DETAIL_PAGES_PER_RUN = int(os.environ.get("MAX_DETAIL_PAGES", "10"))
_detail_page_count = 0  # teller voor detail pagina's per run


def parse_proxy_url(url: str) -> dict:
    """Parse http://user:pass@host:port naar Playwright proxy dict."""
    p = urlparse(url)
    proxy = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


def get_random_proxy() -> str | None:
    """Geef een willekeurige proxy URL terug, of None als er geen zijn."""
    return random.choice(PROXY_URLS) if PROXY_URLS else None

SEARCH_CRITERIA = {
    "model": "Audi Q3 45 TFSI e",
    "fuel": "hybrid",
    "year_min": 2021,          # vanaf 2021
    "km_max": 80_000,          # max 80k km
    "price_max": 40_000,       # max €40.000
    "country": "DE",
}

# mobile.de zoek-URLs
# URL 1: Q3 hybrid met "pano" in titel — alles doorsturen
# URL 2: Q3 Sportback hybrid ZONDER pano in titel — alleen doorsturen als beschrijving panoramadak bevat
MOBILE_DE_SEARCH_URLS = [
    {
        "url": (
            "https://suchen.mobile.de/fahrzeuge/search.html?"
            "dam=false&fr=2021%3A&ft=HYBRID&isSearchRequest=true"
            "&ml=%3A80000&ms=1900%3B37%3B%3Bpano&od=down"
            "&p=%3A40000&s=Car&sb=doc&vc=Car"
        ),
        "label": "Q3 pano",
        "require_pano_in_desc": False,  # pano zit al in URL-filter
    },
    {
        "url": (
            "https://suchen.mobile.de/fahrzeuge/search.html?"
            "cn=DE&dam=false&fr=2021%3A&ft=HYBRID&isSearchRequest=true"
            "&ml=%3A80000&ms=1900%3B37%3B%3Bsportback&od=down"
            "&p=%3A40000&s=Car&sb=doc&vc=Car"
        ),
        "label": "Q3 Sportback (pano check)",
        "require_pano_in_desc": True,  # moet panoramadak in beschrijving hebben
    },
]
# Backwards compat
MOBILE_DE_SEARCH_URL = MOBILE_DE_SEARCH_URLS[0]["url"]

# ── Full-option checklist (alle features die een perfecte Q3 heeft) ──
FULL_OPTION_FEATURES = [
    "panoramadak",
    "keyless",
    "camera_360",
    "s_line",
    "matrix_led",
    "velgen_20",
    "audio_premium",
    "elektrische_stoelen",
    "stoelverwarming",
    "stuurverwarming",
    "acc",
    "lane_assist",
    "drive_select",
    "adaptief_onderstel",
]

# Leesbare namen voor Telegram
FEATURE_DISPLAY_NAMES = {
    "panoramadak": "Panoramadak",
    "keyless": "Keyless Entry",
    "camera_360": "360° Camera",
    "s_line": "S-Line interieur",
    "matrix_led": "Matrix LED",
    "velgen_20": "20 inch velgen",
    "audio_premium": "Premium Audio (SONOS/B&O)",
    "elektrische_stoelen": "Elektrische stoelen + memory",
    "stoelverwarming": "Stoelverwarming",
    "stuurverwarming": "Stuurverwarming",
    "acc": "ACC (Abstandstempomat)",
    "lane_assist": "Lane assist + dodehoek",
    "drive_select": "Drive select",
    "adaptief_onderstel": "Adaptief onderstel",
}

DB_PATH = "listings.db"

# ── Anti-detectie ──────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]


async def human_delay(min_s=0.5, max_s=1.5):
    """Korte pauze tegen rate limiting."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def create_stealth_context(browser):
    """Maak een browser context die moeilijk te detecteren is."""
    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)

    context = await browser.new_context(
        user_agent=ua,
        viewport=vp,
        locale="de-DE",
        timezone_id="Europe/Berlin",
        geolocation={"latitude": 50.1109, "longitude": 8.6821},  # Frankfurt
        permissions=["geolocation"],
        color_scheme="light",
        java_script_enabled=True,
    )

    # Uitgebreide stealth: verberg webdriver, plugins, permissions etc.
    await context.add_init_script("""
        // Webdriver property verbergen
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        delete navigator.__proto__.webdriver;

        // Realistische plugins (Chrome PDF viewer etc.)
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const plugins = [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                    { name: 'Native Client', filename: 'internal-nacl-plugin' },
                ];
                plugins.length = 3;
                return plugins;
            }
        });

        Object.defineProperty(navigator, 'languages', {
            get: () => ['de-DE', 'de', 'en-US', 'en']
        });

        // Chrome object simuleren
        window.chrome = {
            runtime: {
                connect: function() {},
                sendMessage: function() {},
            },
            loadTimes: function() { return {}; },
            csi: function() { return {}; },
        };

        // Permissions API — altijd 'prompt' teruggeven
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters);

        // Canvas fingerprint randomisatie
        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type) {
            if (type === 'image/png' && this.width === 16 && this.height === 16) {
                return origToDataURL.apply(this, arguments);
            }
            return origToDataURL.apply(this, arguments);
        };
    """)

    return context


# ── Database ────────────────────────────────────────────────────────────────


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            price INTEGER,
            year INTEGER,
            km INTEGER,
            url TEXT,
            score INTEGER,
            features TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
        """
    )
    conn.commit()
    return conn


def listing_exists(conn, listing_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM listings WHERE id = ?", (listing_id,)
    ).fetchone()
    return row is not None


def save_listing(conn, listing: "Listing"):
    now = datetime.now(timezone.utc).isoformat()
    if listing_exists(conn, listing.id):
        conn.execute(
            "UPDATE listings SET last_seen = ?, price = ?, score = ? WHERE id = ?",
            (now, listing.price, listing.score, listing.id),
        )
    else:
        conn.execute(
            """INSERT INTO listings (id, source, title, price, year, km, url, score, features, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                listing.id,
                listing.source,
                listing.title,
                listing.price,
                listing.year,
                listing.km,
                listing.url,
                listing.score,
                json.dumps(listing.features),
                now,
                now,
            ),
        )
    conn.commit()


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class Listing:
    id: str
    source: str
    title: str
    price: int
    year: int
    km: int
    url: str
    description: str = ""
    location: str = ""
    listing_date: str = ""
    color: str = ""
    score: int = 0
    features: list = field(default_factory=list)


# ── Hybrid detectie ───────────────────────────────────────────────────────


def is_q3_hybrid(title: str, description: str = "", fuel_type: str = "") -> bool:
    """
    Detecteer of een listing een Q3 (Sportback) hybrid is.
    Checkt op Q3 in titel EN op hybrid indicators.
    Zoekt zowel Q3 als Q3 Sportback — de user zoekt op 'Q3 Pano' breed.
    """
    title_lower = title.lower()

    if "q3" not in title_lower:
        return False

    # Check hybrid — PRIMAIR via titel (betrouwbaarst)
    title_hybrid_patterns = [
        r"45\s*tfsi",             # "45 TFSI" of "45 TFSIe" (Q3 45 TFSI is altijd hybrid)
        r"tfsi\s*e\b",           # "TFSI e" (de e = elektrisch)
        r"plug[\s-]?in",         # "Plug-in Hybrid"
        r"phev",                 # PHEV
    ]
    if any(re.search(p, title_lower) for p in title_hybrid_patterns):
        return True

    # SECUNDAIR: check fuel_type (direct uit JSON, betrouwbaar)
    fuel_lower = fuel_type.lower()
    if any(kw in fuel_lower for kw in ["hybrid", "elektro", "phev", "plug-in", "electric"]):
        return True

    # TERTIAIR: check beschrijving, maar alleen op expliciete hybrid keywords
    # NIET op losse woorden als "elektro" of "benzin" (te veel false positives)
    desc_lower = description.lower()
    desc_hybrid_patterns = [
        r"plug[\s-]?in[\s-]?hybrid",
        r"\bphev\b",
        r"45\s*tfsi",
    ]
    return any(re.search(p, desc_lower) for p in desc_hybrid_patterns)


# ── Feature scoring ────────────────────────────────────────────────────────


FEATURE_PATTERNS = {
    "panoramadak": [
        r"panorama\s*d[ao][ck]h?",
        r"panoramadach",
        r"pano\b",
        r"panoramic",
        r"panorama\s*glas",
        r"panorama\s*schie?be?\s*dach",
        r"panorama\s*dak",
        r"panoramaverglasung",
        r"panorama\-schiebedach",
    ],
    "keyless": [
        r"keyless",
        r"komfort\s*schl[üu]ssel",
        r"komfortschl[üu]ssel",
        r"schl[üu]ssel\s*los",
        r"convenience\s*key",
        r"sleutel\s*loos",
        r"kessy",
    ],
    "camera_360": [
        r"360[\s°]*camera",
        r"360[\s°]?grad[\s-]*kamera",
        r"rundum[\s-]*kamera",
        r"surround\s*view",
        r"umgebungs\s*kamera",
        r"umgebungskamera",
    ],
    "s_line": [
        r"s[\s-]?line",
        r"s-line",
        r"sline",
    ],
    "matrix_led": [
        r"matrix[\s-]*led",
        r"matrix[\s-]*licht",
        r"matrix[\s-]*scheinwerfer",
        r"led[\s-]*matrix",
        r"matrixbeam",
    ],
    "velgen_20": [
        r"20[\s-]*zoll",
        r"20[\s-]*inch",
        r"20\s*\"",
        r"alufelgen\s*20",
        r"felgen\s*20",
        r"20['″\"]?\s*alu",
    ],
    "audio_premium": [
        r"bang[\s&+]*olufsen",
        r"b[\s&+]*o\b",
        r"b&o",
        r"sonos",
        r"premium\s*sound",
        r"sonos\s*premium",
    ],
    "elektrische_stoelen": [
        r"elektrische?\s*stoel",
        r"elektr.*sitz.*verstellung",
        r"el\.\s*sitz\s*verstellung",
        r"sitzverstellung.*elektr",
        r"power\s*seat",
        r"electric\s*seat",
        r"elektrisch\s*verstelba",
        r"memory",
    ],
    "stoelverwarming": [
        r"stoel\s*verwarming",
        r"sitz\s*heizung",
        r"sitzheizung",
        r"verwarmde?\s*stoel",
        r"beheizbare?\s*sitz",
        r"heated\s*seat",
    ],
    "stuurverwarming": [
        r"lenkrad\s*beheizt",
        r"lenkrad\s*heizung",
        r"lenkradheizung",
        r"stuur\s*verwarming",
        r"verwarm.*stuur",
        r"heated\s*steering",
        r"beheizbares?\s*lenkrad",
    ],
    "acc": [
        r"abstands?\s*tempo\s*mat",
        r"abstandstempomat",
        r"adaptive?\s*cruise",
        r"acc\b",
        r"adaptieve?\s*cruise",
        r"distronic",
    ],
    "lane_assist": [
        r"spur\s*halte\s*assist",
        r"spurhalteassist",
        r"lane\s*assist",
        r"totwinkel",
        r"tot[\s-]*winkel[\s-]*assist",
        r"blind\s*spot",
        r"dode\s*hoek",
        r"audi\s*side\s*assist",
        r"side\s*assist",
    ],
    "drive_select": [
        r"drive\s*select",
        r"audi\s*drive\s*select",
        r"fahrmodus",
        r"rijmodus",
    ],
    "adaptief_onderstel": [
        r"adaptie[fv].*onderstel",
        r"sport\s*onderstel",
        r"adaptiv.*fahrwerk",
        r"sport[\s-]*fahrwerk",
        r"damper\s*control",
        r"magnetic\s*ride",
        r"dynamic\s*chassis",
        r"fahrwerk\s*sport",
    ],
}


def parse_color(text: str) -> str:
    """Extraheer de exterieur kleur uit een beschrijving."""
    # Zoek naar expliciete kleur-labels
    for pat in [
        r"Au[ßs]en\s*farbe[:\s]+([A-ZÄÖÜa-zäöüß][\w\s-]{2,30})",
        r"Farbe[:\s]+([A-ZÄÖÜa-zäöüß][\w\s-]{2,30})",
        r"Exterieur\s*farbe[:\s]+([A-ZÄÖÜa-zäöüß][\w\s-]{2,30})",
        r"Lack(?:ierung)?[:\s]+([A-ZÄÖÜa-zäöüß][\w\s-]{2,30})",
        r"colour?[:\s]+([A-Za-z][\w\s-]{2,30})",
        r"color[:\s]+([A-Za-z][\w\s-]{2,30})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            color = m.group(1).strip().rstrip(".,;")
            # Filter onzinnige matches
            if len(color) > 2 and not any(w in color.lower() for w in ["fahrzeug", "ausstattung", "technisch"]):
                return color

    # Fallback: bekende Audi-kleuren
    known_colors = [
        "Mythos Schwarz", "Nano Grau", "Chronos Grau", "Glacier Wei",
        "Turbo Blau", "Pulse Orange", "Atoll Blau", "Florett Silber",
        "Manhattangrau", "Manhattan Grau", "Daytona Grau", "Perleffekt",
        "Schwarz", "Weiss", "Weiß", "Grau", "Silber", "Blau", "Rot",
        "Grün", "Braun", "Orange", "Metallic",
    ]
    text_lower = text.lower()
    for color in known_colors:
        if color.lower() in text_lower:
            # Probeer meer context te vinden (bijv. "Mythos Schwarz Metallic")
            idx = text_lower.index(color.lower())
            snippet = text[max(0, idx):idx + 40].strip()
            # Neem de eerste paar woorden
            words = snippet.split()[:4]
            result = " ".join(words).rstrip(".,;")
            if len(result) > 2:
                return result
    return ""


def score_listing(listing: Listing) -> Listing:
    """Score: tel hoeveel van de 14 full-option features aanwezig zijn."""
    text = f"{listing.title} {listing.description}".lower()
    found = []
    for feature, patterns in FEATURE_PATTERNS.items():
        if feature not in FULL_OPTION_FEATURES:
            continue
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                found.append(feature)
                log.info("Feature '%s' gevonden via '%s' => '%s'", feature, pat, m.group())
                break
    listing.features = found
    listing.score = len(found)

    # Extraheer kleur als die nog niet gezet is
    if not listing.color:
        listing.color = parse_color(listing.description)

    missing = [f for f in FULL_OPTION_FEATURES if f not in found]
    if missing:
        log.info(
            "Score %d/%d %s: MISSING=%s, desc_len=%d",
            listing.score, len(FULL_OPTION_FEATURES),
            listing.id[:30], missing, len(listing.description),
        )
    return listing


def compute_price_score(price: int) -> str:
    """Geef een prijsindicatie terug."""
    if not price:
        return "❓ Prijs onbekend"
    if price <= 28_000:
        return "🟢 Topdeal!"
    if price <= 32_000:
        return "🟢 Goed geprijsd"
    if price <= 35_000:
        return "🟡 Redelijke prijs"
    if price <= 38_000:
        return "🟠 Aan de bovenkant"
    return "🔴 Boven budget"


# ── Telegram ────────────────────────────────────────────────────────────────


def send_telegram(listing: Listing):
    max_score = len(FULL_OPTION_FEATURES)

    # Detecteer model uit titel
    title_lower = listing.title.lower()
    if "sportback" in title_lower:
        model_tag = "Q3 Sportback"
    else:
        model_tag = "Q3"

    # Kleur-indicator op basis van score
    pct = listing.score / max_score if max_score else 0
    if pct >= 0.90:
        header_emoji = "🟢🟢🟢"
        verdict = "TOPPER — bijna full option!"
    elif pct >= 0.75:
        header_emoji = "🟢🟢"
        verdict = "High spec"
    elif pct >= 0.55:
        header_emoji = "🟡"
        verdict = "Redelijk uitgerust"
    elif pct >= 0.35:
        header_emoji = "🟠"
        verdict = "Basis uitvoering"
    else:
        header_emoji = "🔴"
        verdict = "Kaal"

    # Checklist: alle features met ✅ / ❌
    check_lines = []
    missing_names = []
    for f in FULL_OPTION_FEATURES:
        name = FEATURE_DISPLAY_NAMES.get(f, f)
        if f in listing.features:
            check_lines.append(f"  ✅ {name}")
        else:
            check_lines.append(f"  ❌ {name}")
            missing_names.append(name)

    price_str = f"€{listing.price:,}" if listing.price else "onbekend"
    price_verdict = compute_price_score(listing.price)

    location_line = f"📍 {listing.location}\n" if listing.location else ""
    date_line = ""
    color_line = f"🎨 Kleur: {listing.color}\n" if listing.color else ""

    # Wat ontbreekt sectie
    missing_section = ""
    if missing_names:
        missing_section = (
            f"\n{'━' * 30}\n"
            f"<b>Wat ontbreekt:</b>\n"
            + "\n".join(f"  ❌ {name}" for name in missing_names)
            + "\n"
        )

    # Score balk visueel (gevulde/lege blokjes)
    filled = listing.score
    empty = max_score - filled
    score_bar = "▓" * filled + "░" * empty

    # Jaar weergave
    if listing.km == 0:
        year_display = "NIEUW"
    else:
        year_display = str(listing.year)

    text = (
        f"{header_emoji} <b>{model_tag}</b> — <b>{listing.score}/{max_score}</b> — {verdict}\n"
        f"<code>{score_bar}</code>\n"
        f"{'━' * 30}\n\n"
        f"<b>{listing.title}</b>\n\n"
        f"💰 <b>{price_str}</b>  {price_verdict}\n"
        f"📅 Bouwjaar: <b>{year_display}</b>\n"
        f"🛣 {listing.km:,} km\n"
        f"{color_line}"
        f"{location_line}"
        f"{date_line}"
        f"\n<b>Full option check:</b>\n"
        + "\n".join(check_lines)
        + missing_section
        + f"\n{'━' * 30}\n"
        f"🔗 <a href=\"{listing.url}\">👉 BEKIJK ADVERTENTIE</a>\n"
        f"📍 {listing.source}"
    )

    if DRY_RUN:
        log.info("[DRY-RUN] Telegram alert:\n%s", text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = req_lib.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if resp.ok:
        log.info("Telegram alert verstuurd voor %s", listing.id)
    else:
        log.error("Telegram fout: %s %s", resp.status_code, resp.text)


# ── Scrapers ────────────────────────────────────────────────────────────────


# ── mobile.de HTTP scraper (geen browser nodig) ──────────────────────────


def _mobile_de_headers() -> dict:
    """Realistische browser headers voor mobile.de."""
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.5,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }


def _fetch_via_scrapfly(url: str, render: bool = False) -> str | None:
    """Haal pagina op via ScrapFly (beste DataDome bypass, 96% success).

    asp=true activeert anti-scraping bypass (DataDome, Cloudflare, etc.)
    render_js=true activeert headless browser (nodig voor JS challenges).
    country=de zorgt voor Duits IP-adres.
    Kost ~10-25 credits per request (afhankelijk van opties).
    """
    params = {
        "key": SCRAPFLY_KEY,
        "url": url,
        "asp": "true",
        "country": "de",
    }
    if render:
        params["render_js"] = "true"
        params["rendering_wait"] = "3000"

    try:
        resp = req_lib.get("https://api.scrapfly.io/scrape", params=params, timeout=90)
        log.info("ScrapFly: status=%d, size=%d bytes voor %s", resp.status_code, len(resp.content), url[:80])

        if resp.status_code == 200:
            data = resp.json()
            result = data.get("result", {})
            content = result.get("content", "")
            if content and len(content) > 500:
                log.info("ScrapFly: succes — %d chars", len(content))
                return content
            else:
                log.warning("ScrapFly: lege of te kleine response (%d chars)", len(content))
                return None
        elif resp.status_code == 401:
            log.error("ScrapFly: ongeldige API key")
        elif resp.status_code == 429:
            log.warning("ScrapFly: rate limit / credits op")
        elif resp.status_code == 422:
            log.warning("ScrapFly: anti-bot bypass mislukt voor %s", url[:80])
        else:
            log.warning("ScrapFly: fout status %d — %s", resp.status_code, resp.text[:200])
        return None
    except req_lib.RequestException as e:
        log.error("ScrapFly: request mislukt: %s", e)
        return None


def _fetch_via_scraperapi(url: str, render: bool = False) -> str | None:
    """Haal pagina op via ScraperAPI (primair).

    ScraperAPI bypassed DataDome, Cloudflare, Akamai etc.
    render=true activeert headless browser (kost 5 credits i.p.v. 1).
    country_code=de zorgt voor Duits IP-adres.
    """
    api_url = (
        f"http://api.scraperapi.com"
        f"?api_key={SCRAPERAPI_KEY}"
        f"&url={quote(url)}"
        f"&country_code=de"
        f"&premium=true"
        f"&render={'true' if render else 'false'}"
    )

    try:
        resp = req_lib.get(api_url, timeout=90)
        log.info("ScraperAPI: status=%d, size=%d bytes voor %s", resp.status_code, len(resp.content), url[:80])

        if resp.status_code == 200:
            return resp.text
        elif resp.status_code == 401:
            log.error("ScraperAPI: ongeldige API key")
        elif resp.status_code == 429:
            log.warning("ScraperAPI: rate limit / credits op")
        elif resp.status_code == 403:
            log.warning("ScraperAPI: geblokkeerd (403) voor %s", url[:80])
        else:
            log.warning("ScraperAPI: fout status %d — %s", resp.status_code, resp.text[:200])
        return None
    except req_lib.RequestException as e:
        log.error("ScraperAPI: request mislukt: %s", e)
        return None


def _fetch_via_scrape_do(url: str, render: bool = False) -> str | None:
    """Haal pagina op via Scrape.do (fallback).

    super=true activeert geavanceerde anti-bot bypass (nodig voor mobile.de).
    """
    api_url = (
        f"https://api.scrape.do"
        f"?token={SCRAPE_DO_TOKEN}"
        f"&url={quote(url)}"
        f"&super=true"
    )

    try:
        resp = req_lib.get(api_url, timeout=60)
        log.info("Scrape.do: status=%d, size=%d bytes voor %s", resp.status_code, len(resp.content), url[:80])

        if resp.status_code == 200:
            return resp.text
        elif resp.status_code == 401:
            log.error("Scrape.do: ongeldige API token")
        elif resp.status_code == 429:
            log.warning("Scrape.do: rate limit / credits op")
        else:
            log.warning("Scrape.do: fout status %d — %s", resp.status_code, resp.text[:200])
        return None
    except req_lib.RequestException as e:
        log.error("Scrape.do: request mislukt: %s", e)
        return None


def _fetch_via_proxy(url: str, render: bool = False) -> str | None:
    """Haal pagina op via residential proxy + curl_cffi (Chrome TLS fingerprint).

    Geen externe API nodig — alleen een residential proxy (bijv. SmartProxy).
    curl_cffi bootst Chrome's TLS fingerprint na, waardoor DataDome het als
    echt browserverkeer ziet. render parameter wordt genegeerd (geen JS rendering).
    """
    proxy_url = get_random_proxy()
    if not proxy_url:
        log.warning("Directe proxy: geen PROXY_URL(S) geconfigureerd")
        return None

    proxy_label = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
    headers = _mobile_de_headers()
    # Forceer gzip compressie — bespaart ~70% bandwidth
    headers["Accept-Encoding"] = "gzip, deflate, br"
    proxies = {"http": proxy_url, "https": proxy_url}

    # Bepaal of warm-up nodig is (alleen voor mobile.de, niet voor audi-boerse etc.)
    is_mobile_de = "mobile.de" in url
    target_host = urlparse(url).hostname or ""

    try:
        if HAS_CURL_CFFI:
            session = curl_requests.Session(impersonate="chrome124")
            session.proxies = proxies

            # Warm-up: homepage eerst voor cookies/referer (alleen mobile.de)
            if is_mobile_de:
                log.info("Proxy [curl_cffi]: warm-up homepage via %s ...", proxy_label)
                try:
                    home_resp = session.get("https://www.mobile.de", headers=headers, timeout=30)
                    log.info("Proxy: homepage status=%d", home_resp.status_code)
                except Exception as e:
                    log.warning("Proxy: homepage warm-up mislukt: %s", e)
                time.sleep(random.uniform(0.5, 1.5))
                headers["Referer"] = "https://www.mobile.de/"
                headers["Sec-Fetch-Site"] = "same-origin"
            else:
                log.info("Proxy [curl_cffi]: ophalen via %s ...", proxy_label)

            resp = session.get(url, headers=headers, timeout=45)
        else:
            # Fallback: plain requests (minder effectief tegen DataDome)
            session = req_lib.Session()
            session.headers.update(headers)
            session.headers["User-Agent"] = random.choice(USER_AGENTS)
            session.proxies = proxies

            if is_mobile_de:
                log.info("Proxy [requests]: warm-up homepage via %s ...", proxy_label)
                try:
                    session.get("https://www.mobile.de", timeout=30)
                except Exception:
                    pass
                time.sleep(random.uniform(0.5, 1.5))
                headers["Referer"] = "https://www.mobile.de/"

            resp = session.get(url, timeout=45)

        log.info("Proxy: status=%d, size=%d bytes voor %s", resp.status_code, len(resp.content), url[:80])

        if resp.status_code == 200:
            html = resp.text
            # Check op IP-block
            if "zugriff verweigert" in html.lower() or "access denied" in html.lower():
                log.warning("Proxy: IP geblokkeerd door DataDome (Zugriff verweigert)")
                return None
            return html
        elif resp.status_code == 403:
            log.warning("Proxy: 403 Forbidden — DataDome block voor %s", url[:80])
        elif resp.status_code == 429:
            log.warning("Proxy: 429 rate limited")
        else:
            log.warning("Proxy: fout status %d", resp.status_code)
        return None

    except Exception as e:
        log.error("Proxy: request mislukt: %s", e)
        return None


def scrape_do_fetch(url: str, render: bool = False) -> str | None:
    """Haal een pagina op met anti-bot bypass.

    Probeert providers in volgorde:
      1. ScrapFly (SCRAPFLY_KEY) — beste DataDome bypass (96% success), 1000 gratis/maand
      2. ScraperAPI (SCRAPERAPI_KEY) — premium mode voor DataDome, 5000 credits bij signup
      3. Scrape.do (SCRAPE_DO_TOKEN) — fallback
      4. Directe proxy + curl_cffi (PROXY_URL) — gratis (alleen proxy kosten)
    Retourneert HTML string of None bij fout.
    """
    # Provider 1: ScrapFly (beste DataDome bypass)
    if SCRAPFLY_KEY:
        result = _fetch_via_scrapfly(url, render=render)
        if result:
            return result
        log.info("ScrapFly mislukt, probeer ScraperAPI ...")

    # Provider 2: ScraperAPI (premium mode)
    if SCRAPERAPI_KEY:
        result = _fetch_via_scraperapi(url, render=render)
        if result:
            return result
        log.info("ScraperAPI mislukt, probeer fallback ...")

    # Provider 3: Scrape.do (fallback)
    if SCRAPE_DO_TOKEN:
        result = _fetch_via_scrape_do(url, render=render)
        if result:
            return result
        log.info("Scrape.do mislukt, probeer proxy fallback ...")

    # Provider 4: Directe residential proxy + curl_cffi
    if PROXY_URLS:
        result = _fetch_via_proxy(url, render=render)
        if result:
            return result

    if not SCRAPFLY_KEY and not SCRAPERAPI_KEY and not SCRAPE_DO_TOKEN and not PROXY_URLS:
        log.error("Geen scraping methode geconfigureerd (SCRAPFLY_KEY, SCRAPERAPI_KEY, SCRAPE_DO_TOKEN, of PROXY_URL)")
    return None


def scrape_mobile_de_scrapedo(conn, search_url: str = "") -> list:
    """Scrape mobile.de via Scrape.do API + BeautifulSoup.

    Scrape.do bypassed DataDome automatisch — geen TLS spoofing of proxy nodig.
    Haalt ook detail pagina's op voor feature-detectie.
    """
    listings = []

    if not SCRAPFLY_KEY and not SCRAPERAPI_KEY and not SCRAPE_DO_TOKEN and not PROXY_URLS:
        log.info("mobile.de: geen scraping methode beschikbaar, overslaan")
        return listings

    if not search_url:
        search_url = MOBILE_DE_SEARCH_URL

    log.info("mobile.de Scrape.do: zoekpagina ophalen ...")
    html = scrape_do_fetch(search_url)

    if not html:
        log.error("mobile.de Scrape.do: geen HTML ontvangen")
        return listings

    # Detecteer IP-block (zou niet moeten met Scrape.do, maar voor de zekerheid)
    if "zugriff verweigert" in html.lower() or "access denied" in html.lower():
        log.error("mobile.de Scrape.do: geblokkeerd (Zugriff verweigert)")
        try:
            with open("debug_mobile_scrapedo.html", "w", encoding="utf-8") as f:
                f.write(html[:500_000])
        except Exception:
            pass
        return listings

    # Debug: sla HTML op
    try:
        with open("debug_mobile_scrapedo.html", "w", encoding="utf-8") as f:
            f.write(html[:500_000])
    except Exception:
        pass

    # Parse met BeautifulSoup (zelfde logica als HTTP scraper)
    soup = BeautifulSoup(html, "html.parser")

    page_title = soup.title.text if soup.title else ""
    log.info("mobile.de Scrape.do: page title = '%s'", page_title)

    # Zoek listing cards
    cards = soup.select("a[data-testid*='result'], a[data-testid*='listing']")
    if not cards:
        cards = soup.select("a.result-item, div.cBox-body--resultitem")
    if not cards:
        cards = soup.select("a[href*='/fahrzeuge/details']")

    log.info("mobile.de Scrape.do: %d cards gevonden", len(cards))

    if not cards:
        body = soup.get_text(separator=" ", strip=True)
        log.info("mobile.de Scrape.do body (2000 chars): %s", body[:2000])
        return listings

    known_streak = 0
    for card in cards[:50]:
        try:
            # Skip gesponsorde advertenties
            card_full_text = card.get_text(separator=" ", strip=True)
            if "gesponsert" in card_full_text.lower()[:50]:
                log.info("Gesponserde advertentie overgeslagen")
                continue

            # Title
            title = ""
            for tag in card.select("h2, span.h3, .u-text-break-word"):
                title = tag.get_text(strip=True)
                if title:
                    break
            if not title:
                title = card_full_text.split("\n")[0][:100]

            # Strip "Gesponsert" prefix uit titel
            title = re.sub(r"^Gesponsert\s*", "", title, flags=re.IGNORECASE)

            if not title or "q3" not in title.lower():
                continue

            # URL
            href = card.get("href", "")
            if not href:
                link = card.select_one("a[href*='/fahrzeuge/']")
                if link:
                    href = link.get("href", "")
            if href and not href.startswith("http"):
                href = "https://suchen.mobile.de" + href

            listing_id = extract_listing_id(href, "mobile", fallback=str(abs(hash(title))))

            if listing_exists(conn, listing_id):
                known_streak += 1
                log.info("Bekende listing: %s", title[:50])
                if known_streak >= 5:
                    log.info("5 bekende op rij — stoppen")
                    break
                continue
            known_streak = 0

            # Price
            card_text = card_full_text
            price = 0
            for price_el in card.select("span[data-testid*='price'], .price-block span"):
                price = parse_price(price_el.get_text())
                if price:
                    break
            if not price:
                price_match = re.search(r"€\s*([\d.]+)", card_text)
                if price_match:
                    price = parse_price(price_match.group(1))

            # KM
            km = 0
            km_match = re.search(r"([\d.]+)\s*km", card_text, re.IGNORECASE)
            if km_match:
                km = parse_km(km_match.group(0))

            # Year
            year = parse_year(card_text)

            # Detail pagina ophalen voor feature-detectie (bandwidth-bewust)
            global _detail_page_count
            description = card_text[:500]
            location = ""
            listing_date = ""
            if href and _detail_page_count < MAX_DETAIL_PAGES_PER_RUN:
                _detail_page_count += 1
                log.info("mobile.de: detail ophalen [%d/%d] voor %s ...",
                         _detail_page_count, MAX_DETAIL_PAGES_PER_RUN, title[:50])
                time.sleep(random.uniform(0.2, 0.5))  # Minimale pauze
                detail_html = scrape_do_fetch(href, render=True)
                if detail_html:
                    detail_soup = BeautifulSoup(detail_html, "html.parser")
                    description = ""

                    # 1. Titel/kopje
                    for sel in ["h1", "#rbt-ad-title", "[data-testid='ad-title']"]:
                        el = detail_soup.select_one(sel)
                        if el:
                            description += " " + el.get_text(separator=" ", strip=True)
                            break

                    # 2. Technische data
                    for sel in [
                        "div.cBox-body.cBox-body--technical-data",
                        ".cBox--technicalData",
                        "#rbt-td",
                        "[data-testid='ad-detail-technical-data']",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            description += " " + el.get_text(separator=" ", strip=True)

                    # 3. Features / Ausstattung (volledig na JS render)
                    for sel in [
                        ".cBox--features", "#features",
                        "[data-testid='ad-detail-features']",
                        "[data-testid='ad-detail-equipment']",
                        "[data-testid='equipment']",
                        "[class*='FeatureList']",
                        "[class*='equipment']",
                        "[class*='Equipment']",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            description += " " + el.get_text(separator=" ", strip=True)

                    # 4. Vehicle Description (volledig na JS render)
                    for sel in [
                        "#description", ".cBox--vehicleDescription",
                        "[data-testid='ad-detail-description']",
                        "[class*='VehicleDescription']",
                        "[class*='vehicleDescription']",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            description += " " + el.get_text(separator=" ", strip=True)

                    # FALLBACK: als specifieke selectors weinig opleverden
                    if len(description.strip()) < 100:
                        body_text = detail_soup.get_text(separator=" ", strip=True)
                        description = body_text[:15000]
                        log.info("Scrape.do detail fallback (body text): %d chars", len(description))

                    # Locatie (verkoper)
                    for sel in [
                        "#dealer-hp-link-bottom",
                        ".seller-info .seller-address",
                        "[data-testid='seller-info'] p",
                        ".cBox--seller .u-text-break-word",
                        ".cBox--seller",
                        "#rbt-seller",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            loc_text = el.get_text(separator=" ", strip=True)
                            # Probeer postcode + stad te extracten (bijv. "53111 Bonn")
                            loc_match = re.search(r"(\d{5}\s+\S+(?:\s+\S+)?)", loc_text)
                            if loc_match:
                                location = loc_match.group(1).strip()
                            elif len(loc_text) < 100:
                                location = loc_text
                            break

                    # Listing datum (wanneer geplaatst)
                    for sel in [
                        "[data-testid='creation-date']",
                        ".cBox--attributes .u-text-break-word",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            date_text = el.get_text(strip=True)
                            date_match = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})", date_text)
                            if date_match:
                                listing_date = date_match.group(1)
                            break
                    # Fallback: zoek datum in hele pagina tekst
                    if not listing_date:
                        full_text = detail_soup.get_text()
                        date_match = re.search(r"Inseriert?\s*(?:am\s*)?:?\s*(\d{1,2}\.\d{1,2}\.\d{4})", full_text)
                        if date_match:
                            listing_date = date_match.group(1)
                        else:
                            # Probeer "seit DD.MM.YYYY" patroon
                            date_match = re.search(r"seit\s+(\d{1,2}\.\d{1,2}\.\d{4})", full_text, re.IGNORECASE)
                            if date_match:
                                listing_date = date_match.group(1)

                    log.info("mobile.de Scrape.do: detail beschrijving %d chars, locatie=%s, datum=%s",
                             len(description), location or "?", listing_date or "?")

            listing = Listing(
                id=listing_id,
                source="mobile.de",
                title=title,
                price=price,
                year=year,
                km=km,
                url=href,
                description=description,
                location=location,
                listing_date=listing_date,
            )

            log.info("MATCH: %s — €%s — %d km — %d", title[:50], f"{price:,}" if price else "?", km, year)
            listings.append(listing)

        except Exception as e:
            log.warning("mobile.de Scrape.do card: %s", e)
            continue

    log.info("mobile.de Scrape.do: %d listings gevonden", len(listings))
    return listings


def scrape_mobile_de_http(conn, proxy_url: str | None = None, search_url: str = "") -> list:
    """Scrape mobile.de via HTTP + BeautifulSoup.

    Gebruikt curl_cffi (Chrome TLS fingerprint) als beschikbaar,
    anders plain requests als fallback.
    """
    listings = []

    if not search_url:
        search_url = MOBILE_DE_SEARCH_URL

    proxy_label = f"MET proxy ({proxy_url.split('@')[-1] if '@' in (proxy_url or '') else proxy_url})" if proxy_url else "ZONDER proxy"
    use_impersonate = HAS_CURL_CFFI
    engine = "curl_cffi (Chrome TLS)" if use_impersonate else "requests (plain)"
    log.info("mobile.de HTTP [%s]: %s", engine, proxy_label)

    headers = _mobile_de_headers()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    try:
        # Stap 1: Bezoek homepage (cookie + referer opbouwen)
        log.info("mobile.de HTTP: warm-up homepage ...")
        time.sleep(random.uniform(0.5, 1.5))

        if use_impersonate:
            # curl_cffi: impersonate Chrome 124 — TLS fingerprint identiek aan echte Chrome
            session = curl_requests.Session(impersonate="chrome124")
            if proxies:
                session.proxies = proxies
            home_resp = session.get("https://www.mobile.de", headers=headers, timeout=30)
        else:
            session = req_lib.Session()
            session.headers.update(headers)
            session.headers["User-Agent"] = random.choice(USER_AGENTS)
            if proxies:
                session.proxies = proxies
            home_resp = session.get("https://www.mobile.de", timeout=30)

        log.info("mobile.de HTTP: homepage status=%d", home_resp.status_code)

        if home_resp.status_code != 200:
            log.warning("mobile.de HTTP: homepage gaf %d", home_resp.status_code)

        time.sleep(random.uniform(0.3, 0.8))

        # Stap 2: Zoekresultaten ophalen
        headers["Referer"] = "https://www.mobile.de/"
        headers["Sec-Fetch-Site"] = "same-origin"

        log.info("mobile.de HTTP: zoekpagina ophalen ...")
        if use_impersonate:
            resp = session.get(search_url, headers=headers, timeout=30)
        else:
            resp = session.get(search_url, timeout=30)
        log.info("mobile.de HTTP: status=%d, size=%d bytes", resp.status_code, len(resp.content))

        if resp.status_code != 200:
            log.error("mobile.de HTTP: fout status %d", resp.status_code)
            return listings

        html = resp.text

        # Detecteer IP-block
        if "zugriff verweigert" in html.lower() or "access denied" in html.lower():
            log.error("mobile.de HTTP: IP geblokkeerd (Zugriff verweigert)")
            # Debug: sla op
            try:
                with open("debug_mobile_http.html", "w", encoding="utf-8") as f:
                    f.write(html[:500_000])
            except Exception:
                pass
            return listings

        # Debug: sla HTML op
        try:
            with open("debug_mobile_http.html", "w", encoding="utf-8") as f:
                f.write(html[:500_000])
        except Exception:
            pass

        # Stap 3: Parse met BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        page_title = soup.title.text if soup.title else ""
        log.info("mobile.de HTTP: page title = '%s'", page_title)

        # Methode 1: data-testid links (moderne mobile.de)
        cards = soup.select("a[data-testid*='result'], a[data-testid*='listing']")
        if not cards:
            # Methode 2: legacy selectors
            cards = soup.select("a.result-item, div.cBox-body--resultitem")
        if not cards:
            # Methode 3: alle links naar /fahrzeuge/ details
            cards = soup.select("a[href*='/fahrzeuge/details']")

        log.info("mobile.de HTTP: %d cards gevonden", len(cards))

        if not cards:
            # Probeer JSON-LD of script data te extracten
            for script in soup.select("script[type='application/ld+json']"):
                try:
                    data = json.loads(script.string)
                    log.info("mobile.de HTTP: JSON-LD gevonden: %s", str(data)[:500])
                except Exception:
                    pass
            # Log body text voor debug
            body = soup.get_text(separator=" ", strip=True)
            log.info("mobile.de HTTP body (2000 chars): %s", body[:2000])
            return listings

        known_streak = 0
        for card in cards[:50]:
            try:
                # Skip gesponsorde advertenties
                card_full_text = card.get_text(separator=" ", strip=True)
                if "gesponsert" in card_full_text.lower()[:50]:
                    log.info("Gesponsorde advertentie overgeslagen")
                    continue

                # Title
                title = ""
                for tag in card.select("h2, span.h3, .u-text-break-word"):
                    title = tag.get_text(strip=True)
                    if title:
                        break
                if not title:
                    title = card_full_text.split("\n")[0][:100]

                # Strip "Gesponsert" prefix uit titel
                title = re.sub(r"^Gesponsert\s*", "", title, flags=re.IGNORECASE)

                if not title or "q3" not in title.lower():
                    continue

                # URL
                href = card.get("href", "")
                if not href:
                    link = card.select_one("a[href*='/fahrzeuge/']")
                    if link:
                        href = link.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://suchen.mobile.de" + href

                listing_id = extract_listing_id(href, "mobile", fallback=str(abs(hash(title))))

                if listing_exists(conn, listing_id):
                    known_streak += 1
                    log.info("Bekende listing: %s", title[:50])
                    if known_streak >= 5:
                        log.info("5 bekende op rij — stoppen")
                        break
                    continue
                known_streak = 0

                # Price — zoek in card text
                card_text = card_full_text
                price = 0
                for price_el in card.select("span[data-testid*='price'], .price-block span"):
                    price = parse_price(price_el.get_text())
                    if price:
                        break
                if not price:
                    price_match = re.search(r"€\s*([\d.]+)", card_text)
                    if price_match:
                        price = parse_price(price_match.group(1))

                # KM
                km = 0
                km_match = re.search(r"([\d.]+)\s*km", card_text, re.IGNORECASE)
                if km_match:
                    km = parse_km(km_match.group(0))

                # Year
                year = parse_year(card_text)

                listing = Listing(
                    id=listing_id,
                    source="mobile.de",
                    title=title,
                    price=price,
                    year=year,
                    km=km,
                    url=href,
                    description=card_text[:500],
                )
                log.info("MATCH: %s — €%s — %d km — %d", title[:50], f"{price:,}" if price else "?", km, year)
                listings.append(listing)

            except Exception as e:
                log.warning("mobile.de HTTP card: %s", e)
                continue

    except req_lib.RequestException as e:
        log.error("mobile.de HTTP request mislukt: %s", e)
    except Exception as e:
        log.error("mobile.de HTTP scraper fout: %s", e, exc_info=True)

    return listings


def parse_price(text: str) -> int:
    """Extract numeric price from text like '€ 34.900' or '34900'."""
    cleaned = re.sub(r"[^\d]", "", text)
    return int(cleaned) if cleaned else 0


def parse_km(text: str) -> int:
    """Extract km from text like '45.000 km'."""
    cleaned = re.sub(r"[^\d]", "", text.split("km")[0] if "km" in text.lower() else text)
    return int(cleaned) if cleaned else 0


def parse_year(text: str) -> int:
    """Extract year from text."""
    # Try EZ (Erstzulassung) pattern specific to mobile.de
    ez_match = re.search(r"EZ\s*(\d{2}/)?(20[12]\d)", text, re.IGNORECASE)
    if ez_match:
        return int(ez_match.group(2))

    # Find all year candidates
    matches = [int(m) for m in re.findall(r"(20[12]\d)", text)]
    if not matches:
        return 0

    current_year = datetime.now().year
    # Filter out current/future years (likely posting dates, not build years)
    past_years = [y for y in matches if y < current_year]
    if past_years:
        return max(past_years)  # Most recent past year = most likely build year
    return matches[0]


def extract_listing_id(href: str, source: str, fallback: str = "") -> str:
    """Extract een stabiel listing ID uit een URL.

    mobile.de: pakt 'id' query parameter of numeriek pad-segment
    autoscout24: pakt het laatste pad-segment (UUID/slug)
    Fallback: hash van de volledige URL (zonder tracking params)
    """
    if not href:
        return f"{source}_{fallback}" if fallback else ""

    parsed = urlparse(href)

    if source == "mobile":
        # mobile.de: ?id=354960329
        params = parse_qs(parsed.query)
        if "id" in params:
            return f"mobile_{params['id'][0]}"
        # Fallback: zoek numeriek segment in pad
        nums = re.findall(r"(\d{6,})", parsed.path)
        if nums:
            return f"mobile_{nums[-1]}"

    elif source == "as24":
        # autoscout24: /angebote/audi-q3-...-uuid
        path = parsed.path.rstrip("/")
        if path:
            slug = path.split("/")[-1]
            return f"as24_{slug}"

    elif source == "audi":
        # audi-boerse.de: zoek numeriek ID in URL of pad
        params = parse_qs(parsed.query)
        if "id" in params:
            return f"audi_{params['id'][0]}"
        nums = re.findall(r"(\d{6,})", parsed.path)
        if nums:
            return f"audi_{nums[-1]}"
        # Slug-based fallback
        path = parsed.path.rstrip("/")
        if path:
            slug = path.split("/")[-1]
            if len(slug) > 3:
                return f"audi_{slug}"

    # Fallback: hash van URL zonder query params
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return f"{source}_{abs(hash(clean_url)) % 10**12}"


async def scrape_mobile_de(page, conn, search_url: str = "") -> list[Listing]:
    """Scrape mobile.de for Audi Q3 hybrid listings in Duitsland.
    Blokkeert images/fonts voor snelheid via proxy."""
    listings = []
    if not search_url:
        search_url = MOBILE_DE_SEARCH_URL

    log.info("Scraping mobile.de ...")
    try:
        # Blokkeer alleen third-party fonts (niet mobile.de resources!)
        # DataDome serveert een tracking pixel — als je die blokkeert word je gedetecteerd
        async def _selective_block(route):
            url = route.request.url
            if "mobile.de" in url or "datadome" in url:
                await route.continue_()
            elif any(url.endswith(ext) for ext in (".woff", ".woff2", ".ttf", ".eot")):
                await route.abort()
            else:
                await route.continue_()

        await page.context.route("**/*.{woff,woff2,ttf,eot}", _selective_block)

        # ── Warm-up: bezoek homepage eerst ──
        # DataDome serveert een JS challenge die een cookie zet.
        # We MOETEN wachten tot die klaar is voordat we naar de zoekpagina gaan.
        log.info("mobile.de: warm-up via homepage ...")
        await human_delay(1, 2)
        try:
            # networkidle wacht tot alle JS klaar is (incl. DataDome challenge)
            await page.goto("https://www.mobile.de", wait_until="networkidle", timeout=45000)
            home_title = await page.title()
            log.info("mobile.de: homepage title = '%s'", home_title)

            # Check of DataDome challenge actief is en wacht tot die opgelost is
            for attempt in range(3):
                cookies = await page.context.cookies()
                dd_cookie = [c for c in cookies if "datadome" in c["name"].lower()]
                if dd_cookie:
                    log.info("mobile.de: DataDome cookie gezet (%d cookies)", len(dd_cookie))
                    break
                log.info("mobile.de: wachten op DataDome challenge (poging %d) ...", attempt + 1)
                await page.wait_for_timeout(3000)

            # Menselijk gedrag: scroll en wacht
            await page.evaluate("window.scrollTo(0, Math.random() * 300)")
            await human_delay(2, 4)
        except Exception as e:
            log.warning("mobile.de: warm-up mislukt: %s", e)

        # ── Navigeer naar zoekresultaten ──
        await human_delay(1, 2)
        await page.goto(search_url, wait_until="networkidle", timeout=90000)

        # Wacht extra voor dynamische content
        await page.wait_for_timeout(3000)

        page_title = await page.title()
        page_url = page.url
        log.info("mobile.de page title: '%s'", page_title)
        log.info("mobile.de page URL: %s", page_url)

        # ── Detecteer DataDome block ──
        if "zugriff verweigert" in page_title.lower() or "access denied" in page_title.lower():
            log.warning("mobile.de: DataDome block, wacht langer en retry ...")
            # Wacht langer — DataDome soms lost zichzelf op na JS execution
            await page.wait_for_timeout(5000)
            page_title = await page.title()
            if "zugriff verweigert" not in page_title.lower():
                log.info("mobile.de: DataDome challenge opgelost na wachten!")
            else:
                # Retry: terug naar homepage, nieuw cookie, opnieuw proberen
                log.info("mobile.de: retry via homepage ...")
                await page.goto("https://www.mobile.de", wait_until="networkidle", timeout=45000)
                await page.wait_for_timeout(random.randint(5000, 8000))
                await page.goto(search_url, wait_until="networkidle", timeout=90000)
                await page.wait_for_timeout(3000)
                page_title = await page.title()
                if "zugriff verweigert" in page_title.lower() or "access denied" in page_title.lower():
                    log.error("mobile.de: IP definitief geblokkeerd (Zugriff verweigert)")
                    return listings
                log.info("mobile.de: retry succesvol! title: '%s'", page_title)

        # ── Debug: sla HTML op ──
        try:
            html = await page.content()
            with open("debug_mobile.html", "w", encoding="utf-8") as f:
                f.write(html[:500_000])
            log.info("mobile.de: HTML opgeslagen (%d bytes)", len(html))
        except Exception:
            pass

        try:
            await page.screenshot(path="debug_mobile.png", timeout=10000)
        except Exception:
            pass

        # ── Consent: probeer te klikken, maar data zit ook achter overlay ──
        for frame in page.frames:
            try:
                btn = frame.locator(
                    "button[title='Zustimmen'], "
                    "button:has-text('Zustimmen'), "
                    "button:has-text('Akzeptieren'), "
                    "button:has-text('Alle akzeptieren')"
                )
                if await btn.count() > 0:
                    await btn.first.click(timeout=3000)
                    log.info("mobile.de: consent geaccepteerd")
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        # ── Wacht op listings (data-testid selectors uit werkende scraper) ──
        try:
            await page.wait_for_selector(
                "a[data-testid*='result'], a[data-testid*='listing']",
                timeout=15000,
            )
        except Exception:
            log.info("mobile.de: wachten op data-testid selectors...")
            await page.wait_for_timeout(2000)

        # ── Zoek listing cards — data-testid eerst (meest betrouwbaar) ──
        count = 0
        cards = None
        for sel in [
            "a[data-testid*='result']",           # Werkende scraper feb 2024
            "[data-testid*='listing-entry']",       # Alternatief
            "a.result-item",                        # Legacy
            ".cBox-body--resultitem",               # Legacy
        ]:
            cards = page.locator(sel)
            count = await cards.count()
            if count > 0:
                log.info("mobile.de: %d cards met '%s'", count, sel)
                break

        if count == 0:
            # Fallback: log body + sla alles op voor debug
            log.warning("mobile.de: 0 listings gevonden")
            try:
                body_text = await page.locator("body").inner_text()
                log.info("mobile.de body (2000 chars): %s", body_text[:2000])
            except Exception:
                pass
            try:
                html = await page.content()
                with open("debug_mobile_final.html", "w", encoding="utf-8") as f:
                    f.write(html[:500_000])
            except Exception:
                pass
            try:
                await page.screenshot(path="debug_mobile_no_results.png", timeout=10000)
            except Exception:
                pass
            return listings

        # ── Parse listing cards ──
        known_streak = 0

        for i in range(min(count, 50)):
            try:
                card = cards.nth(i)

                # Skip gesponsorde advertenties
                try:
                    card_full = await card.inner_text()
                    if "gesponsert" in card_full.lower()[:50]:
                        log.info("Gesponsorde advertentie overgeslagen")
                        continue
                except Exception:
                    pass

                # Title — h2 is de standaard (feb 2024 scraper), fallback naar spans
                title = ""
                for title_sel in ["h2", "span.h3", ".u-text-break-word"]:
                    title_el = card.locator(title_sel)
                    if await title_el.count() > 0:
                        title = (await title_el.first.inner_text()).strip()
                        if title:
                            break
                if not title:
                    try:
                        card_text = await card.inner_text()
                        lines = [l.strip() for l in card_text.split("\n") if l.strip()]
                        title = lines[0] if lines else ""
                    except Exception:
                        continue

                # Strip "Gesponsert" prefix uit titel
                title = re.sub(r"^Gesponsert\s*", "", title, flags=re.IGNORECASE)

                if not title or "q3" not in title.lower():
                    continue

                # URL — card is vaak zelf een <a> tag
                href = await card.get_attribute("href") or ""
                if not href:
                    link_el = card.locator("a[href*='/fahrzeuge/']")
                    if await link_el.count() > 0:
                        href = await link_el.first.get_attribute("href") or ""
                if href and not href.startswith("http"):
                    href = "https://suchen.mobile.de" + href

                listing_id = extract_listing_id(href, "mobile", fallback=str(i))

                if listing_exists(conn, listing_id):
                    known_streak += 1
                    log.info("Bekende listing: %s", title[:50])
                    if known_streak >= 5:
                        log.info("5 bekende op rij — stoppen")
                        break
                    continue
                known_streak = 0

                # Price — data-testid="price-label" (feb 2024 scraper)
                price = 0
                for price_sel in [
                    "span[data-testid*='price-label']",
                    "span[data-testid*='price']",
                    ".price-block span",
                    ".h3.u-block",
                ]:
                    price_el = card.locator(price_sel)
                    if await price_el.count() > 0:
                        price = parse_price(await price_el.first.inner_text())
                        if price:
                            break

                # Details — km, jaar, pk
                details_text = ""
                for det_sel in [".rbt-regMilPow", "[data-testid*='regMilPow']", ".vehicle-data"]:
                    det_el = card.locator(det_sel)
                    if await det_el.count() > 0:
                        details_text = await det_el.first.inner_text()
                        break

                year = parse_year(details_text)
                km = parse_km(details_text)

                # Fallback: parse uit volledige card text
                if not year or not km:
                    try:
                        card_text = await card.inner_text()
                        if not year:
                            year = parse_year(card_text)
                        if not km:
                            km = parse_km(card_text)
                    except Exception:
                        pass

                listing = Listing(
                    id=listing_id, source="mobile.de", title=title,
                    price=price, year=year, km=km, url=href or search_url,
                )

                # Detail pagina voor features (alleen nieuwe listings)
                if href:
                    try:
                        dp = await page.context.new_page()
                        await human_delay(1, 2)
                        await dp.goto(href, wait_until="domcontentloaded", timeout=30000)
                        await dp.wait_for_timeout(1500)

                        # --- Klik "Show all" / "Show more" buttons om alles te tonen ---
                        expand_selectors = [
                            # Vehicle Description "Show all" button
                            "button:has-text('Show all')",
                            "a:has-text('Show all')",
                            "[data-testid='showAll']",
                            "[class*='description'] button",
                            "[class*='Description'] button",
                            ".cBox--vehicleDescription button",
                            "#description button",
                            # Features "Show more" button
                            "button:has-text('Show more')",
                            "a:has-text('Show more')",
                            "[data-testid='showMore']",
                            "[class*='feature'] button",
                            "[class*='Feature'] button",
                            ".cBox--features button",
                            "#features button",
                            # Duitse varianten
                            "button:has-text('Alle anzeigen')",
                            "a:has-text('Alle anzeigen')",
                            "button:has-text('Mehr anzeigen')",
                            "a:has-text('Mehr anzeigen')",
                            "button:has-text('Alles anzeigen')",
                            "a:has-text('Alles anzeigen')",
                            # Generieke expand buttons
                            "button:has-text('meer')",
                            "button:has-text('more')",
                            "button:has-text('Mehr')",
                        ]
                        for expand_sel in expand_selectors:
                            try:
                                buttons = dp.locator(expand_sel)
                                count = await buttons.count()
                                for bi in range(count):
                                    try:
                                        btn = buttons.nth(bi)
                                        if await btn.is_visible():
                                            await btn.click()
                                            log.info("Expand button geklikt: %s [%d]", expand_sel, bi)
                                            await dp.wait_for_timeout(500)
                                    except Exception:
                                        pass
                            except Exception:
                                pass

                        # Wacht even tot content geladen is na klikken
                        await dp.wait_for_timeout(1500)

                        detail_text = ""

                        # 1. Titel/kopje van de pagina
                        for title_sel in [
                            "h1", "#rbt-ad-title",
                            "[data-testid='ad-title']",
                            "[class*='StageTitle']",
                            ".cBox-title--bold",
                        ]:
                            try:
                                tel = dp.locator(title_sel)
                                if await tel.count() > 0:
                                    detail_text += " " + await tel.first.inner_text()
                                    break
                            except Exception:
                                pass

                        # 2. Technical Data sectie
                        for td_sel in [
                            "div.cBox-body.cBox-body--technical-data",
                            ".cBox--technicalData",
                            "#rbt-td",
                            "[data-testid='ad-detail-technical-data']",
                            "[class*='TechnicalData']",
                            ".technical-data",
                        ]:
                            try:
                                el = dp.locator(td_sel)
                                if await el.count() > 0:
                                    detail_text += " " + await el.first.inner_text()
                            except Exception:
                                pass

                        # 3. Features / Ausstattung sectie
                        for feat_sel in [
                            ".cBox--features", "#features",
                            "[data-testid='ad-detail-features']",
                            "[data-testid='ad-detail-equipment']",
                            "[data-testid='equipment']",
                            "[class*='FeatureList']",
                            "[class*='equipment']",
                            "[class*='feature']",
                            "[class*='Equipment']",
                            "[class*='Feature']",
                        ]:
                            try:
                                el = dp.locator(feat_sel)
                                if await el.count() > 0:
                                    detail_text += " " + await el.first.inner_text()
                            except Exception:
                                pass

                        # 4. Vehicle Description sectie
                        for desc_sel in [
                            "#description", ".cBox--vehicleDescription",
                            "[data-testid='ad-detail-description']",
                            "[class*='VehicleDescription']",
                            "[class*='vehicleDescription']",
                            "[class*='Description']",
                            ".vehicle-description",
                        ]:
                            try:
                                el = dp.locator(desc_sel)
                                if await el.count() > 0:
                                    detail_text += " " + await el.first.inner_text()
                            except Exception:
                                pass

                        # 5. Overige relevante secties
                        for extra_sel in [
                            ".vehicle-details",
                            ".listing-details",
                        ]:
                            try:
                                el = dp.locator(extra_sel)
                                if await el.count() > 0:
                                    detail_text += " " + await el.first.inner_text()
                            except Exception:
                                pass

                        # FALLBACK: als specifieke selectors weinig opleverden,
                        # pak de volledige zichtbare tekst van de pagina
                        if len(detail_text.strip()) < 100:
                            try:
                                body_text = await dp.locator("body").inner_text()
                                detail_text = body_text[:15000]
                                log.info("Detail fallback (body text): %d chars", len(detail_text))
                            except Exception:
                                pass

                        if detail_text:
                            listing.description = detail_text
                            log.info(
                                "Detail tekst voor %s: %d chars — preview: %s",
                                listing_id, len(detail_text),
                                detail_text[:200].replace("\n", " "),
                            )
                        else:
                            log.warning("Geen detail tekst gevonden voor %s", listing_id)

                        await dp.close()
                    except Exception as e:
                        log.warning("Detail fout: %s", e)

                log.info("MATCH: %s — €%s — %d km — %d", title[:50], f"{price:,}" if price else "?", km, year)
                listings.append(listing)
            except Exception as e:
                log.warning("mobile.de card %d: %s", i, e)
                continue

    except Exception as e:
        log.error("mobile.de scraping mislukt: %s", e)

    return listings


async def scrape_autoscout24(page, conn, base_url: str | None = None) -> list[Listing]:
    """Scrape AutoScout24 for Audi Q3 hybrid listings in Duitsland.
    Gesorteerd op nieuwste eerst — scraped meerdere pagina's."""
    listings = []
    # Audi Q3 hybrid, Germany, year >= 2021, km <= 80000, price <= 40000
    # fuel=H = hybrid filter, sort=age = nieuwste eerst
    if base_url is None:
        base_url = (
            "https://www.autoscout24.de/lst/audi/q3-sportback"
            "?atype=C&cy=D&desc=0&fregfrom=2020&fuel=H"
            "&kmto=80000&priceto=40000&sort=age&ustate=N%2CU"
        )
    MAX_PAGES = 5  # Meer pagina's nu alles hybrids zijn
    consent_done = False

    model_label = "Q3 Sportback" if "q3-sportback" in base_url else "Q3"
    log.info("Scraping AutoScout24 [%s] ...", model_label)
    try:
      for page_num in range(1, MAX_PAGES + 1):
        search_url = base_url if page_num == 1 else f"{base_url}&page={page_num}"
        log.info("AutoScout24 [%s] pagina %d ...", model_label, page_num)

        await human_delay(1, 3)
        await page.goto(search_url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2000)

        log.info("AutoScout24 page title: %s", await page.title())
        log.info("AutoScout24 page URL: %s", page.url)
        if page_num == 1:
            await page.screenshot(path="debug_autoscout.png", timeout=15000)

            # Sla HTML op voor debug
            html_content = await page.content()
            try:
                with open("debug_autoscout.html", "w", encoding="utf-8") as f:
                    f.write(html_content[:500_000])
            except Exception:
                pass

        # Accept cookies (OneTrust) — alleen eerste keer
        if not consent_done:
            try:
                consent = page.locator("#onetrust-accept-btn-handler, button:has-text('Einverstanden'), button:has-text('Akzeptieren'), button:has-text('Alle akzeptieren'), button:has-text('Accept All')")
                await consent.first.wait_for(timeout=5000)
                await human_delay(0.5, 1.5)
                await consent.first.click()
                log.info("AutoScout24: consent geaccepteerd")
                await human_delay(2, 3)
                consent_done = True
            except Exception:
                log.info("AutoScout24: geen consent popup gevonden")
                consent_done = True

        # ── METHODE 1: Parse __NEXT_DATA__ JSON (AutoScout24 is Next.js) ──
        next_data = await page.evaluate("""() => {
            const el = document.getElementById('__NEXT_DATA__');
            if (el) { try { return JSON.parse(el.textContent); } catch(e) {} }
            return null;
        }""")

        if next_data:
            log.info("AutoScout24: __NEXT_DATA__ gevonden!")
            try:
                with open("debug_autoscout_nextdata.json", "w") as f:
                    json.dump(next_data, f, indent=2, default=str)
            except Exception:
                pass

            page_props = next_data.get("props", {}).get("pageProps", {})
            log.info("AutoScout24 pageProps keys: %s", list(page_props.keys()))

            # Zoek listings in JSON — meerdere mogelijke paden
            listings_json = []
            for key_path in [
                lambda pp: pp.get("listings", []),
                lambda pp: pp.get("listingSearchResult", {}).get("listings", []),
                lambda pp: pp.get("searchResult", {}).get("listings", []),
                lambda pp: pp.get("data", {}).get("listings", []),
            ]:
                try:
                    result = key_path(page_props)
                    if isinstance(result, list) and len(result) > 0:
                        listings_json = result
                        break
                except Exception:
                    continue

            log.info("AutoScout24: %d listings in JSON gevonden", len(listings_json))

            known_streak = 0
            for item in listings_json:
                try:
                    raw_id = str(item.get("id", item.get("listingId", "")))
                    if not raw_id:
                        continue
                    listing_id = f"as24_{raw_id}"

                    # Title
                    title = item.get("heading", "") or item.get("title", "")
                    if not title:
                        t = item.get("tracking", {})
                        title = t.get("title", "")
                    if not title:
                        v = item.get("vehicle", {})
                        title = f"{v.get('make', '')} {v.get('model', '')} {v.get('modelVersionInput', '')}".strip()

                    if not title or "q3" not in title.lower():
                        continue

                    if listing_exists(conn, listing_id):
                        known_streak += 1
                        log.info("Bekende listing, skip: %s", title[:50])
                        if known_streak >= 5:
                            log.info("5 bekende op rij — stoppen")
                            break
                        continue
                    known_streak = 0

                    # Price
                    price = 0
                    pd = item.get("price", item.get("prices", {}))
                    if isinstance(pd, dict):
                        price = pd.get("amount", pd.get("price", pd.get("public", {}).get("priceRaw", 0)))
                    elif isinstance(pd, (int, float)):
                        price = int(pd)
                    if not price:
                        price = item.get("tracking", {}).get("price", 0)
                    price = int(price) if price else 0

                    # Mileage
                    km = 0
                    tracking = item.get("tracking", {})
                    km_v = tracking.get("mileage", 0)
                    if km_v:
                        km = int(str(km_v).replace(".", "").replace(",", ""))
                    if not km:
                        v = item.get("vehicle", {})
                        km = v.get("mileage", v.get("mileageInKmNumeric", 0))
                    km = int(km) if km else 0

                    # Year
                    year = 0
                    reg = tracking.get("first_registration", "")
                    if not reg:
                        v = item.get("vehicle", {})
                        reg = v.get("firstRegistrationDate", v.get("firstRegistration", ""))
                    year = parse_year(str(reg)) if reg else 0

                    # URL
                    url_path = item.get("url", "")
                    if url_path and not url_path.startswith("http"):
                        url = f"https://www.autoscout24.de{url_path}"
                    else:
                        url = url_path or search_url

                    if price and price > SEARCH_CRITERIA["price_max"]:
                        continue
                    if km and km > SEARCH_CRITERIA["km_max"]:
                        continue
                    if year and year < SEARCH_CRITERIA["year_min"]:
                        continue

                    # Fuel type (apart ophalen voor hybrid check)
                    fuel_type_str = ""
                    fuel = item.get("vehicle", {}).get("fuelType", {})
                    if isinstance(fuel, dict):
                        fuel_type_str = fuel.get("translated", fuel.get("label", ""))
                    fuel_t = tracking.get("fuel_type", "")
                    if fuel_t:
                        fuel_type_str = f"{fuel_type_str} {fuel_t}".strip()

                    # Hybrid check VOOR detail pagina (bespaar requests voor niet-hybrids)
                    # Description uit JSON (kort, van zoekpagina)
                    desc_parts = []
                    for dk in ["description", "subtitle"]:
                        val = item.get(dk, "")
                        if val and isinstance(val, str):
                            desc_parts.append(val)
                    for ek in ["equipment", "features", "equipments"]:
                        eq = item.get(ek, [])
                        if isinstance(eq, list):
                            desc_parts.extend(str(e) for e in eq)
                        elif isinstance(eq, dict):
                            for sub in eq.values():
                                if isinstance(sub, list):
                                    desc_parts.extend(str(e) for e in sub)

                    short_desc = " ".join(filter(None, desc_parts))

                    if not is_q3_hybrid(title, short_desc, fuel_type_str):
                        log.info("Overgeslagen (geen Q3 hybrid): %s", title[:60])
                        continue

                    listing = Listing(
                        id=listing_id, source="AutoScout24", title=title,
                        price=price, year=year, km=km, url=url,
                        description=short_desc,
                    )

                    # ALTIJD detail pagina scrapen voor volledige equipment lijst
                    # (pano, keyless etc. staan bijna nooit in zoekresultaat JSON)
                    if url != search_url:
                        try:
                            dp = await page.context.new_page()
                            await human_delay(1, 2)
                            await dp.goto(url, wait_until="domcontentloaded", timeout=30000)
                            await human_delay(1, 2)
                            dd = await dp.evaluate("""() => {
                                const el = document.getElementById('__NEXT_DATA__');
                                if (el) { try { return JSON.parse(el.textContent); } catch(e) {} }
                                return null;
                            }""")
                            if dd:
                                dpp = dd.get("props", {}).get("pageProps", {})
                                for dk in ["listingDetails", "listing", "data"]:
                                    ld = dpp.get(dk, {})
                                    if isinstance(ld, dict):
                                        desc = ld.get("description", "")
                                        if desc:
                                            listing.description += " " + desc
                                        # Equipment/features lijst — vaak geneste structuur
                                        for fk in ["equipment", "features", "equipments", "vehicleDetails"]:
                                            feats = ld.get(fk, [])
                                            if isinstance(feats, list):
                                                listing.description += " " + " ".join(str(f) for f in feats)
                                            elif isinstance(feats, dict):
                                                for sub in feats.values():
                                                    if isinstance(sub, list):
                                                        listing.description += " " + " ".join(str(f) for f in sub)
                                                    elif isinstance(sub, str):
                                                        listing.description += " " + sub

                            # Fallback: pak ook zichtbare tekst van equipmentlijst
                            if not dd or len(listing.description) < 100:
                                for sel in [
                                    "[data-testid='collapsible-section']",
                                    ".VehicleOverview_itemContainer__XSLWi",
                                    ".DetailsSection_content__bZUWQ",
                                    "[class*='equipment']",
                                    "[class*='Equipment']",
                                ]:
                                    el = dp.locator(sel)
                                    if await el.count() > 0:
                                        eq_text = await el.all_inner_texts()
                                        listing.description += " " + " ".join(eq_text)
                                        break

                            await dp.close()
                        except Exception as e:
                            log.warning("AS24 detail fout: %s", e)

                    log.info("Detail beschrijving lengte: %d chars", len(listing.description))

                    if not is_q3_hybrid(listing.title, listing.description, fuel_type_str):
                        log.info("Overgeslagen na detail check (geen hybrid): %s", title[:60])
                        continue

                    log.info("MATCH: %s — €%s — %d km — %d", title[:60], f"{price:,}" if price else "?", km, year)
                    listings.append(listing)
                except Exception as e:
                    log.warning("AS24 JSON item fout: %s", e)

            if listings_json:
                # Check of er meer pagina's zijn
                num_pages = page_props.get("numberOfPages", 1)
                log.info("AutoScout24: pagina %d/%d verwerkt, %d hybrids tot nu toe",
                         page_num, num_pages, len(listings))
                if page_num >= num_pages:
                    break
                continue  # Ga naar volgende pagina

        # ── METHODE 2: CSS selector fallback ──
        log.info("AutoScout24: fallback naar CSS selectors ...")
        await page.wait_for_timeout(3000)
        await page.screenshot(path="debug_autoscout_fallback.png", timeout=15000)

        for sel in ["article[data-testid]", ".list-page-item", ".cl-list-element", "article", "[data-testid='listing-entry']"]:
            cards = page.locator(sel)
            count = await cards.count()
            if count > 0:
                log.info("AutoScout24 CSS: %d cards met '%s'", count, sel)
                break
        else:
            count = 0

        if count == 0:
            body_text = await page.locator("body").inner_text()
            log.info("AutoScout24 body (eerste 2000): %s", body_text[:2000])
            break  # Geen CSS cards, stop paginatie

        known_streak = 0
        for i in range(min(count, 50)):
            try:
                card = cards.nth(i)
                title = ""
                for ts in ["a h2", "h2", "a span", "a"]:
                    te = card.locator(ts)
                    if await te.count() > 0:
                        title = (await te.first.inner_text()).strip()
                        if title:
                            break
                if not title or "q3" not in title.lower():
                    continue

                link_el = card.locator("a[href*='/angebot/'], a[href*='/aanbod/'], a[href*='/offers/'], a[href*='/listing/']")
                href = ""
                if await link_el.count() > 0:
                    href = await link_el.first.get_attribute("href")
                    if href and not href.startswith("http"):
                        href = "https://www.autoscout24.de" + href

                listing_id = extract_listing_id(href, "as24", fallback=str(i))

                if listing_exists(conn, listing_id):
                    known_streak += 1
                    if known_streak >= 5:
                        break
                    continue
                known_streak = 0

                card_text = await card.inner_text()
                price = parse_price(card_text.split("€")[1].split("\n")[0]) if "€" in card_text else 0
                year = parse_year(card_text)
                km = parse_km(card_text)

                if price and price > SEARCH_CRITERIA["price_max"]:
                    continue
                if km and km > SEARCH_CRITERIA["km_max"]:
                    continue
                if year and year < SEARCH_CRITERIA["year_min"]:
                    continue

                listing = Listing(
                    id=listing_id, source="AutoScout24", title=title,
                    price=price, year=year, km=km, url=href or search_url,
                )

                if href:
                    try:
                        dp = await page.context.new_page()
                        await human_delay(1, 2)
                        await dp.goto(href, wait_until="domcontentloaded", timeout=30000)
                        await human_delay(1, 2)
                        for sel2 in ["[data-testid='description']", ".vehicle-description", "#description"]:
                            el = dp.locator(sel2)
                            if await el.count() > 0:
                                listing.description = await el.first.inner_text()
                                break
                        for sel2 in ["[data-testid='equipments']", ".equipment-list", "#equipment"]:
                            el = dp.locator(sel2)
                            if await el.count() > 0:
                                listing.description += " " + await el.first.inner_text()
                                break
                        await dp.close()
                    except Exception as e:
                        log.warning("Detail fout: %s", e)

                if not is_q3_hybrid(listing.title, listing.description, ""):
                    continue
                listings.append(listing)
            except Exception as e:
                log.warning("AS24 CSS card %d: %s", i, e)

        break  # CSS fallback paginatie niet ondersteund

    except Exception as e:
        log.error("AutoScout24 scraping mislukt: %s", e, exc_info=True)

    return listings


# ── Audi Gebrauchtwagenboerse scraper ──────────────────────────────────────

# audi-boerse.de legacy platform (server-rendered HTML via controller.htm)
# s|10 = merk Audi, l|50 = 50 resultaten, PRICE_SALE,U = sorteer op prijs oplopend
AUDI_BOERSE_SEARCH_URL = (
    "https://www.audi-boerse.de/gebrauchtwagen/i/s|10/l|50,1,PRICE_SALE,U/controller.htm"
)


def scrape_audi_boerse(conn) -> list[Listing]:
    """Scrape Audi Gebrauchtwagenboerse (audi-boerse.de) via Scrape.do.

    Zoekt op alle Audi modellen en filtert op Q3 45 TFSI e hybrid.
    Detail pagina's worden opgehaald voor feature-detectie.
    """
    listings = []

    if not SCRAPERAPI_KEY and not SCRAPE_DO_TOKEN and not PROXY_URLS:
        log.info("Audi Börse: geen scraping methode beschikbaar, overslaan")
        return listings

    log.info("━━━ Audi Gebrauchtwagenboerse ━━━")
    log.info("Audi Börse: zoekpagina ophalen ...")
    html = scrape_do_fetch(AUDI_BOERSE_SEARCH_URL)

    if not html:
        log.error("Audi Börse: geen HTML ontvangen")
        return listings

    # Debug HTML opslaan
    try:
        with open("debug_audi_boerse.html", "w", encoding="utf-8") as f:
            f.write(html[:500_000])
    except Exception:
        pass

    if "zugriff verweigert" in html.lower() or "access denied" in html.lower():
        log.error("Audi Börse: geblokkeerd (Zugriff verweigert)")
        return listings

    soup = BeautifulSoup(html, "html.parser")

    # Probeer meerdere selectors voor auto-cards
    cards = []
    used_selector = ""
    for sel in [
        ".vehicle-card", ".listing-entry", ".result-list-entry",
        ".car-item", "article.result", "[data-vehicle-id]",
        ".sc-list-item", ".search-result-item", ".offer-item",
        ".fahrzeug", ".car-result", ".result-item",
        "article", ".card",
    ]:
        cards = soup.select(sel)
        if cards:
            used_selector = sel
            log.info("Audi Börse: %d cards gevonden met selector '%s'", len(cards), sel)
            break

    if not cards:
        # Fallback: zoek alle links naar detail-pagina's
        all_links = soup.select("a[href*='/gebrauchtwagen/']")
        log.warning(
            "Audi Börse: geen bekende card-selectors gevonden. "
            "Links met /gebrauchtwagen/: %d. HTML preview: %.500s",
            len(all_links),
            soup.get_text(separator=" ", strip=True)[:500],
        )
        return listings

    for i, card in enumerate(cards):
        try:
            card_text = card.get_text(separator=" ", strip=True)

            # Filter: alleen Q3
            if "q3" not in card_text.lower():
                continue

            # Filter: alleen hybrid
            if not is_q3_hybrid(card_text):
                continue

            # Title
            title = ""
            for title_sel in ["h2", "h3", ".title", ".heading", "[class*='title']", "a"]:
                title_el = card.select_one(title_sel)
                if title_el and len(title_el.get_text(strip=True)) > 5:
                    title = title_el.get_text(strip=True)
                    break
            if not title:
                title = card_text[:80]

            # Price
            price = 0
            for price_sel in ["[class*='price']", ".price", "[data-price]"]:
                price_el = card.select_one(price_sel)
                if price_el:
                    price = parse_price(price_el.get_text())
                    if price:
                        break
            if not price:
                price_match = re.search(r"([\d.]+)\s*€|€\s*([\d.]+)", card_text)
                if price_match:
                    price = parse_price(price_match.group(1) or price_match.group(2))

            # KM
            km = parse_km(card_text)

            # Year
            year = parse_year(card_text)

            # URL
            link = card.select_one("a[href]")
            href = link["href"] if link else ""
            if href and not href.startswith("http"):
                href = f"https://www.audi-boerse.de{href}"

            # Listing ID
            listing_id = extract_listing_id(href, "audi", fallback=f"card_{i}")
            if not listing_id:
                continue

            # Criteria check
            if price and price > SEARCH_CRITERIA["price_max"]:
                continue
            if km and km > SEARCH_CRITERIA["km_max"]:
                continue
            if year and year < SEARCH_CRITERIA["year_min"]:
                continue

            # Skip bekende listings
            if listing_exists(conn, listing_id):
                log.info("Audi Börse: bekende listing, skip: %s", title[:50])
                continue

            listing = Listing(
                id=listing_id,
                source="Audi Börse",
                title=title,
                price=price,
                year=year,
                km=km,
                url=href,
                description=card_text[:500],
            )

            # Detail pagina ophalen voor features (bandwidth-bewust)
            if href and _detail_page_count < MAX_DETAIL_PAGES_PER_RUN:
                _detail_page_count += 1
                log.info("Audi Börse: detail ophalen [%d/%d] ...", _detail_page_count, MAX_DETAIL_PAGES_PER_RUN)
                time.sleep(random.uniform(0.3, 0.8))
                detail_html = scrape_do_fetch(href)
                if detail_html:
                    detail_soup = BeautifulSoup(detail_html, "html.parser")
                    detail_text = detail_soup.get_text(separator=" ", strip=True)
                    listing.description = detail_text[:3000]
                    if not listing.color:
                        listing.color = parse_color(detail_text)

            log.info(
                "MATCH: %s — €%s — %d km — %d",
                title[:50], f"{price:,}" if price else "?", km, year,
            )
            listings.append(listing)

        except Exception as e:
            log.warning("Audi Börse card %d: %s", i, e)
            continue

    log.info("Audi Börse: %d Q3 hybrid listings gevonden", len(listings))
    return listings


# ── Main ────────────────────────────────────────────────────────────────────


async def main():
    # ── Quiet hours: niet scrapen tussen 20:00 en 08:00 CET ──
    # Handmatige GitHub Actions trigger (workflow_dispatch) slaat quiet hours over
    force = "--force" in sys.argv or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not force:
        now_cet = datetime.now(ZoneInfo("Europe/Amsterdam"))
        hour = now_cet.hour
        if hour >= 20 or hour < 8:
            log.info("Quiet hours (%02d:%02d CET) — overslaan (actief 08:00-20:00)", hour, now_cet.minute)
            return
    else:
        log.info("--force: quiet hours overgeslagen")

    log.info("=== Auto-Alert Scraper gestart ===")
    log.info(
        "Zoekcriteria: %s (%s), max €%s, land: %s",
        SEARCH_CRITERIA["model"],
        SEARCH_CRITERIA["fuel"],
        f"{SEARCH_CRITERIA['price_max']:,}",
        SEARCH_CRITERIA["country"],
    )
    log.info(
        "  Filters: %d+, max %d km, max €%s",
        SEARCH_CRITERIA["year_min"],
        SEARCH_CRITERIA["km_max"],
        f"{SEARCH_CRITERIA['price_max']:,}",
    )

    conn = init_db()

    all_listings: list[Listing] = []
    seen_ids: set[str] = set()  # voorkom dubbele listings tussen URLs

    # Panoramadak patterns voor beschrijving-check (URL 2)
    pano_patterns = FEATURE_PATTERNS["panoramadak"]

    def has_pano_in_text(text: str) -> bool:
        text_lower = text.lower()
        return any(re.search(p, text_lower, re.IGNORECASE) for p in pano_patterns)

    if not SCRAPERAPI_KEY and not SCRAPE_DO_TOKEN and not PROXY_URLS:
        log.error("Geen scraping methode geconfigureerd (SCRAPERAPI_KEY, SCRAPE_DO_TOKEN, of PROXY_URL)")
        return
    log.info("Scraping: ScraperAPI=%s, Scrape.do=%s, Proxy=%s (%d URLs)",
             bool(SCRAPERAPI_KEY), bool(SCRAPE_DO_TOKEN), bool(PROXY_URLS), len(PROXY_URLS))

    # ── Bepaal scraping methode ──
    # Camoufox (Firefox anti-detect) > Patchright > ScraperAPI/Scrape.do > curl_cffi
    use_browser = (HAS_CAMOUFOX or HAS_PLAYWRIGHT) and PROXY_URLS
    if use_browser:
        log.info("Methode: %s + residential proxy (DataDome bypass)", PLAYWRIGHT_ENGINE)
    elif SCRAPERAPI_KEY or SCRAPE_DO_TOKEN:
        log.info("Methode: ScraperAPI/Scrape.do API")
    else:
        log.info("Methode: curl_cffi + proxy (kan falen bij DataDome)")

    # ── Browser starten (indien beschikbaar) ──
    pw_browser = None
    pw_page = None
    _camoufox_ctx = None  # voor context manager cleanup
    if use_browser:
        proxy_url = get_random_proxy()
        proxy_conf = parse_proxy_url(proxy_url) if proxy_url else None
        proxy_label = proxy_url.split("@")[-1] if proxy_url and "@" in proxy_url else ""

        if HAS_CAMOUFOX:
            # Camoufox: Firefox met C++ level fingerprint spoofing
            # headless='virtual' gebruikt Xvfb op Linux (GitHub Actions)
            # geoip=True matcht timezone/locale automatisch met proxy locatie
            try:
                _camoufox_ctx = AsyncCamoufox(
                    headless="virtual",
                    proxy=proxy_conf,
                    geoip=True,
                    humanize=True,
                )
                pw_browser = await _camoufox_ctx.__aenter__()
                pw_page = await pw_browser.new_page()
                log.info("Camoufox (Firefox anti-detect) gestart met proxy %s", proxy_label)
            except Exception as e:
                log.error("Camoufox starten mislukt: %s — probeer fallback", e)
                _camoufox_ctx = None
                use_browser = False

        if not pw_page and HAS_PLAYWRIGHT:
            # Fallback: Patchright/Playwright
            try:
                pw = await async_playwright().start()
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ]
                pw_browser = await pw.chromium.launch(
                    headless=True,
                    proxy=proxy_conf,
                    args=launch_args,
                )
                pw_context = await pw_browser.new_context(
                    locale="de-DE",
                    timezone_id="Europe/Berlin",
                )
                pw_page = await pw_context.new_page()
                log.info("%s browser gestart met proxy %s", PLAYWRIGHT_ENGINE, proxy_label)
                use_browser = True
            except Exception as e:
                log.error("Playwright starten mislukt: %s — fallback naar API", e)
                use_browser = False

    # ── Loop door alle zoek-URLs ──
    for search_cfg in MOBILE_DE_SEARCH_URLS:
        search_url = search_cfg["url"]
        url_label = search_cfg["label"]
        require_pano = search_cfg["require_pano_in_desc"]

        log.info("━━━ %s ━━━", url_label)

        if use_browser and pw_page:
            mobile_listings = await scrape_mobile_de(pw_page, conn, search_url=search_url)
        else:
            mobile_listings = scrape_mobile_de_scrapedo(conn, search_url=search_url)

        if mobile_listings:
            log.info("[%s] %d listings gevonden", url_label, len(mobile_listings))
        else:
            log.warning("[%s] 0 listings", url_label)

        # Filter: pano-check op beschrijving als nodig + dedup tussen URLs
        for lst in mobile_listings:
            if lst.id in seen_ids:
                log.info("[%s] Overgeslagen (al in andere URL): %s", url_label, lst.title[:40])
                continue
            if require_pano and not has_pano_in_text(f"{lst.title} {lst.description}"):
                log.info("[%s] Overgeslagen (geen pano in beschrijving): %s", url_label, lst.title[:40])
                continue
            seen_ids.add(lst.id)
            all_listings.append(lst)

        log.info("[%s] %d listings toegevoegd", url_label, len(mobile_listings))

    log.info("Totaal: %d listings, nu scoren ...", len(all_listings))

    new_count = 0
    alert_count = 0

    for listing in all_listings:
        listing = score_listing(listing)
        is_new = not listing_exists(conn, listing.id)
        save_listing(conn, listing)

        if is_new:
            new_count += 1
            send_telegram(listing)
            alert_count += 1
            log.info(
                "ALERT: %s — score %d/%d — €%s — %s",
                listing.title,
                listing.score,
                len(FULL_OPTION_FEATURES),
                f"{listing.price:,}" if listing.price else "?",
                listing.url,
            )
        else:
            log.info("Bekende listing bijgewerkt: %s", listing.id)

    conn.close()

    # Browser sluiten
    if _camoufox_ctx:
        try:
            await _camoufox_ctx.__aexit__(None, None, None)
        except Exception:
            pass
    elif pw_browser:
        try:
            await pw_browser.close()
        except Exception:
            pass

    log.info(
        "=== Klaar: %d totaal, %d nieuw, %d alerts verstuurd ===",
        len(all_listings),
        new_count,
        alert_count,
    )

    log.info("Scan samenvatting niet verstuurd (uitgeschakeld)")


if __name__ == "__main__":
    asyncio.run(main())
