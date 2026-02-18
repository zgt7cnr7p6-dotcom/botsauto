#!/usr/bin/env python3
"""
Auto-Alert Scraper
Scraped mobile.de en AutoScout24 voor Audi Q3 45 TFSI e (hybrid) in Duitsland.
Stuurt Telegram alerts met feature-checklist en prijsscore.
Anti-detectie: random delays, user-agent rotatie, menselijk browse-gedrag.
"""

import os
import re
import json
import random
import sqlite3
import asyncio
import logging
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import quote_plus

import requests
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEARCH_CRITERIA = {
    "model": "Audi Q3 Sportback 45 TFSI e",
    "fuel": "hybrid",
    "year_min": 2021,
    "km_max": 85_000,
    "price_max": 38_000,
    "country": "DE",
}

# Must-have: advertentie wordt alleen gemeld als minstens enkele hiervan matchen
MUST_HAVE_FEATURES = [
    "keyless",
    "panoramadak",
    "audio_premium",
    "matrix_led",
    "s_line",
    "camera",
]

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

    # Webdriver property verbergen (belangrijkste anti-bot check)
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });
        Object.defineProperty(navigator, 'languages', {
            get: () => ['de-DE', 'de', 'en-US', 'en']
        });
        window.chrome = { runtime: {} };
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
    now = datetime.utcnow().isoformat()
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
    score: int = 0
    features: list = field(default_factory=list)
    must_have_count: int = 0
    nice_to_have_count: int = 0


# ── Hybrid detectie ───────────────────────────────────────────────────────

