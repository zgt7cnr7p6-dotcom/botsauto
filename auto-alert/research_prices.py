#!/usr/bin/env python3
"""Research NL marktprijzen via Gaspedaal/AutoScout24.

Scraped zoekpagina's via Scrape.do en parsed prijzen, km, bouwjaar en opties.
Output: gestructureerde prijstabel per model/jaar/optieniveau.

Gebruik: python research_prices.py
Vereist: SCRAPE_DO_TOKEN env var
"""

import json
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

SCRAPE_DO_TOKEN = os.environ.get("SCRAPE_DO_TOKEN", "")
if not SCRAPE_DO_TOKEN:
    print("ERROR: SCRAPE_DO_TOKEN niet gezet")
    sys.exit(1)


SEARCH_URLS = {
    "q3_sportback_45_tfsi_e": [
        {
            "url": "https://www.autoscout24.nl/lst/audi/q3/ve_sportback/bt_hybrid?fregfrom=2021&cy=NL&sort=standard&desc=0&ustate=N%2CU&size=20&atype=C",
            "site": "autoscout24",
        },
    ],
    "q5_50_tfsi_e": [
        {
            "url": "https://www.autoscout24.nl/lst/audi/q5/bt_hybrid?fregfrom=2021&cy=NL&sort=standard&desc=0&ustate=N%2CU&size=20&atype=C",
            "site": "autoscout24",
        },
    ],
    "mercedes_glc_300e": [
        {
            "url": "https://www.autoscout24.nl/lst/mercedes-benz/glc/ve_300-e?fregfrom=2021&cy=NL&sort=standard&desc=0&ustate=N%2CU&size=20&atype=C",
            "site": "autoscout24",
        },
    ],
    "mercedes_c_300e": [
        {
            "url": "https://www.autoscout24.nl/lst/mercedes-benz/c-klasse/ve_300-e?fregfrom=2021&cy=NL&sort=standard&desc=0&ustate=N%2CU&size=20&atype=C",
            "site": "autoscout24",
        },
    ],
    "bmw_330e": [
        {
            "url": "https://www.autoscout24.nl/lst/bmw/3-serie/ve_330e?fregfrom=2021&cy=NL&sort=standard&desc=0&ustate=N%2CU&size=20&atype=C",
            "site": "autoscout24",
        },
    ],
}

IMPORTANT_OPTIONS = [
    "panorama", "pano", "glasdach",
    "s line", "s-line", "sline", "amg", "m sport", "m-sport", "msport",
    "sonos", "b&o", "bang", "olufsen", "burmester", "harman", "kardon", "beats",
    "matrix", "laser", "digital light",
    "keyless", "komfort", "kessy", "comfort access",
    "360", "surround", "topview", "top view", "area view",
    "head-up", "headup", "hud", "head up",
    "acc", "distronic", "adaptiv", "cruise",
    "leder", "leather", "alcantara",
    "memory",
    "ahk", "anhänger", "trekhaak", "towbar",
    "luchtv", "airmatic", "air suspension",
    "19", "20", "21",  # velgenmaten
    "night", "shadow", "optik", "black",
    "elektr", "achterklep", "heckklappe",
    "ambient",
    "side assist", "dodehoek", "totwinkel",
    "stoelverw", "sitzheiz", "seat heat",
    "stuurverw", "lenkradheiz",
    "camera", "kamera", "rückfahr",
]


@dataclass
class Listing:
    title: str = ""
    price: int = 0
    year: int = 0
    km: int = 0
    options_found: list = field(default_factory=list)
    option_score: str = ""  # "full", "mid", "basis"
    raw_text: str = ""


def scrape_do_fetch(url: str) -> str:
    """Fetch via Scrape.do (basic mode, 1 credit)."""
    api_url = "https://api.scrape.do?" + urllib.parse.urlencode({
        "token": SCRAPE_DO_TOKEN,
        "url": url,
        "geoCode": "nl",
    })
    try:
        resp = requests.get(api_url, timeout=30)
        if resp.status_code == 200:
            print(f"  OK: {len(resp.text)} chars van {url[:80]}")
            return resp.text
        else:
            print(f"  FAIL: status {resp.status_code} van {url[:80]}")
            return ""
    except Exception as e:
        print(f"  ERROR: {e}")
        return ""


