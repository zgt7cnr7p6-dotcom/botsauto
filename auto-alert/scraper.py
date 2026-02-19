#!/usr/bin/env python3
"""
Auto-Alert Scraper
Scraped mobile.de en AutoScout24 voor Audi Q3 45 TFSI e (hybrid) in Duitsland.
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
from urllib.parse import urlparse, quote

import requests as req_lib
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# curl_cffi: HTTP client die Chrome's TLS-vingerafdruk nabootst
# Hierdoor ziet mobile.de het als een echte browser (op TLS niveau)
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
    "year_min": 2020,          # vanaf 2020
    "km_max": 80_000,          # max 80k km
    "price_max": 40_000,       # max €40.000
    "country": "DE",
}

# mobile.de zoek-URL — exact zoals de user zoekt
# ms=1900;37;;pano = Audi Q3 met "pano" in tekst, ft=HYBRID, fr=2020+, ml=max80k, p=max40k
# sb=doc = sorteer op nieuwste eerst (beter voor monitoring dan sb=rel)
MOBILE_DE_SEARCH_URL = (
    "https://suchen.mobile.de/fahrzeuge/search.html?"
    "dam=false&fr=2020%3A&ft=HYBRID&isSearchRequest=true"
    "&ml=%3A80000&ms=1900%3B37%3B%3Bpano&od=down"
    "&p=%3A40000&ref=srp&s=Car&sb=doc&vc=Car"
)

# Must-have: advertentie wordt alleen gemeld als minstens enkele hiervan matchen
MUST_HAVE_FEATURES = [
    "keyless",
    "panoramadak",
    "audio_premium",
    "matrix_led",
    "s_line",
    "camera",
]

# HARDE EISEN: zonder AL deze features wordt GEEN alert verstuurd
REQUIRED_FEATURES = ["panoramadak", "camera", "keyless"]

# Nice-to-have: bonuspunten, maar niet vereist
NICE_TO_HAVE_FEATURES = [
    "stoelverwarming",
    "elektrische_stoelen",
    "adaptief_onderstel",
]

# Leesbare namen voor Telegram (DE/NL mix zodat je het herkent)
FEATURE_DISPLAY_NAMES = {
    "keyless": "Keyless Entry",
    "panoramadak": "Panoramadak",
    "audio_premium": "Premium Audio",
    "matrix_led": "Matrix LED",
    "s_line": "S-Line",
    "camera": "Camera (achteruit/360)",
    "stoelverwarming": "Stoelverwarming",
    "elektrische_stoelen": "Elektrische stoelen",
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


async def human_delay(min_s=1.5, max_s=4.0):
    """Wacht een willekeurige tijd om menselijk gedrag te simuleren."""
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
    score: int = 0
    features: list = field(default_factory=list)
    must_have_count: int = 0
    nice_to_have_count: int = 0


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
    # ── Must-have ──
    "keyless": [
        r"keyless",
        r"komfort\s*schl[üu]ssel",
        r"komfortschl[üu]ssel",
        r"schl[üu]ssel\s*los",
        r"convenience\s*key",
        r"sleutel\s*loos",
        r"kessy",
    ],
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
    "audio_premium": [
        r"bang[\s&+]*olufsen",
        r"b[\s&+]*o\b",
        r"b&o",
        r"sonos",
        r"premium\s*sound",
        r"audi\s*sound\s*system",
        r"soundsystem",
        r"sound\s*system",
    ],
    "matrix_led": [
        r"matrix[\s-]*led",
        r"matrix[\s-]*licht",
        r"matrix[\s-]*scheinwerfer",
        r"led[\s-]*matrix",
        r"matrixbeam",
    ],
    "s_line": [
        r"s[\s-]?line",
        r"s-line",
        r"sline",
    ],
    "camera": [
        r"r[üu]ckfahr\s*kamera",
        r"r[üu]ckfahrkamera",
        r"achteruitrij\s*camera",
        r"rear\s*view\s*camera",
        r"backup\s*camera",
        r"reversing\s*camera",
        r"360[\s°]*camera",
        r"360[\s°]?grad[\s-]*kamera",
        r"rundum[\s-]*kamera",
        r"surround\s*view",
        r"umgebungs\s*kamera",
        r"umgebungskamera",
    ],
    # ── Nice-to-have ──
    "stoelverwarming": [
        r"stoel\s*verwarming",
        r"sitz\s*heizung",
        r"sitzheizung",
        r"verwarmde?\s*stoel",
        r"beheizbare?\s*sitz",
        r"heated\s*seat",
    ],
    "elektrische_stoelen": [
        r"elektrische?\s*stoel",
        r"elektr.*sitz.*verstellung",
        r"sitzverstellung.*elektr",
        r"power\s*seat",
        r"electric\s*seat",
        r"elektrisch\s*verstelba",
    ],
    "adaptief_onderstel": [
        r"adaptie[fv].*onderstel",
        r"sport\s*onderstel",
        r"adaptiv.*fahrwerk",
        r"sport[\s-]*fahrwerk",
        r"progressive\s*steering",
        r"damper\s*control",
        r"magnetic\s*ride",
        r"dynamic\s*chassis",
        r"fahrwerk\s*sport",
    ],
}


def score_listing(listing: Listing) -> Listing:
    """Score a listing. Must-have features count 2 pts, nice-to-have 1 pt."""
    text = f"{listing.title} {listing.description}".lower()
    found_must = []
    found_nice = []
    for feature, patterns in FEATURE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                if feature in MUST_HAVE_FEATURES:
                    found_must.append(feature)
                elif feature in NICE_TO_HAVE_FEATURES:
                    found_nice.append(feature)
                break
    listing.features = found_must + found_nice
    # Must-haves tellen dubbel
    listing.score = len(found_must) * 2 + len(found_nice)
    listing.must_have_count = len(found_must)
    listing.nice_to_have_count = len(found_nice)
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
    max_score = len(MUST_HAVE_FEATURES) * 2 + len(NICE_TO_HAVE_FEATURES)

    # Bouw checklist per feature met ✅ / ❌
    must_lines = []
    for f in MUST_HAVE_FEATURES:
        name = FEATURE_DISPLAY_NAMES.get(f, f)
        if f in listing.features:
            must_lines.append(f"  ✅ {name}")
        else:
            must_lines.append(f"  ❌ {name}")

    nice_lines = []
    for f in NICE_TO_HAVE_FEATURES:
        name = FEATURE_DISPLAY_NAMES.get(f, f)
        if f in listing.features:
            nice_lines.append(f"  ✅ {name}")
        else:
            nice_lines.append(f"  ➖ {name}")

    price_str = f"€{listing.price:,}" if listing.price else "onbekend"
    price_verdict = compute_price_score(listing.price)

    # Stars gebaseerd op totaalscore
    pct = listing.score / max_score if max_score else 0
    if pct >= 0.85:
        rating = "🔥🔥🔥 TOPPER"
    elif pct >= 0.65:
        rating = "⭐⭐ Goed"
    elif pct >= 0.40:
        rating = "⭐ Matig"
    else:
        rating = "👎 Weinig opties"

    location_line = f"📍 Locatie: {listing.location}\n" if listing.location else ""
    date_line = f"🗓 Geplaatst: {listing.listing_date}\n" if listing.listing_date else ""

    text = (
        f"🚗 <b>Nieuwe Q3 Hybrid gevonden!</b>\n"
        f"{'━' * 30}\n\n"
        f"<b>{listing.title}</b>\n\n"
        f"💰 <b>{price_str}</b>  {price_verdict}\n"
        f"📅 Bouwjaar: {listing.year}\n"
        f"🛣 Kilometerstand: {listing.km:,} km\n"
        f"{location_line}"
        f"{date_line}"
        f"📊 Score: <b>{listing.score}/{max_score}</b> — {rating}\n\n"
        f"{'━' * 30}\n"
        f"<b>Must-have opties:</b>\n"
        + "\n".join(must_lines)
        + "\n\n"
        f"<b>Nice-to-have:</b>\n"
        + "\n".join(nice_lines)
        + "\n\n"
        f"{'━' * 30}\n"
        f"🔗 <a href=\"{listing.url}\">👉 BEKIJK ADVERTENTIE</a>\n"
        f"📍 Bron: {listing.source}"
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


def scrape_do_fetch(url: str, render: bool = False) -> str | None:
    """Haal een pagina op via Scrape.do API.

    Scrape.do handelt DataDome, CAPTCHAs en anti-bot bypass automatisch af.
    super=true activeert geavanceerde anti-bot bypass (nodig voor mobile.de).
    geoCode=de zorgt voor Duits IP-adres.
    Retourneert HTML string of None bij fout.
    """
    if not SCRAPE_DO_TOKEN:
        return None

    api_url = (
        f"https://api.scrape.do"
        f"?token={SCRAPE_DO_TOKEN}"
        f"&url={quote(url)}"
        f"&super=true"
        f"&geoCode=de"
    )
    if render:
        api_url += "&render=true"

    try:
        resp = req_lib.get(api_url, timeout=60)
        log.info("Scrape.do: status=%d, size=%d bytes voor %s", resp.status_code, len(resp.content), url[:80])

        if resp.status_code == 200:
            return resp.text
        elif resp.status_code == 401:
            log.error("Scrape.do: ongeldige API token")
        elif resp.status_code == 429:
            log.warning("Scrape.do: rate limit bereikt")
        else:
            log.warning("Scrape.do: fout status %d — %s", resp.status_code, resp.text[:200])
        return None
    except req_lib.RequestException as e:
        log.error("Scrape.do: request mislukt: %s", e)
        return None


def scrape_mobile_de_scrapedo(conn) -> list:
    """Scrape mobile.de via Scrape.do API + BeautifulSoup.

    Scrape.do bypassed DataDome automatisch — geen TLS spoofing of proxy nodig.
    Haalt ook detail pagina's op voor feature-detectie.
    """
    listings = []

    if not SCRAPE_DO_TOKEN:
        log.info("mobile.de Scrape.do: geen API token, overslaan")
        return listings

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

            listing_id = f"mobile_{re.sub(r'[^a-zA-Z0-9]', '', href[-20:])}" if href else f"mobile_{hash(title)}"

            if listing_exists(conn, listing_id):
                known_streak += 1
                log.info("Bekende listing: %s", title[:50])
                if known_streak >= 3:
                    log.info("3 bekende op rij — stoppen")
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

            # Filter
            if price and price > SEARCH_CRITERIA["price_max"]:
                continue
            if km and km > SEARCH_CRITERIA["km_max"]:
                continue
            if year and year < SEARCH_CRITERIA["year_min"]:
                continue

            # Hybrid check (op basis van card text)
            if not is_q3_hybrid(title, card_text, ""):
                continue

            # Detail pagina ophalen via Scrape.do voor feature-detectie
            description = card_text[:500]
            location = ""
            listing_date = ""
            if href:
                log.info("mobile.de Scrape.do: detail ophalen voor %s ...", title[:50])
                time.sleep(random.uniform(0.5, 1.5))  # Korte pauze tussen requests
                detail_html = scrape_do_fetch(href)
                if detail_html:
                    detail_soup = BeautifulSoup(detail_html, "html.parser")

                    # Technische data
                    for sel in [
                        "div.cBox-body.cBox-body--technical-data",
                        ".cBox--technicalData",
                        "#rbt-td",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            description = el.get_text(separator=" ", strip=True)
                            break

                    # Features + beschrijving
                    for sel in [
                        "#description", ".cBox--vehicleDescription",
                        ".cBox--features", "#features",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            description += " " + el.get_text(separator=" ", strip=True)

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

            # Hybrid check opnieuw met volledige beschrijving
            if not is_q3_hybrid(listing.title, listing.description, ""):
                log.info("Geen hybrid na detail check: %s", title[:50])
                continue

            log.info("MATCH: %s — €%s — %d km — %d", title[:50], f"{price:,}" if price else "?", km, year)
            listings.append(listing)

        except Exception as e:
            log.warning("mobile.de Scrape.do card: %s", e)
            continue

    log.info("mobile.de Scrape.do: %d listings gevonden", len(listings))
    return listings


def scrape_mobile_de_http(conn, proxy_url: str | None = None) -> list:
    """Scrape mobile.de via HTTP + BeautifulSoup.

    Gebruikt curl_cffi (Chrome TLS fingerprint) als beschikbaar,
    anders plain requests als fallback.
    """
    listings = []

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

        # Korte pauze (menselijk)
        time.sleep(random.uniform(1.0, 3.0))

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

                listing_id = f"mobile_{re.sub(r'[^a-zA-Z0-9]', '', href[-20:])}" if href else f"mobile_{hash(title)}"

                if listing_exists(conn, listing_id):
                    known_streak += 1
                    log.info("Bekende listing: %s", title[:50])
                    if known_streak >= 3:
                        log.info("3 bekende op rij — stoppen")
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

                # Filter
                if price and price > SEARCH_CRITERIA["price_max"]:
                    continue
                if km and km > SEARCH_CRITERIA["km_max"]:
                    continue
                if year and year < SEARCH_CRITERIA["year_min"]:
                    continue

                # Hybrid check
                if not is_q3_hybrid(title, card_text, ""):
                    continue

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
    match = re.search(r"(20[12]\d)", text)
    return int(match.group(1)) if match else 0


async def scrape_mobile_de(page, conn) -> list[Listing]:
    """Scrape mobile.de for Audi Q3 hybrid listings in Duitsland.
    Zoekt op 'Q3 Pano' met hybrid filter — matched de user's eigen zoekopdracht.
    Blokkeert images/fonts voor snelheid via proxy."""
    listings = []
    search_url = MOBILE_DE_SEARCH_URL

    log.info("Scraping mobile.de ...")
    try:
        # Blokkeer zware resources — maakt proxy 3x sneller
        await page.context.route(
            "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
            lambda route: route.abort(),
        )

        # ── Warm-up: bezoek homepage eerst (minder verdacht dan directe zoek-URL) ──
        log.info("mobile.de: warm-up via homepage ...")
        await human_delay(2, 4)
        try:
            await page.goto("https://www.mobile.de", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(random.randint(2000, 4000))
            # Scroll een beetje om menselijk te lijken
            await page.evaluate("window.scrollTo(0, Math.random() * 300)")
            await human_delay(1, 2)
        except Exception as e:
            log.warning("mobile.de: warm-up mislukt: %s", e)

        # ── Navigeer naar zoekresultaten ──
        await human_delay(1, 3)
        await page.goto(search_url, wait_until="domcontentloaded", timeout=90000)

        # Wacht tot JS content geladen is (mobile.de rendert dynamisch)
        await page.wait_for_timeout(5000)

        page_title = await page.title()
        page_url = page.url
        log.info("mobile.de page title: '%s'", page_title)
        log.info("mobile.de page URL: %s", page_url)

        # ── Detecteer IP-block ──
        if "zugriff verweigert" in page_title.lower() or "access denied" in page_title.lower():
            log.warning("mobile.de: IP geblokkeerd, wacht en retry ...")
            # Retry: wacht en probeer opnieuw (soms is het tijdelijk)
            await asyncio.sleep(random.uniform(5, 10))
            await page.goto(search_url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(5000)
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
            await page.wait_for_timeout(5000)

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

                listing_id = f"mobile_{re.sub(r'[^a-zA-Z0-9]', '', href[-20:])}" if href else f"mobile_{i}"

                if listing_exists(conn, listing_id):
                    known_streak += 1
                    log.info("Bekende listing: %s", title[:50])
                    if known_streak >= 3:
                        log.info("3 bekende op rij — stoppen")
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

                if price and price > SEARCH_CRITERIA["price_max"]:
                    continue
                if km and km > SEARCH_CRITERIA["km_max"]:
                    continue
                if year and year < SEARCH_CRITERIA["year_min"]:
                    continue

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
                        await dp.wait_for_timeout(3000)

                        # Technische data (bewezen selector)
                        for sel in [
                            "div.cBox-body.cBox-body--technical-data",
                            ".cBox--technicalData",
                            "#rbt-td",
                        ]:
                            el = dp.locator(sel)
                            if await el.count() > 0:
                                listing.description = await el.first.inner_text()
                                break

                        # Beschrijving + features
                        for sel in [
                            "#description", ".cBox--vehicleDescription",
                            ".cBox--features", "#features",
                        ]:
                            el = dp.locator(sel)
                            if await el.count() > 0:
                                listing.description += " " + await el.first.inner_text()

                        await dp.close()
                    except Exception as e:
                        log.warning("Detail fout: %s", e)

                if not is_q3_hybrid(listing.title, listing.description):
                    log.info("Geen Sportback hybrid: %s", title[:50])
                    continue

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
        await page.wait_for_timeout(5000)

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
                        if known_streak >= 3:
                            log.info("3 bekende op rij — stoppen")
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

                listing_id = f"as24_{re.sub(r'[^a-zA-Z0-9]', '', href[-20:])}" if href else f"as24_{i}"

                if listing_exists(conn, listing_id):
                    known_streak += 1
                    if known_streak >= 3:
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

    browser_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled",
    ]

    all_listings: list[Listing] = []

    # ── mobile.de: Scrape.do eerst, dan HTTP/proxies, dan Playwright ──
    mobile_listings = []

    # PRIMAIR: Scrape.do API (bypassed DataDome automatisch)
    if SCRAPE_DO_TOKEN:
        log.info("mobile.de [Scrape.do API] ...")
        mobile_listings = scrape_mobile_de_scrapedo(conn)
        if mobile_listings:
            log.info("mobile.de [Scrape.do]: %d listings!", len(mobile_listings))
        else:
            log.warning("mobile.de [Scrape.do]: 0 listings, probeer fallback ...")

    # FALLBACK 1: HTTP met curl_cffi/proxies
    if not mobile_listings:
        http_attempts: list[tuple[str, str | None]] = []
        for pu in PROXY_URLS:
            label = pu.split("@")[-1] if "@" in pu else pu
            http_attempts.append((f"HTTP proxy {label}", pu))
        http_attempts.append(("HTTP direct", None))

        for label, proxy in http_attempts:
            if mobile_listings:
                break
            log.info("mobile.de [%s] ...", label)
            mobile_listings = scrape_mobile_de_http(conn, proxy_url=proxy)
            if mobile_listings:
                log.info("mobile.de [%s]: %d listings!", label, len(mobile_listings))
                break
            log.warning("mobile.de [%s]: 0 listings", label)
            time.sleep(random.uniform(1, 3))

    # FALLBACK 2: Playwright browser
    if not mobile_listings:
        log.info("mobile.de: HTTP mislukt, probeer Playwright ...")

    async with async_playwright() as p:
        if not mobile_listings:
            pw_attempts: list[tuple[str, str | None]] = []
            for pu in PROXY_URLS:
                label = pu.split("@")[-1] if "@" in pu else pu
                pw_attempts.append((f"Playwright proxy {label}", pu))
            pw_attempts.append(("Playwright direct", None))

            for label, proxy_url in pw_attempts:
                log.info("mobile.de [%s] ...", label)
                if proxy_url:
                    proxy_cfg = parse_proxy_url(proxy_url)
                    mobile_browser = await p.chromium.launch(
                        headless=True, args=browser_args, proxy=proxy_cfg,
                    )
                else:
                    mobile_browser = await p.chromium.launch(
                        headless=True, args=browser_args,
                    )

                mobile_ctx = await create_stealth_context(mobile_browser)
                mobile_page = await mobile_ctx.new_page()
                mobile_listings = await scrape_mobile_de(mobile_page, conn)
                await mobile_browser.close()

                if mobile_listings:
                    log.info("mobile.de [%s]: %d listings!", label, len(mobile_listings))
                    break
                log.warning("mobile.de [%s]: 0 listings", label)

        all_listings.extend(mobile_listings)
        log.info("mobile.de: %d NIEUWE hybrid listings gevonden", len(mobile_listings))

    log.info("Totaal: %d listings gevonden, nu scoren ...", len(all_listings))

    new_count = 0
    alert_count = 0

    for listing in all_listings:
        listing = score_listing(listing)
        is_new = not listing_exists(conn, listing.id)
        save_listing(conn, listing)

        if is_new:
            new_count += 1
            # Check HARDE EISEN: alleen alert als ALLE required features aanwezig zijn
            missing = [f for f in REQUIRED_FEATURES if f not in listing.features]
            if missing:
                missing_names = ", ".join(FEATURE_DISPLAY_NAMES.get(f, f) for f in missing)
                log.info(
                    "SKIP (mist vereiste features: %s): %s — score %d — %s",
                    missing_names, listing.title[:50], listing.score, listing.url,
                )
            else:
                send_telegram(listing)
                alert_count += 1
                log.info(
                    "ALERT: %s — must-have %d/%d, score %d — €%s — %s",
                    listing.title,
                    listing.must_have_count,
                    len(MUST_HAVE_FEATURES),
                    listing.score,
                    f"{listing.price:,}" if listing.price else "?",
                    listing.url,
                )
        else:
            log.info("Bekende listing bijgewerkt: %s", listing.id)

    conn.close()

    log.info(
        "=== Klaar: %d totaal, %d nieuw, %d alerts verstuurd ===",
        len(all_listings),
        new_count,
        alert_count,
    )

    # Send summary — alleen listings die ALLE vereiste features hebben
    if all_listings:
        max_score = len(MUST_HAVE_FEATURES) * 2 + len(NICE_TO_HAVE_FEATURES)
        qualified = [
            lst for lst in all_listings
            if all(f in lst.features for f in REQUIRED_FEATURES)
        ]
        sorted_listings = sorted(qualified, key=lambda l: l.score, reverse=True)
        skipped = len(all_listings) - len(qualified)

        summary = (
            f"📊 <b>Scan samenvatting</b>\n\n"
            f"🔍 Totaal gevonden: {len(all_listings)}\n"
            f"✅ Voldoet aan eisen: {len(qualified)}\n"
            f"❌ Afgevallen (mist camera/keyless/pano): {skipped}\n"
            f"🔔 Alerts verstuurd: {alert_count}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"{'━' * 30}\n"
        )

        if not sorted_listings:
            summary += "<i>Geen listings met alle vereiste features gevonden.</i>\n"
        else:
            summary += f"<b>🏆 Top {len(sorted_listings)} listings:</b>\n\n"

        for i, lst in enumerate(sorted_listings[:15], 1):
            # Rating
            pct = lst.score / max_score if max_score else 0
            if pct >= 0.85:
                rating = "🔥"
            elif pct >= 0.65:
                rating = "⭐⭐"
            elif pct >= 0.40:
                rating = "⭐"
            else:
                rating = "👎"

            price_str = f"€{lst.price:,}" if lst.price else "?"
            km_str = f"{lst.km // 1000}k" if lst.km else "?"
            loc_str = f" · {lst.location}" if lst.location else ""
            date_str = f" · {lst.listing_date}" if lst.listing_date else ""
            features_str = ", ".join(
                FEATURE_DISPLAY_NAMES.get(f, f) for f in lst.features[:4]
            )

            summary += (
                f"{i}. {rating} <b>{lst.title[:45]}</b>\n"
                f"   {price_str} · {lst.year} · {km_str} km{loc_str}{date_str}\n"
                f"   Score {lst.score}/{max_score} · {features_str}\n"
                f"   <a href=\"{lst.url}\">🔗 Bekijken</a>\n\n"
            )

        if DRY_RUN:
            log.info("[DRY-RUN] Telegram summary:\n%s", summary)
        else:
            # Telegram max 4096 chars — split als nodig
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            if len(summary) <= 4096:
                req_lib.post(
                    tg_url,
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": summary, "parse_mode": "HTML", "disable_web_page_preview": True},
                    timeout=30,
                )
            else:
                # Stuur in delen
                for chunk_start in range(0, len(summary), 4000):
                    chunk = summary[chunk_start:chunk_start + 4000]
                    req_lib.post(
                        tg_url,
                        json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True},
                        timeout=30,
                    )


if __name__ == "__main__":
    asyncio.run(main())
