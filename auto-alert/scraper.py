#!/usr/bin/env python3
"""
Auto-Alert Scraper
Scraped mobile.de voor Audi Q3 45 TFSI e (hybrid) in Duitsland.
Stuurt Telegram alerts met feature-checklist en prijsscore.
Gebruikt Scrape.do als enige scraping provider (DataDome bypass).
"""

import os
import re
import sys
import json
import random
import sqlite3
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs

import requests as req_lib
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = "--dry-run" in sys.argv or not TELEGRAM_BOT_TOKEN

# Scrape.do API token — enige scraping provider
# Bypasses DataDome, anti-bot, CAPTCHAs automatisch
SCRAPE_DO_TOKEN = os.environ.get("SCRAPE_DO_TOKEN", "")

# Max aantal detail pagina's per run (beperkt API credits)
MAX_DETAIL_PAGES_PER_RUN = int(os.environ.get("MAX_DETAIL_PAGES", "15"))
_detail_page_count = 0

SEARCH_CRITERIA = {
    "model": "Audi Q3 45 TFSI e",
    "fuel": "hybrid",
    "year_min": 2021,
    "km_max": 80_000,
    "price_max": 40_000,
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
        "require_pano_in_desc": False,
    },
    {
        "url": (
            "https://suchen.mobile.de/fahrzeuge/search.html?"
            "cn=DE&dam=false&fr=2021%3A&ft=HYBRID&isSearchRequest=true"
            "&ml=%3A80000&ms=1900%3B37%3B%3Bsportback&od=down"
            "&p=%3A40000&s=Car&sb=doc&vc=Car"
        ),
        "label": "Q3 Sportback (pano check)",
        "require_pano_in_desc": True,
    },
]
MOBILE_DE_SEARCH_URL = MOBILE_DE_SEARCH_URLS[0]["url"]

# ── Full-option checklist ──────────────────────────────────────────────────
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
    """Detecteer of een listing een Q3 (Sportback) hybrid is."""
    title_lower = title.lower()

    if "q3" not in title_lower:
        return False

    title_hybrid_patterns = [
        r"45\s*tfsi",
        r"tfsi\s*e\b",
        r"plug[\s-]?in",
        r"phev",
    ]
    if any(re.search(p, title_lower) for p in title_hybrid_patterns):
        return True

    fuel_lower = fuel_type.lower()
    if any(kw in fuel_lower for kw in ["hybrid", "elektro", "phev", "plug-in", "electric"]):
        return True

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
            if len(color) > 2 and not any(w in color.lower() for w in ["fahrzeug", "ausstattung", "technisch"]):
                return color

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
            idx = text_lower.index(color.lower())
            snippet = text[max(0, idx):idx + 40].strip()
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

    title_lower = listing.title.lower()
    if "sportback" in title_lower:
        model_tag = "Q3 Sportback"
    else:
        model_tag = "Q3"

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
    color_line = f"🎨 Kleur: {listing.color}\n" if listing.color else ""

    missing_section = ""
    if missing_names:
        missing_section = (
            f"\n{'━' * 30}\n"
            f"<b>Wat ontbreekt:</b>\n"
            + "\n".join(f"  ❌ {name}" for name in missing_names)
            + "\n"
        )

    filled = listing.score
    empty = max_score - filled
    score_bar = "▓" * filled + "░" * empty

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


# ── Scrape.do fetch ─────────────────────────────────────────────────────────