def is_q3_sportback_hybrid(title: str, description: str = "") -> bool:
    """
    Detecteer of een listing een Q3 Sportback hybrid is.
    Checkt op Sportback in titel/beschrijving EN op hybrid indicators.
    Soms staat 'Sportback' alleen in beschrijving of 'SB' in titel.
    """
    text = f"{title} {description}".lower()

    if "q3" not in text:
        return False

    # Check Sportback — soms als "SB", "Sportback", of in beschrijving
    sportback_patterns = [
        r"sportback",
        r"q3\s*sb\b",
    ]
    is_sportback = any(re.search(p, text, re.IGNORECASE) for p in sportback_patterns)
    if not is_sportback:
        return False

    # Check hybrid
    hybrid_patterns = [
        r"45\s*tfsi\s*e?\b",      # "45 TFSI e" of "45 TFSI"
        r"tfsi\s*e\b",            # "TFSI e" (de e = elektrisch)
        r"plug[\s-]?in",          # "Plug-in Hybrid"
        r"phev",                  # PHEV
        r"hybrid",                # Hybrid
    ]

    return any(re.search(p, text, re.IGNORECASE) for p in hybrid_patterns)


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

    text = (
        f"🚗 <b>Nieuwe Q3 Sportback 45 TFSI e gevonden!</b>\n"
        f"{'━' * 30}\n\n"
        f"<b>{listing.title}</b>\n\n"
        f"💰 <b>{price_str}</b>  {price_verdict}\n"
        f"📅 Bouwjaar: {listing.year}\n"
        f"🛣 Kilometerstand: {listing.km:,} km\n"
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

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
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
    Gesorteerd op nieuwste eerst — stopt bij bekende listings voor snelheid."""
    listings = []
    # ms=1900;62 = Audi Q3 (incl. Sportback), fuel=HYBRID, Germany, year>=2021, km<=85000, price<=38000
    # sb=doc = sorteren op nieuwste eerst. Sportback filter via is_q3_sportback_hybrid()
    search_url = (
        "https://suchen.mobile.de/fahrzeuge/search.html?"
        "dam=false&isSearchRequest=true&ms=1900%3B62%3B%3B&"
        "fuel=HYBRID&maxMileage=85000&maxPrice=38000&"
        "minFirstRegistrationDate=2021-01-01&"
        "ref=srpHead&refId=&s=Car&sb=doc&vc=Car"
    )

    log.info("Scraping mobile.de ...")
    try:
        await human_delay(1, 3)
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        await human_delay(2, 4)

        # Debug: screenshot + page info
        log.info("mobile.de page title: %s", await page.title())
        log.info("mobile.de page URL: %s", page.url)
        await page.screenshot(path="debug_mobile.png", full_page=True)
        log.info("Screenshot opgeslagen: debug_mobile.png")

        # Accept cookies if popup appears
        try:
            consent = page.locator("button:has-text('Akzeptieren'), button:has-text('Accept'), [data-testid='gdpr-consent-accept-btn'], button[id*='consent'], button[id*='accept'], .mde-consent-accept-btn")
            if await consent.count() > 0:
                await human_delay(0.5, 1.5)
                await consent.first.click()
                await human_delay(2, 4)
                await page.screenshot(path="debug_mobile_after_consent.png")
                log.info("Cookies geaccepteerd, screenshot opgeslagen")
        except Exception as e:
            log.info("Geen cookie popup of fout: %s", e)

        # Wacht extra op dynamische content
        await page.wait_for_timeout(5000)

        # Find listing cards — meerdere mogelijke selectors
        cards = page.locator(".cBox-body--resultitem, [data-testid='result-listing-entry'], .result-item, .cBox-body--resultInnerRow")
        count = await cards.count()
        log.info("mobile.de: %d resultaten gevonden", count)

        # Debug: als 0 resultaten, log page content
        if count == 0:
            body_text = await page.locator("body").inner_text()
            log.info("mobile.de body text (eerste 1000 chars): %s", body_text[:1000])

        known_streak = 0  # Tel opeenvolgende bekende listings

        for i in range(min(count, 50)):
            try:
                card = cards.nth(i)
                title_el = card.locator("a.link--muted-secondary, .headline-block a, [data-testid='result-listing-entry-header'] a").first
                title = (await title_el.inner_text()).strip()

                if "q3" not in title.lower():
                    continue

                href = await title_el.get_attribute("href")
                if href and not href.startswith("http"):
                    href = "https://suchen.mobile.de" + href

                listing_id = f"mobile_{re.sub(r'[^a-zA-Z0-9]', '', href[-20:])}" if href else f"mobile_{i}"

                # SNELHEID: bekende listing? Skip detail pagina!
                if listing_exists(conn, listing_id):
                    known_streak += 1
                    log.info("Bekende listing, skip: %s", title[:50])
                    # Na 3 bekende op rij → rest is ook oud, stop
                    if known_streak >= 3:
                        log.info("3 bekende listings op rij — stoppen (nieuwste eerst)")
                        break
                    continue
                known_streak = 0  # Reset bij nieuwe listing

                price_el = card.locator(".price-block .h3, [data-testid='price-label'], .pricePrimaryCountryOfSale").first
                price_text = await price_el.inner_text()
                price = parse_price(price_text)

                details_el = card.locator(".rbt-regMil498, .vehicle-data, [data-testid='regMilPow']").first
                details_text = ""
                if await details_el.count() > 0:
                    details_text = await details_el.inner_text()

                year = parse_year(details_text)
                km = parse_km(details_text)

                if price and price > SEARCH_CRITERIA["price_max"]:
                    continue
                if km and km > SEARCH_CRITERIA["km_max"]:
                    continue
                if year and year < SEARCH_CRITERIA["year_min"]:
                    continue

                listing = Listing(
                    id=listing_id,
                    source="mobile.de",
                    title=title,
                    price=price,
                    year=year,
                    km=km,
                    url=href or search_url,
                )

                # Alleen detail pagina voor NIEUWE listings
                if href:
                    try:
                        detail_page = await page.context.new_page()
                        await human_delay(1, 2.5)
                        await detail_page.goto(href, wait_until="domcontentloaded", timeout=30000)
                        await human_delay(1.5, 3)

                        desc_el = detail_page.locator("#description, .cBox--vehicleDescription, .description-text, .g-col-12")
                        if await desc_el.count() > 0:
                            listing.description = await desc_el.first.inner_text()

                        feat_el = detail_page.locator(".cBox--features, #features, .vehicle-features, .bullet-list")
                        if await feat_el.count() > 0:
                            listing.description += " " + await feat_el.first.inner_text()

                        tech_el = detail_page.locator(".cBox--technicalData, .technical-data, #rbt-td")
                        if await tech_el.count() > 0:
                            listing.description += " " + await tech_el.first.inner_text()

                        await detail_page.close()
                    except Exception as e:
                        log.warning("Kon detail pagina niet laden: %s", e)

                # Hybrid check met volledige info
                if not is_q3_sportback_hybrid(listing.title, listing.description):
                    log.info("Overgeslagen (geen Sportback hybrid): %s", title)
                    continue

                listings.append(listing)
            except Exception as e:
                log.warning("mobile.de card %d overgeslagen: %s", i, e)
                continue

    except Exception as e:
        log.error("mobile.de scraping mislukt: %s", e)

    return listings


async def scrape_autoscout24(page, conn) -> list[Listing]:
    """Scrape AutoScout24 for Audi Q3 hybrid listings in Duitsland.
    Gesorteerd op nieuwste eerst — stopt bij bekende listings voor snelheid."""
    listings = []
    # Audi Q3 Sportback, hybrid fuel, only Germany, year >= 2021, km <= 85000, price <= 38000
    # sort=age = nieuwste eerst
    search_url = (
        "https://www.autoscout24.de/lst/audi/q3-sportback"
        "?atype=C&cy=D&desc=0&fregfrom=2021&fuel=E"
        "&kmto=85000&priceto=38000&search_id=1&sort=age&source=listpage_pagination&ustate=N%2CU"
    )

    log.info("Scraping AutoScout24 ...")
    try:
        await human_delay(1, 3)
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        await human_delay(2, 4)

        # Debug: screenshot + page info
        log.info("AutoScout24 page title: %s", await page.title())
        log.info("AutoScout24 page URL: %s", page.url)
        await page.screenshot(path="debug_autoscout.png", full_page=True)
        log.info("Screenshot opgeslagen: debug_autoscout.png")

        # Accept cookies
        try:
            consent = page.locator("button:has-text('Einverstanden'), button:has-text('Akzeptieren'), button:has-text('Agree'), #onetrust-accept-btn-handler, button[id*='accept']")
            if await consent.count() > 0:
                await human_delay(0.5, 1.5)
                await consent.first.click()
                await human_delay(2, 4)
                await page.screenshot(path="debug_autoscout_after_consent.png")
                log.info("Cookies geaccepteerd, screenshot opgeslagen")
        except Exception as e:
            log.info("Geen cookie popup of fout: %s", e)

        # Wacht extra op dynamische content
        await page.wait_for_timeout(5000)

        cards = page.locator("article[data-testid], .list-page-item, .cl-list-element, .ListItem_wrapper__TxHWu")
        count = await cards.count()
        log.info("AutoScout24: %d resultaten gevonden", count)

        # Debug: als 0 resultaten, log page content
        if count == 0:
            body_text = await page.locator("body").inner_text()
            log.info("AutoScout24 body text (eerste 1000 chars): %s", body_text[:1000])

        known_streak = 0

        for i in range(min(count, 50)):
            try:
                card = cards.nth(i)

                title_el = card.locator("a h2, a[data-testid] span, .title a, a.ListItem_title__ndA4s").first
                if await title_el.count() == 0:
                    continue
                title = (await title_el.inner_text()).strip()

                if "q3" not in title.lower():
                    continue

                link_el = card.locator("a[href*='/angebot/'], a[href*='/aanbod/'], a[href*='/offers/']").first
                href = ""
                if await link_el.count() > 0:
                    href = await link_el.get_attribute("href")
                    if href and not href.startswith("http"):
                        href = "https://www.autoscout24.de" + href

                listing_id = f"as24_{re.sub(r'[^a-zA-Z0-9]', '', href[-20:])}" if href else f"as24_{i}"

                # SNELHEID: bekende listing? Skip!
                if listing_exists(conn, listing_id):
                    known_streak += 1
                    log.info("Bekende listing, skip: %s", title[:50])
                    if known_streak >= 3:
                        log.info("3 bekende listings op rij — stoppen (nieuwste eerst)")
                        break
                    continue
                known_streak = 0

                price_text = ""
                price_el = card.locator("[data-testid='price'], .price, span:has-text('€')").first
                if await price_el.count() > 0:
                    price_text = await price_el.inner_text()
                price = parse_price(price_text)

                details_text = ""
                details_el = card.locator("[data-testid='vehicle-details'], .vehicle-details, span:has-text('km')").first
                if await details_el.count() > 0:
                    details_text = await details_el.inner_text()

                year = parse_year(details_text)
                km = parse_km(details_text)

                card_text = await card.inner_text()
                if not year:
                    year = parse_year(card_text)
                if not km:
                    km = parse_km(card_text)

                if price and price > SEARCH_CRITERIA["price_max"]:
                    continue
                if km and km > SEARCH_CRITERIA["km_max"]:
                    continue
                if year and year < SEARCH_CRITERIA["year_min"]:
                    continue

                listing = Listing(
                    id=listing_id,
                    source="AutoScout24",
                    title=title,
                    price=price,
                    year=year,
                    km=km,
                    url=href or search_url,
                )

                # Alleen detail pagina voor NIEUWE listings
                if href:
                    try:
                        detail_page = await page.context.new_page()
                        await human_delay(1, 2.5)
                        await detail_page.goto(href, wait_until="domcontentloaded", timeout=30000)
                        await human_delay(1.5, 3)

                        desc_el = detail_page.locator("[data-testid='description'], .vehicle-description, .cldt-stage-data, #description")
                        if await desc_el.count() > 0:
                            listing.description = await desc_el.first.inner_text()

                        equip_el = detail_page.locator("[data-testid='equipments'], .equipment-list, .cldt-equipment, #equipment")
                        if await equip_el.count() > 0:
                            listing.description += " " + await equip_el.first.inner_text()

                        tech_el = detail_page.locator("[data-testid='technical-data'], .cldt-technical-data, .StageArea_overviewContainer__UHhFb")
                        if await tech_el.count() > 0:
                            listing.description += " " + await tech_el.first.inner_text()

                        await detail_page.close()
                    except Exception as e:
                        log.warning("Kon detail pagina niet laden: %s", e)

                # Hybrid check met volledige info
                if not is_q3_sportback_hybrid(listing.title, listing.description):
                    log.info("Overgeslagen (geen Sportback hybrid): %s", title)
                    continue

                listings.append(listing)
            except Exception as e:
                log.warning("AutoScout24 card %d overgeslagen: %s", i, e)
                continue

    except Exception as e:
        log.error("AutoScout24 scraping mislukt: %s", e)

    return listings


# ── Main ────────────────────────────────────────────────────────────────────


async def main():
    log.info("=== Auto-Alert Scraper gestart ===")
    log.info(
        "Zoekcriteria: %s (%s), %d+, max %d km, max €%s, land: %s",
        SEARCH_CRITERIA["model"],
        SEARCH_CRITERIA["fuel"],
        SEARCH_CRITERIA["year_min"],
        SEARCH_CRITERIA["km_max"],
        f"{SEARCH_CRITERIA['price_max']:,}",
        SEARCH_CRITERIA["country"],
    )

    conn = init_db()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await create_stealth_context(browser)
        page = await context.new_page()

        all_listings: list[Listing] = []

        # Scrape both sources (conn meegeven voor snelle bekende-listing check)
        mobile_listings = await scrape_mobile_de(page, conn)
        all_listings.extend(mobile_listings)
        log.info("mobile.de: %d NIEUWE hybrid listings gevonden", len(mobile_listings))

        # Korte pauze tussen sites
        await human_delay(3, 6)

        as24_listings = await scrape_autoscout24(page, conn)
        all_listings.extend(as24_listings)
        log.info("AutoScout24: %d NIEUWE hybrid listings gevonden", len(as24_listings))

        await browser.close()

    log.info("Totaal: %d listings gevonden, nu scoren ...", len(all_listings))

    new_count = 0
    alert_count = 0

    for listing in all_listings:
        listing = score_listing(listing)
        is_new = not listing_exists(conn, listing.id)
        save_listing(conn, listing)

        if is_new:
            new_count += 1
            # Alert als minstens 2 must-have features gevonden
            if listing.must_have_count >= 2:
                send_telegram(listing)
                alert_count += 1
                log.info(
                    "ALERT: %s — must-have %d/%d, nice %d/%d, score %d — €%s — %s",
                    listing.title,
                    listing.must_have_count,
                    len(MUST_HAVE_FEATURES),
                    listing.nice_to_have_count,
                    len(NICE_TO_HAVE_FEATURES),
                    listing.score,
                    f"{listing.price:,}" if listing.price else "?",
                    listing.url,
                )
            else:
                log.info(
                    "Nieuw maar weinig must-haves: %s — must-have %d/%d",
                    listing.title,
                    listing.must_have_count,
                    len(MUST_HAVE_FEATURES),
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

    # Send summary if any new listings found
    if new_count > 0:
        summary = (
            f"📊 <b>Scan samenvatting</b>\n\n"
            f"🔍 Totaal gevonden: {len(all_listings)}\n"
            f"🆕 Nieuw: {new_count}\n"
            f"🔔 Alerts verstuurd: {alert_count}\n"
            f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": summary, "parse_mode": "HTML"},
            timeout=30,
        )


if __name__ == "__main__":
    asyncio.run(main())
