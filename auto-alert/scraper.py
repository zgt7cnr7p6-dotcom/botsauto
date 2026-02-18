#!/usr/bin/env python3
"""
Auto-Alert Scraper
Scraped mobile.de en AutoScout24 voor Audi Q3 45 TFSI e deals.
Stuurt Telegram alerts bij goede matches.
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
    "model": "Audi Q3 45 TFSI e",
    "fuel": "hybride",
    "year_min": 2018,
    "km_max": 80_000,
    "price_max": 37_500,
}

MUST_HAVE_FEATURES = [
    "panoramadak",
    "achteruitrijcamera",
    "ambient lighting",
    "s line",
    "keyless",
    "elektrische stoelen",
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


# ── Feature scoring ────────────────────────────────────────────────────────


FEATURE_PATTERNS = {
    "panoramadak": [r"panorama", r"panoramic", r"pano[\s-]?dak", r"panoramadach"],
    "achteruitrijcamera": [
        r"achteruitrij\s*camera",
        r"r[üu]ckfahr\s*kamera",
        r"rear\s*view\s*camera",
        r"backup\s*camera",
        r"reversing\s*camera",
    ],
    "ambient lighting": [
        r"ambient",
        r"sfeer\s*verlichting",
        r"contour\s*verlichting",
        r"innenraumbeleuchtung",
    ],
    "s line": [r"s[\s-]?line", r"s-line", r"sline"],
    "keyless": [
        r"keyless",
        r"sleutel\s*loos",
        r"komfort\s*schl[üu]ssel",
        r"schl[üu]ssell",
        r"convenience\s*key",
    ],
    "elektrische stoelen": [
        r"elektrische?\s*stoel",
        r"elektr.*sitz",
        r"power\s*seat",
        r"electric\s*seat",
        r"sitzverstellung.*elektr",
        r"elektr.*sitzverstellung",
    ],
}


def score_listing(listing: Listing) -> Listing:
    text = f"{listing.title} {listing.description}".lower()
    found = []
    for feature, patterns in FEATURE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                found.append(feature)
                break
    listing.features = found
    listing.score = len(found)
    return listing


# ── Telegram ────────────────────────────────────────────────────────────────


def send_telegram(listing: Listing):
    features_str = ", ".join(listing.features) if listing.features else "geen gevonden"
    missing = [f for f in MUST_HAVE_FEATURES if f not in listing.features]
    missing_str = ", ".join(missing) if missing else "geen — alles aanwezig!"

    stars = "⭐" * listing.score
    text = (
        f"🚗 <b>Nieuwe match gevonden!</b>\n\n"
        f"<b>{listing.title}</b>\n"
        f"💰 €{listing.price:,}\n"
        f"📅 {listing.year} | 🛣 {listing.km:,} km\n"
        f"📊 Score: {listing.score}/{len(MUST_HAVE_FEATURES)} {stars}\n\n"
        f"✅ <b>Gevonden features:</b>\n{features_str}\n"
        f"❌ <b>Ontbrekend:</b>\n{missing_str}\n\n"
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
    """Scrape mobile.de for Audi Q3 45 TFSI e listings."""
    listings = []
    search_url = (
        "https://suchen.mobile.de/fahrzeuge/search.html?"
        "dam=false&isSearchRequest=true&ms=1900%3B62%3B%3B&"
        "fuel=HYBRID&maxMileage=80000&maxPrice=37500&"
        "minFirstRegistrationDate=2018-01-01&"
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

                # Filter on Q3 45 TFSI e
                if "q3" not in title.lower() or "45" not in title:
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

                if price > SEARCH_CRITERIA["price_max"]:
                    continue
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
    """Scrape AutoScout24 for Audi Q3 45 TFSI e listings."""
    listings = []
    search_url = (
        "https://www.autoscout24.nl/lst/audi/q3/ft_HYBRID"
        "?atype=C&cy=D%2CNL%2CB&desc=0&fregfrom=2018"
        "&kmto=80000&priceto=37500&search_id=1&sort=age&source=listpage_pagination&ustate=N%2CU"
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

                if "q3" not in title.lower() or "45" not in title:
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

                if price and price > SEARCH_CRITERIA["price_max"]:
                    continue
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
        "Zoekcriteria: %s, %s, %d+, max %d km, max €%s",
        SEARCH_CRITERIA["model"],
        SEARCH_CRITERIA["fuel"],
        SEARCH_CRITERIA["year_min"],
        SEARCH_CRITERIA["km_max"],
        f"{SEARCH_CRITERIA['price_max']:,}",
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
            # Send alert for listings with score >= 2
            if listing.score >= 2:
                send_telegram(listing)
                alert_count += 1
                log.info(
                    "ALERT: %s — score %d/6 — €%s — %s",
                    listing.title,
                    listing.score,
                    f"{listing.price:,}",
                    listing.url,
                )
            else:
                log.info(
                    "Nieuw maar lage score: %s — score %d/6",
                    listing.title,
                    listing.score,
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