def scrape_do_fetch(url: str, render: bool = False, retries: int = 1, super_mode: bool = True) -> str | None:
    """Haal een pagina op via Scrape.do met retry logica.

    super=true activeert geavanceerde anti-bot bypass (10 credits per request).
    super=false gebruikt standaard modus (1 credit per request).
    render=true activeert JS rendering (extra credits).
    """
    if not SCRAPE_DO_TOKEN:
        log.error("SCRAPE_DO_TOKEN niet geconfigureerd")
        return None

    for attempt in range(retries + 1):
        try:
            params = {
                "token": SCRAPE_DO_TOKEN,
                "url": url,
            }
            if super_mode:
                params["super"] = "true"
            if render:
                params["render"] = "true"
                params["wait"] = "3000"

            resp = req_lib.get("https://api.scrape.do", params=params, timeout=90)
            log.info("Scrape.do: status=%d, size=%d bytes voor %s",
                     resp.status_code, len(resp.content), url[:80])

            if resp.status_code == 200:
                html = resp.text
                if len(html) > 500:
                    return html
                log.warning("Scrape.do: te kleine response (%d chars)", len(html))
            elif resp.status_code == 401:
                log.error("Scrape.do: ongeldige API token")
                return None  # geen retry bij auth fout
            elif resp.status_code == 429:
                log.warning("Scrape.do: rate limit / credits op")
                if attempt < retries:
                    wait = 2 ** (attempt + 1)
                    log.info("Wacht %ds voor retry ...", wait)
                    time.sleep(wait)
                    continue
            else:
                log.warning("Scrape.do: fout status %d — %s", resp.status_code, resp.text[:200])

            if attempt < retries and resp.status_code != 200:
                wait = 2 ** (attempt + 1)
                log.info("Retry %d/%d na %ds ...", attempt + 1, retries, wait)
                time.sleep(wait)
                continue

            return None

        except req_lib.RequestException as e:
            log.error("Scrape.do: request mislukt: %s", e)
            if attempt < retries:
                wait = 2 ** (attempt + 1)
                log.info("Retry %d/%d na %ds ...", attempt + 1, retries, wait)
                time.sleep(wait)
                continue
            return None

    return None


# ── Helpers ─────────────────────────────────────────────────────────────────


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
    ez_match = re.search(r"EZ\s*(\d{2}/)?(20[12]\d)", text, re.IGNORECASE)
    if ez_match:
        return int(ez_match.group(2))

    matches = [int(m) for m in re.findall(r"(20[12]\d)", text)]
    if not matches:
        return 0

    current_year = datetime.now().year
    past_years = [y for y in matches if y < current_year]
    if past_years:
        return max(past_years)
    return matches[0]


def extract_listing_id(href: str, source: str, fallback: str = "") -> str:
    """Extract een stabiel listing ID uit een URL."""
    if not href:
        return f"{source}_{fallback}" if fallback else ""

    parsed = urlparse(href)

    if source == "mobile":
        params = parse_qs(parsed.query)
        if "id" in params:
            return f"mobile_{params['id'][0]}"
        nums = re.findall(r"(\d{6,})", parsed.path)
        if nums:
            return f"mobile_{nums[-1]}"

    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return f"{source}_{abs(hash(clean_url)) % 10**12}"


# ── mobile.de scraper ───────────────────────────────────────────────────────