def detect_options(text: str) -> list:
    """Detecteer belangrijke opties in tekst."""
    text_lower = text.lower()
    found = []
    for opt in IMPORTANT_OPTIONS:
        if opt.lower() in text_lower:
            found.append(opt)
    return list(set(found))


def classify_options(options: list) -> str:
    """Classificeer optieniveau: full, mid, basis."""
    has_pano = any(o in ["panorama", "pano", "glasdach"] for o in options)
    has_sport = any(o in ["s line", "s-line", "sline", "amg", "m sport", "m-sport", "msport"] for o in options)
    has_premium_audio = any(o in ["sonos", "b&o", "bang", "olufsen", "burmester", "harman", "kardon", "beats"] for o in options)
    has_camera = any(o in ["360", "surround", "topview", "top view", "area view"] for o in options)
    has_hud = any(o in ["head-up", "headup", "hud", "head up"] for o in options)
    has_keyless = any(o in ["keyless", "komfort", "kessy", "comfort access"] for o in options)
    has_matrix = any(o in ["matrix", "laser", "digital light"] for o in options)

    score = sum([has_pano, has_sport, has_premium_audio, has_camera, has_hud, has_keyless, has_matrix])

    if score >= 5:
        return "full"
    elif score >= 3:
        return "mid"
    else:
        return "basis"


def parse_price(text: str) -> int:
    """Parse prijs uit tekst."""
    text = text.replace(".", "").replace(",", "").replace(" ", "")
    m = re.search(r"€?\s*(\d{4,6})", text)
    if m:
        val = int(m.group(1))
        if 5000 < val < 200000:
            return val
    return 0


def parse_km(text: str) -> int:
    """Parse km uit tekst."""
    m = re.search(r"([\d.]+)\s*km", text, re.IGNORECASE)
    if m:
        km_str = m.group(1).replace(".", "")
        try:
            return int(km_str)
        except ValueError:
            pass
    return 0


def parse_year(text: str) -> int:
    """Parse bouwjaar uit tekst."""
    for pattern in [r"\b(202[0-9])\b", r"(\d{2})/(\d{4})", r"(\d{2})/(\d{2})"]:
        m = re.search(pattern, text)
        if m:
            if len(m.groups()) == 1:
                return int(m.group(1))
            else:
                year = int(m.group(2))
                if year < 100:
                    year += 2000
                if 2020 <= year <= 2026:
                    return year
    return 0


def parse_autoscout24(html: str) -> list:
    """Parse AutoScout24 zoekresultaten."""
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    # AutoScout24 gebruikt article elementen voor listings
    articles = soup.select("article, [data-testid='listing-entry'], .ListItem_wrapper__TxHWu, .cl-list-element")
    if not articles:
        # Fallback: zoek alle links naar /aanbod/
        articles = soup.select("a[href*='/aanbod/']")

    print(f"  Gevonden elementen: {len(articles)}")

    # Als geen articles, probeer de hele pagina te parsen
    if not articles:
        text = soup.get_text(separator="\n", strip=True)
        print(f"  Pagina tekst (eerste 2000 chars):\n{text[:2000]}")
        return listings

    for art in articles[:25]:
        try:
            full_text = art.get_text(separator=" ", strip=True)
            if len(full_text) < 20:
                continue

            listing = Listing()
            listing.raw_text = full_text[:500]

            # Titel
            title_el = art.select_one("h2, h3, .ListItem_title__ndA4s, [data-testid='listing-title']")
            if title_el:
                listing.title = title_el.get_text(strip=True)
            else:
                listing.title = full_text[:80]

            # Prijs
            price_el = art.select_one("[data-testid='price'], .ListItem_price__APlgs, .Price_price__APlgs")
            if price_el:
                listing.price = parse_price(price_el.get_text())
            if not listing.price:
                listing.price = parse_price(full_text)

            # Jaar en km uit tekst
            listing.year = parse_year(full_text)
            listing.km = parse_km(full_text)

            # Opties detecteren
            listing.options_found = detect_options(full_text)
            listing.option_score = classify_options(listing.options_found)

            if listing.price > 0:
                listings.append(listing)

        except Exception as e:
            print(f"  Parse error: {e}")
            continue

    return listings


