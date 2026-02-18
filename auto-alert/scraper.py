#!/usr/bin/env python3
"""
Auto-Alert Scraper
Scraped mobile.de en AutoScout24 voor Audi Q3 deals.
Stuurt Telegram alerts bij goede matches (must-have / nice-to-have scoring).
"""

import os
import re
import json
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
    "model": "Audi Q3",
    "year_min": 2021,
    "km_max": 85_000,
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


# ── Feature scoring ────────────────────────────────────────────────────────


FEATURE_PATTERNS = {
    # ── Must-have ──
    "keyless": [
        r"keyless",
        r"komfort\s*schl[üu]ssel",
        r"schl[üu]ssel\s*los",
        r"convenience\s*key",
        r"sleutel\s*loos",
        r"kessy",  # Audi intern
    ],
    "panoramadak": [
        r"panorama\s*d[ao][ck]h?",
        r"pano\b",
        r"panoramic",
        r"panorama\s*glas",
        r"panorama\s*schie?be?\s*dach",
        r"panorama\s*dak",
        r"panoramaverglasung",
    ],
    "audio_premium": [
        r"bang\s*[&+]\s*olufsen",
        r"b\s*[&+]\s*o",
        r"b&o",
        r"sonos",
        r"premium\s*sound",
        r"audi\s*sound",
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
        r"s[\s-]?line\s*int",
        r"s[\s-]?line\s*ext",
    ],
    "camera": [
        r"r[üu]ckfahr\s*kamera",
        r"achteruitrij\s*camera",
        r"rear\s*view\s*camera",
        r"backup\s*camera",
        r"reversing\s*camera",
        r"360\s*camera",
        r"360.?grad.?kamera",
        r"rundum\s*kamera",
        r"surround\s*view",
        r"umgebungs\s*kamera",
        r"r[üu]ckfahrkamera",
    ],
    # ── Nice-to-have ──
    "stoelverwarming": [
        r"stoel\s*verwarming",
        r"sitz\s*heizung",
        r"verwarmde?\s*stoel",
        r"beheizbare?\s*sitz",
        r"heated\s*seat",
        r"seat\s*heat",
    ],
    "elektrische_stoelen": [
        r"elektrische?\s*stoel",
        r"elektr.*sitz",
        r"power\s*seat",
        r"electric\s*seat",
        r"sitzverstellung.*elektr",
        r"elektr.*sitzverstellung",
        r"elektrisch\s*verstelba",
    ],
    "adaptief_onderstel": [
        r"adaptie[fv].*onderstel",
        r"sport\s*onderstel",
        r"adaptiv.*fahrwerk",
        r"sport\s*fahrwerk",
        r"s[\s-]?sport\s*fahrwerk",
        r"damper\s*control",
        r"magnetic\s*ride",
        r"dynamic\s*chassis",
        r"select\s*fahrwerk",
    ],
}


def score_listing(listing: Listing) -> Listing:
    """Score a listing.  Must-have features count 2 pts, nice-to-have 1 pt."""
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


# ── Telegram ────────────────────────────────────────────────────────────────