def scrape_mobile_de(conn, search_url: str = "") -> list:
    """Scrape mobile.de via Scrape.do + BeautifulSoup.

    Haalt zoekpagina op en detail pagina's voor feature-detectie.
    """
    listings = []

    if not SCRAPE_DO_TOKEN:
        log.info("mobile.de: SCRAPE_DO_TOKEN niet geconfigureerd, overslaan")
        return listings

    if not search_url:
        search_url = MOBILE_DE_SEARCH_URL

    # Zoekpagina eerst zonder super mode (1 credit ipv 10)
    # Fallback naar super mode als we geblokkeerd worden
    log.info("mobile.de: zoekpagina ophalen (standaard modus) ...")
    html = scrape_do_fetch(search_url, super_mode=False, retries=0)

    blocked = False
    if html and ("zugriff verweigert" in html.lower() or "access denied" in html.lower()):
        log.warning("mobile.de: geblokkeerd zonder super mode, retry met super=true ...")
        blocked = True
        html = None

    if not html:
        if not blocked:
            log.warning("mobile.de: standaard modus mislukt, retry met super=true ...")
        html = scrape_do_fetch(search_url, super_mode=True, retries=1)

    if not html:
        log.error("mobile.de: geen HTML ontvangen")
        return listings

    # Detecteer block (ook na super mode)
    if "zugriff verweigert" in html.lower() or "access denied" in html.lower():
        log.error("mobile.de: geblokkeerd (Zugriff verweigert / Access denied)")
        try:
            with open("debug_mobile.html", "w", encoding="utf-8") as f:
                f.write(html[:500_000])
        except Exception:
            pass
        return listings

    # Debug HTML opslaan
    try:
        with open("debug_mobile.html", "w", encoding="utf-8") as f:
            f.write(html[:500_000])
    except Exception:
        pass

    soup = BeautifulSoup(html, "html.parser")

    page_title = soup.title.text if soup.title else ""
    log.info("mobile.de: page title = '%s'", page_title)

    # Zoek listing cards
    cards = soup.select("a[data-testid*='result'], a[data-testid*='listing']")
    if not cards:
        cards = soup.select("a.result-item, div.cBox-body--resultitem")
    if not cards:
        cards = soup.select("a[href*='/fahrzeuge/details']")

    log.info("mobile.de: %d cards gevonden", len(cards))

    if not cards:
        body = soup.get_text(separator=" ", strip=True)
        log.info("mobile.de body (2000 chars): %s", body[:2000])
        return listings

    known_streak = 0
    for card in cards[:50]:
        try:
            card_full_text = card.get_text(separator=" ", strip=True)

            # Skip gesponsorde advertenties
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

            # Detail pagina ophalen voor feature-detectie
            global _detail_page_count
            description = card_text[:500]
            location = ""
            listing_date = ""
            if href and _detail_page_count < MAX_DETAIL_PAGES_PER_RUN:
                _detail_page_count += 1
                log.info("mobile.de: detail ophalen [%d/%d] voor %s ...",
                         _detail_page_count, MAX_DETAIL_PAGES_PER_RUN, title[:50])
                time.sleep(random.uniform(0.3, 0.8))
                detail_html = scrape_do_fetch(href, render=True)
                if detail_html:
                    detail_soup = BeautifulSoup(detail_html, "html.parser")
                    description = ""

                    # 1. Titel
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

                    # 3. Features / Ausstattung
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

                    # 4. Vehicle Description
                    for sel in [
                        "#description", ".cBox--vehicleDescription",
                        "[data-testid='ad-detail-description']",
                        "[class*='VehicleDescription']",
                        "[class*='vehicleDescription']",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            description += " " + el.get_text(separator=" ", strip=True)

                    # Fallback: volledige body text
                    if len(description.strip()) < 100:
                        body_text = detail_soup.get_text(separator=" ", strip=True)
                        description = body_text[:15000]
                        log.info("Detail fallback (body text): %d chars", len(description))

                    # Locatie
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
                            loc_match = re.search(r"(\d{5}\s+\S+(?:\s+\S+)?)", loc_text)
                            if loc_match:
                                location = loc_match.group(1).strip()
                            elif len(loc_text) < 100:
                                location = loc_text
                            break

                    # Listing datum
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
                    if not listing_date:
                        full_text = detail_soup.get_text()
                        date_match = re.search(r"Inseriert?\s*(?:am\s*)?:?\s*(\d{1,2}\.\d{1,2}\.\d{4})", full_text)
                        if date_match:
                            listing_date = date_match.group(1)
                        else:
                            date_match = re.search(r"seit\s+(\d{1,2}\.\d{1,2}\.\d{4})", full_text, re.IGNORECASE)
                            if date_match:
                                listing_date = date_match.group(1)

                    log.info("mobile.de: detail %d chars, locatie=%s, datum=%s",
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
            log.warning("mobile.de card: %s", e)
            continue

    log.info("mobile.de: %d listings gevonden", len(listings))
    return listings


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    # Quiet hours: niet scrapen tussen 20:00 en 08:00 CET
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

    if not SCRAPE_DO_TOKEN:
        log.error("SCRAPE_DO_TOKEN niet geconfigureerd — kan niet scrapen")
        return

    log.info("Methode: Scrape.do API (super=true, DataDome bypass)")

    conn = init_db()

    all_listings: list[Listing] = []
    seen_ids: set[str] = set()

    pano_patterns = FEATURE_PATTERNS["panoramadak"]

    def has_pano_in_text(text: str) -> bool:
        text_lower = text.lower()
        return any(re.search(p, text_lower, re.IGNORECASE) for p in pano_patterns)

    # ── mobile.de ──
    for search_cfg in MOBILE_DE_SEARCH_URLS:
        search_url = search_cfg["url"]
        url_label = search_cfg["label"]
        require_pano = search_cfg["require_pano_in_desc"]

        log.info("━━━ %s ━━━", url_label)

        mobile_listings = scrape_mobile_de(conn, search_url=search_url)

        if mobile_listings:
            log.info("[%s] %d listings gevonden", url_label, len(mobile_listings))
        else:
            log.warning("[%s] 0 listings", url_label)

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

    # ── Score en alert ──
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

    log.info(
        "=== Klaar: %d totaal, %d nieuw, %d alerts verstuurd ===",
        len(all_listings),
        new_count,
        alert_count,
    )


if __name__ == "__main__":
    main()