def analyze_model(model_key: str, all_listings: list):
    """Analyseer listings per model en geef prijstabel."""
    print(f"\n{'='*70}")
    print(f"MODEL: {model_key}")
    print(f"{'='*70}")
    print(f"Totaal listings: {len(all_listings)}")

    if not all_listings:
        print("  Geen listings gevonden!")
        return {}

    # Print alle listings
    for i, lst in enumerate(all_listings, 1):
        opts = ", ".join(lst.options_found[:8]) if lst.options_found else "geen opties gedetecteerd"
        print(f"  {i:2d}. €{lst.price:>6,} | {lst.year} | {lst.km:>6,} km | [{lst.option_score:5s}] | {lst.title[:60]}")
        print(f"      Opties: {opts}")

    # Groepeer per jaar
    by_year = {}
    for lst in all_listings:
        if lst.year not in by_year:
            by_year[lst.year] = {"full": [], "mid": [], "basis": []}
        by_year[lst.year][lst.option_score].append(lst.price)

    print(f"\n  Prijsoverzicht per jaar/niveau:")
    print(f"  {'Jaar':>5} | {'Basis':>15} | {'Mid':>15} | {'Full':>15}")
    print(f"  {'-'*5} | {'-'*15} | {'-'*15} | {'-'*15}")

    result = {}
    for year in sorted(by_year.keys()):
        if year == 0:
            continue
        result[year] = {}
        for level in ["basis", "mid", "full"]:
            prices = by_year[year][level]
            if prices:
                avg = sum(prices) // len(prices)
                result[year][level] = avg
                lo, hi = min(prices), max(prices)
                print(f"  {year:>5} | " if level == "basis" else "       | ", end="")
                if level == "basis":
                    print(f"€{avg:>6,} ({len(prices)}x, {lo:,}-{hi:,})", end=" | ")
                elif level == "mid":
                    print(f"€{avg:>6,} ({len(prices)}x, {lo:,}-{hi:,})", end=" | ")
                else:
                    print(f"€{avg:>6,} ({len(prices)}x, {lo:,}-{hi:,})")
            else:
                if level == "basis":
                    print(f"  {year:>5} | {'—':>15} | ", end="")
                elif level == "mid":
                    print(f"{'—':>15} | ", end="")
                else:
                    print(f"{'—':>15}")

    return result


def main():
    print("=" * 70)
    print("NL MARKTPRIJS RESEARCH")
    print("=" * 70)
    print(f"Models: {len(SEARCH_URLS)}")
    print(f"Scrape.do credits geschat: {len(SEARCH_URLS)} (basic mode)")
    print()

    all_results = {}

    for model_key, urls in SEARCH_URLS.items():
        print(f"\n--- Scraping: {model_key} ---")
        model_listings = []

        for url_info in urls:
            url = url_info["url"]
            site = url_info["site"]
            print(f"  Fetching {site}: {url[:80]}...")

            html = scrape_do_fetch(url)
            if not html:
                continue

            time.sleep(1)  # even wachten tussen requests

            if site == "autoscout24":
                listings = parse_autoscout24(html)
            else:
                listings = []

            print(f"  Parsed: {len(listings)} listings")
            model_listings.extend(listings)

        result = analyze_model(model_key, model_listings)
        all_results[model_key] = result

    # Finale samenvatting als Python dict
    print("\n\n" + "=" * 70)
    print("PYTHON DICT VOOR SCRAPER (copy-paste):")
    print("=" * 70)
    print("NL_MARKET_PRICES = {")
    for model_key, years in all_results.items():
        print(f'    "{model_key}": {{')
        for year in sorted(years.keys()):
            levels = years[year]
            parts = []
            for level in ["basis", "mid", "full"]:
                if level in levels:
                    parts.append(f'"{level}": {levels[level]}')
            print(f"        {year}: {{{', '.join(parts)}}},")
        print("    },")
    print("}")


if __name__ == "__main__":
    main()
