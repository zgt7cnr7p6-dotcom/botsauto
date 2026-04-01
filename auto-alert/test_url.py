#!/usr/bin/env python3
"""Test: haal een echte mobile.de detail page op en scoor 'm.

Gebruik:
    python test_url.py https://suchen.mobile.de/fahrzeuge/details.html?id=451110550
    python test_url.py 451110550
    python test_url.py https://suchen.mobile.de/fahrzeuge/details.html?id=451110550 https://suchen.mobile.de/fahrzeuge/details.html?id=451110557
"""
import os
import sys
import re

if not os.environ.get("SCRAPE_DO_TOKEN"):
    print("❌ SCRAPE_DO_TOKEN niet gezet")
    sys.exit(1)
if not os.environ.get("ANTHROPIC_API_KEY"):
    print("❌ ANTHROPIC_API_KEY niet gezet")
    sys.exit(1)

from scraper import (
    scrape_do_fetch, score_listing, clean_detail_html, Listing,
    FULL_OPTION_FEATURES, FEATURE_DISPLAY_NAMES, MUST_HAVE_FEATURES,
)
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs


def url_from_arg(arg: str) -> str:
    """Accepteer een volledige URL of alleen een listing ID."""
    if arg.startswith("http"):
        # Clean de URL — strip zoekparams, behoud alleen id
        parsed = urlparse(arg)
        params = parse_qs(parsed.query)
        if "id" in params:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?id={params['id'][0]}"
        return arg
    # Alleen een ID nummer
    if re.match(r"^\d+$", arg):
        return f"https://suchen.mobile.de/fahrzeuge/details.html?id={arg}"
    print(f"❌ Ongeldig argument: {arg}")
    sys.exit(1)


CONSENT_ACTIONS = [
    {"Action": "Wait", "Timeout": 3000},
    {"Action": "Execute", "Execute": "const host = document.getElementById('usercentrics-root'); if (host && host.shadowRoot) { const btn = host.shadowRoot.querySelector('button[data-testid=\"uc-accept-all-button\"]'); if (btn) btn.click(); }"},
    {"Action": "Wait", "Timeout": 2000},
]


def test_url(url: str):
    print(f"\n{'='*60}")
    print(f"🔗 URL: {url}")
    print(f"📡 Detail page ophalen via scrape.do (render=true, geoCode=de, consent click) ...")

    html = scrape_do_fetch(url, render=True, super_mode=True, retries=2, timeout=90, geo_code="de", play_with_browser=CONSENT_ACTIONS)

    if not html:
        print("❌ Geen response ontvangen")
        return

    print(f"📦 Response: {len(html)} bytes")

    if len(html) < 5000:
        print(f"⚠️  Response te klein ({len(html)} bytes) — waarschijnlijk geblokkeerd")
        # Toon de eerste 500 chars om te zien wat we terugkrijgen
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator=" ", strip=True)
        print(f"📄 Inhoud ({len(text)} chars): {text[:500]}")
        return

    soup = BeautifulSoup(html, "html.parser")
    body_text = clean_detail_html(soup)
    print(f"📄 Body text (cleaned): {len(body_text)} chars")

    if len(body_text) < 100:
        print("⚠️  Body text te kort — pagina niet goed geladen")
        return

    description = body_text[:20000]

    # Titel proberen te vinden
    title = "Onbekend"
    title_tag = soup.select_one("h1, [data-testid='ad-title']")
    if title_tag:
        title = title_tag.get_text(strip=True)
    print(f"🚗 Titel: {title}")

    # Prijs
    price = 0
    price_tag = soup.select_one("[data-testid='prime-price'], .price-block span")
    if price_tag:
        price_text = price_tag.get_text(strip=True)
        price_nums = re.findall(r"[\d.]+", price_text.replace(".", ""))
        if price_nums:
            price = int(price_nums[0])
    print(f"💰 Prijs: €{price:,}" if price else "💰 Prijs: onbekend")

    # Maak listing en scoor
    listing = Listing(
        id="test-url",
        source="test",
        title=title,
        price=price,
        year=0,
        km=0,
        url=url,
        description=description,
    )

    print(f"\n🤖 AI scoring ...")
    result = score_listing(listing)

    if not result:
        print("❌ Scoring mislukt")
        return

    max_score = len(FULL_OPTION_FEATURES)
    print(f"\n✅ Score: {result.score}/{max_score}")
    print()

    for f in FULL_OPTION_FEATURES:
        name = FEATURE_DISPLAY_NAMES.get(f, f)
        star = " ⭐" if f in MUST_HAVE_FEATURES else ""
        if f in result.features:
            print(f"  ✅ {name}{star}")
        else:
            print(f"  ❌ {name}{star}")

    # Beschrijving snippet tonen
    print(f"\n📝 Beschrijving (eerste 500 chars):")
    print(f"  {description[:500]}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Gebruik: python test_url.py <url-of-id> [url-of-id] ...")
        print("  python test_url.py 451110550")
        print("  python test_url.py https://suchen.mobile.de/fahrzeuge/details.html?id=451110550")
        sys.exit(1)

    for arg in sys.argv[1:]:
        url = url_from_arg(arg)
        test_url(url)

    print(f"\n{'='*60}")
    print("✅ Klaar")