def send_telegram(listing: Listing):
    max_score = len(MUST_HAVE_FEATURES) * 2 + len(NICE_TO_HAVE_FEATURES)

    found_must = [f for f in listing.features if f in MUST_HAVE_FEATURES]
    found_nice = [f for f in listing.features if f in NICE_TO_HAVE_FEATURES]
    missing_must = [f for f in MUST_HAVE_FEATURES if f not in listing.features]

    must_str = ", ".join(found_must) if found_must else "geen"
    nice_str = ", ".join(found_nice) if found_nice else "geen"
    missing_str = ", ".join(missing_must) if missing_must else "alles aanwezig!"

    stars = "⭐" * min(listing.must_have_count, 6)
    price_str = f"€{listing.price:,}" if listing.price else "onbekend"

    text = (
        f"🚗 <b>Nieuwe Audi Q3 gevonden!</b>\n\n"
        f"<b>{listing.title}</b>\n"
        f"💰 {price_str}\n"
        f"📅 {listing.year} | 🛣 {listing.km:,} km\n"
        f"📊 Score: {listing.score}/{max_score} | Must-haves: {listing.must_have_count}/{len(MUST_HAVE_FEATURES)} {stars}\n\n"
        f"✅ <b>Must-have gevonden:</b>\n{must_str}\n"
        f"❌ <b>Must-have ontbrekend:</b>\n{missing_str}\n"
        f"💡 <b>Nice-to-have gevonden:</b>\n{nice_str}\n\n"
        f"🔗 <a href=\"{listing.url}\">Bekijk advertentie</a>\n"
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


async def scrape_mobile_de(page) -> list[Listing]:
    """Scrape mobile.de for Audi Q3 listings."""
    listings = []
    # ms=1900;62 = Audi Q3 (alle varianten), year >= 2021, km <= 85000
    search_url = (
        "https://suchen.mobile.de/fahrzeuge/search.html?"
        "dam=false&isSearchRequest=true&ms=1900%3B62%3B%3B&"
        "maxMileage=85000&"
        "minFirstRegistrationDate=2021-01-01&"
        "ref=srpHead&refId=&s=Car&sb=doc&vc=Car"
    )

    log.info("Scraping mobile.de ...")
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        # Accept cookies if popup appears
        try:
            consent = page.locator("button:has-text('Akzeptieren'), button:has-text('Accept'), [data-testid='gdpr-consent-accept-btn']")
            if await consent.count() > 0:
                await consent.first.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        # Find listing cards
        cards = page.locator(".cBox-body--resultitem, [data-testid='result-listing-entry'], .result-item")
        count = await cards.count()
        log.info("mobile.de: %d resultaten gevonden", count)

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

                price_el = card.locator(".price-block .h3, [data-testid='price-label'], .pricePrimaryCountryOfSale").first
                price_text = await price_el.inner_text()
                price = parse_price(price_text)

                details_el = card.locator(".rbt-regMil498, .vehicle-data, [data-testid='regMilPow']").first
                details_text = ""
                if await details_el.count() > 0:
                    details_text = await details_el.inner_text()

                year = parse_year(details_text)
                km = parse_km(details_text)

                if km > SEARCH_CRITERIA["km_max"]:
                    continue
                if year < SEARCH_CRITERIA["year_min"]:
                    continue

                listing_id = f"mobile_{re.sub(r'[^a-zA-Z0-9]', '', href[-20:])}" if href else f"mobile_{i}"

                listing = Listing(
                    id=listing_id,
                    source="mobile.de",
                    title=title,
                    price=price,
                    year=year,
                    km=km,
                    url=href or search_url,
                )

                # Navigate to detail page for description
                if href:
                    try:
                        detail_page = await page.context.new_page()
                        await detail_page.goto(href, wait_until="domcontentloaded", timeout=30000)
                        await detail_page.wait_for_timeout(2000)
                        desc_el = detail_page.locator("#description, .cBox--vehicleDescription, .description-text, .g-col-12")
                        if await desc_el.count() > 0:
                            listing.description = await desc_el.first.inner_text()

                        # Also grab features from features section
                        feat_el = detail_page.locator(".cBox--features, #features, .vehicle-features")
                        if await feat_el.count() > 0:
                            listing.description += " " + await feat_el.first.inner_text()
                        await detail_page.close()
                    except Exception as e:
                        log.warning("Kon detail pagina niet laden: %s", e)

                listings.append(listing)
            except Exception as e:
                log.warning("mobile.de card %d overgeslagen: %s", i, e)
                continue

    except Exception as e:
        log.error("mobile.de scraping mislukt: %s", e)

    return listings


async def scrape_autoscout24(page) -> list[Listing]:
    """Scrape AutoScout24 for Audi Q3 listings."""
    listings = []
    # Alle Audi Q3 varianten, year >= 2021, km <= 85000, DE + NL + BE
    search_url = (
        "https://www.autoscout24.nl/lst/audi/q3"
        "?atype=C&cy=D%2CNL%2CB&desc=0&fregfrom=2021"
        "&kmto=85000&search_id=1&sort=age&source=listpage_pagination&ustate=N%2CU"
    )

    log.info("Scraping AutoScout24 ...")
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        # Accept cookies
        try:
            consent = page.locator("button:has-text('Akkoord'), button:has-text('Agree'), #onetrust-accept-btn-handler")
            if await consent.count() > 0:
                await consent.first.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        cards = page.locator("article[data-testid], .list-page-item, .cl-list-element")
        count = await cards.count()
        log.info("AutoScout24: %d resultaten gevonden", count)

        for i in range(min(count, 50)):
            try:
                card = cards.nth(i)

                title_el = card.locator("a h2, a[data-testid] span, .title a, a.ListItem_title__ndA4s").first
                if await title_el.count() == 0:
                    continue
                title = (await title_el.inner_text()).strip()

                if "q3" not in title.lower():
                    continue

                link_el = card.locator("a[href*='/aanbod/'], a[href*='/offers/'], a[href*='/angebot/']").first
                href = ""
                if await link_el.count() > 0:
                    href = await link_el.get_attribute("href")
                    if href and not href.startswith("http"):
                        href = "https://www.autoscout24.nl" + href

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

                if km and km > SEARCH_CRITERIA["km_max"]:
                    continue
                if year and year < SEARCH_CRITERIA["year_min"]:
                    continue

                listing_id = f"as24_{re.sub(r'[^a-zA-Z0-9]', '', href[-20:])}" if href else f"as24_{i}"

                listing = Listing(
                    id=listing_id,
                    source="AutoScout24",
                    title=title,
                    price=price,
                    year=year,
                    km=km,
                    url=href or search_url,
                )

                # Detail page for description
                if href:
                    try:
                        detail_page = await page.context.new_page()
                        await detail_page.goto(href, wait_until="domcontentloaded", timeout=30000)
                        await detail_page.wait_for_timeout(2000)
                        desc_el = detail_page.locator("[data-testid='description'], .vehicle-description, .cldt-stage-data, #description")
                        if await desc_el.count() > 0:
                            listing.description = await desc_el.first.inner_text()

                        equip_el = detail_page.locator("[data-testid='equipments'], .equipment-list, .cldt-equipment, #equipment")
                        if await equip_el.count() > 0:
                            listing.description += " " + await equip_el.first.inner_text()
                        await detail_page.close()
                    except Exception as e:
                        log.warning("Kon detail pagina niet laden: %s", e)

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
        "Zoekcriteria: %s, %d+, max %d km",
        SEARCH_CRITERIA["model"],
        SEARCH_CRITERIA["year_min"],
        SEARCH_CRITERIA["km_max"],
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
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="nl-NL",
        )

        page = await context.new_page()

        all_listings: list[Listing] = []

        # Scrape both sources
        mobile_listings = await scrape_mobile_de(page)
        all_listings.extend(mobile_listings)
        log.info("mobile.de: %d relevante listings gevonden", len(mobile_listings))

        as24_listings = await scrape_autoscout24(page)
        all_listings.extend(as24_listings)
        log.info("AutoScout24: %d relevante listings gevonden", len(as24_listings))

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
