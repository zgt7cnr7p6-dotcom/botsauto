#!/usr/bin/env python3
"""Research NL marktprijzen via Gaspedaal + Marktplaats.

Scraped zoekpagina's via Scrape.do en parsed prijzen, km, bouwjaar en opties.
Output: gestructureerde prijstabel per model/jaar/optieniveau.

Gaspedaal = alles behalve Marktplaats (AutoTrack, AutoScout24, viaBOVAG, etc.)
Marktplaats = grootste NL platform

Samen = 100% NL marktdekking.

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


# Gaspedaal URLs — getest formaat
# Marktplaats URLs — zoekpagina met filters
SEARCH_URLS = {
    "q3_sportback_45_tfsi_e": {
        "gaspedaal": "https://www.gaspedaal.nl/audi-q3-sportback/45-tfsi-e",
        "marktplaats": "https://www.marktplaats.nl/l/auto-s/audi/q-q3+sportback+45+tfsi+e/",
    },
    "q5_50_tfsi_e": {
        "gaspedaal": "https://www.gaspedaal.nl/audi-q5/50-tfsi-e",
        "marktplaats": "https://www.marktplaats.nl/l/auto-s/audi/q-q5+50+tfsi+e/",
    },
    "mercedes_glc_300e": {
        "gaspedaal": "https://www.gaspedaal.nl/mercedes-benz-glc/300-e",
        "marktplaats": "https://www.marktplaats.nl/l/auto-s/mercedes-benz/q-glc+300+e+panorama/",
    },
    "mercedes_c_300e": {
        "gaspedaal": "https://www.gaspedaal.nl/mercedes-benz-c-klasse/300-e",
        "marktplaats": "https://www.marktplaats.nl/l/auto-s/mercedes-benz/q-c+300+e+amg/",
    },
    "bmw_330e": {
        "gaspedaal": "https://www.gaspedaal.nl/bmw-3-serie/330e",
        "marktplaats": "https://www.marktplaats.nl/l/auto-s/bmw/q-330e+m+sport/",
    },
}

IMPORTANT_OPTIONS = [
    "panorama", "pano", "glasdach", "panoramadak", "schuifdak",
    "s line", "s-line", "sline", "s edition", "amg", "amg line", "m sport", "m-sport", "msport", "m paket",
    "sonos", "b&o", "bang", "olufsen", "burmester", "harman", "kardon", "beats",
    "matrix", "laser", "digital light",
    "keyless", "komfortschlüssel", "kessy", "comfort access", "keyless-go",
    "360", "surround", "topview", "top view", "area view",
    "head-up", "headup", "hud", "head up",
    "acc", "distronic", "adaptive cruise", "afstandstempomat",
    "leder", "leather", "alcantara", "leer",
    "memory",
    "ahk", "anhänger", "trekhaak", "towbar",
    "luchtv", "airmatic", "air suspension", "luftfederung",
    "19\"", "20\"", "21\"", "19 inch", "20 inch", "21 inch",
    "night", "shadow", "optik pakket", "black style",
    "elektrische achterklep", "elektr. heckkl", "heckklappe",
    "ambient",
    "side assist", "dodehoek", "totwinkel",
    "stoelverwarming", "sitzheiz", "verwarmde stoel",
    "stuurverwarming", "lenkradheiz", "verwarmd stuur",
    "camera", "kamera", "achteruitrij",
    "travel assist",
    "lane assist", "spurhalte",
    "full option", "vol optie", "full", "compleet",
]


@dataclass
class Listing:
    title: str = ""
    price: int = 0
    year: int = 0
    km: int = 0
    options_found: list = field(default_factory=list)
    option_score: str = ""
    source: str = ""


def scrape_do_fetch(url: str, render: bool = False) -> str:
    """Fetch via Scrape.do."""
    params = {
        "token": SCRAPE_DO_TOKEN,
        "url": url,
        "geoCode": "nl",
    }
    if render:
        params["render"] = "true"

    api_url = "https://api.scrape.do?" + urllib.parse.urlencode(params)
    credits = 5 if render else 1
    try:
        resp = requests.get(api_url, timeout=45)
        if resp.status_code == 200:
            print(f"  [{credits}cr] OK: {len(resp.text):,} chars — {url[:80]}")
            return resp.text
        else:
            print(f"  [{credits}cr] FAIL: status {resp.status_code} — {url[:80]}")
            if resp.status_code in (403, 429, 502):
                print(f"  Response: {resp.text[:500]}")
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


def classify_options(options: list, title: str = "") -> str:
    """Classificeer optieniveau: full, mid, basis."""
    text = " ".join(options).lower() + " " + title.lower()

    has_pano = any(o in text for o in ["panorama", "pano", "glasdach", "panoramadak", "schuifdak"])
    has_sport = any(o in text for o in ["s line", "s-line", "sline", "s edition", "amg", "m sport", "m-sport", "m paket"])
    has_audio = any(o in text for o in ["sonos", "b&o", "bang", "olufsen", "burmester", "harman", "kardon", "beats"])
    has_cam360 = any(o in text for o in ["360", "surround", "topview", "area view"])
    has_hud = any(o in text for o in ["head-up", "headup", "hud", "head up"])
    has_keyless = any(o in text for o in ["keyless", "kessy", "comfort access", "keyless-go", "komfortschlüssel"])
    has_matrix = any(o in text for o in ["matrix", "laser", "digital light"])
    has_acc = any(o in text for o in ["acc", "distronic", "adaptive cruise", "afstandstempomat"])
    has_leather = any(o in text for o in ["leder", "leather", "alcantara", "leer"])

    score = sum([has_pano, has_sport, has_audio, has_cam360, has_hud, has_keyless, has_matrix, has_acc, has_leather])

    if score >= 6:
        return "full"
    elif score >= 3:
        return "mid"
    else:
        return "basis"


def parse_price(text: str) -> int:
    """Parse prijs uit tekst."""
    text = text.replace("\xa0", " ").replace(" ", " ")
    # €29.950 of € 29.950 of 29950
    m = re.search(r"€\s*([\d.]+)", text)
    if m:
        val_str = m.group(1).replace(".", "")
        try:
            val = int(val_str)
            if 5000 < val < 200000:
                return val
        except ValueError:
            pass
    return 0


def parse_km(text: str) -> int:
    """Parse km uit tekst."""
    m = re.search(r"([\d.]+)\s*km", text, re.IGNORECASE)
    if m:
        km_str = m.group(1).replace(".", "")
        try:
            val = int(km_str)
            if val < 500000:
                return val
        except ValueError:
            pass
    return 0


def parse_year(text: str) -> int:
    """Parse bouwjaar uit tekst."""
    # Zoek 4-digit jaar
    for m in re.finditer(r"\b(202[0-6])\b", text):
        return int(m.group(1))
    # mm/yyyy
    m = re.search(r"(\d{2})[/-](202[0-6])", text)
    if m:
        return int(m.group(2))
    return 0


def parse_gaspedaal(html: str) -> list:
    """Parse Gaspedaal zoekresultaten."""
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    # Debug: toon page title
    title = soup.title.string if soup.title else "geen titel"
    print(f"  Page title: {title}")

    # Gaspedaal listing cards
    cards = soup.select(".listing, .result, article, [class*='listing'], [class*='result'], [class*='car-card'], [class*='vehicle']")
    print(f"  Cards via CSS selectors: {len(cards)}")

    if cards:
        for card in cards[:30]:
            try:
                text = card.get_text(separator=" ", strip=True)
                if len(text) < 15:
                    continue

                listing = Listing(source="gaspedaal")
                listing.title = text[:100]
                listing.price = parse_price(text)
                listing.year = parse_year(text)
                listing.km = parse_km(text)
                listing.options_found = detect_options(text)
                listing.option_score = classify_options(listing.options_found, listing.title)

                if listing.price > 10000:
                    listings.append(listing)
            except Exception as e:
                print(f"  Card parse error: {e}")

    # Fallback: regex op hele pagina
    if len(listings) < 3:
        print("  Fallback: regex parsing...")
        text = soup.get_text(separator="\n", strip=True)

        # Zoek blokken met prijzen
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            price = parse_price(line)
            if 10000 < price < 100000:
                # Pak context: 5 regels ervoor en erna
                context_lines = lines[max(0, i-5):min(len(lines), i+5)]
                context = " ".join(context_lines)

                listing = Listing(source="gaspedaal")
                listing.price = price
                listing.year = parse_year(context)
                listing.km = parse_km(context)
                listing.title = context[:100]
                listing.options_found = detect_options(context)
                listing.option_score = classify_options(listing.options_found, listing.title)

                if listing.year >= 2021:
                    listings.append(listing)
                    i += 5
                    continue
            i += 1

    # Dedup op prijs
    seen = set()
    unique = []
    for lst in listings:
        key = (lst.price, lst.year)
        if key not in seen:
            seen.add(key)
            unique.append(lst)

    print(f"  Totaal na dedup: {len(unique)}")

    # Debug eerste 3000 chars als weinig resultaten
    if len(unique) < 3:
        text = soup.get_text(separator="\n", strip=True)
        print(f"  --- DEBUG TEKST (3000 chars) ---")
        print(text[:3000])
        print(f"  --- EINDE DEBUG ---")

    return unique


def parse_marktplaats(html: str) -> list:
    """Parse Marktplaats zoekresultaten."""
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    title = soup.title.string if soup.title else "geen titel"
    print(f"  Page title: {title}")

    # Marktplaats listing cards
    cards = soup.select("[class*='Listing'], [class*='listing'], article, [data-testid*='listing']")
    print(f"  Cards via CSS selectors: {len(cards)}")

    # Probeer ook JSON-LD
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and "offers" in str(data):
                print(f"  JSON-LD gevonden: {list(data.keys())[:10]}")
        except (json.JSONDecodeError, TypeError):
            pass

    # Zoek __NEXT_DATA__
    next_data = soup.select_one("script#__NEXT_DATA__")
    if next_data:
        try:
            data = json.loads(next_data.string)
            props = data.get("props", {}).get("pageProps", {})
            # Zoek listings in de data
            print(f"  __NEXT_DATA__ pageProps keys: {list(props.keys())[:15]}")

            # Marktplaats stopt listings vaak in searchRequestResult of listings
            for key in ["searchRequestResult", "listings", "items", "results", "data"]:
                if key in props:
                    val = props[key]
                    if isinstance(val, dict):
                        print(f"    {key} keys: {list(val.keys())[:15]}")
                        # Zoek listings in nested dict
                        for k2 in ["listings", "items", "results", "ads"]:
                            if k2 in val and isinstance(val[k2], list):
                                listing_data = val[k2]
                                print(f"    {key}.{k2}: {len(listing_data)} items")
                                for item in listing_data[:25]:
                                    if not isinstance(item, dict):
                                        continue
                                    listing = Listing(source="marktplaats")
                                    listing.title = str(item.get("title", item.get("name", "")))[:100]
                                    # Prijs
                                    price_info = item.get("priceInfo", item.get("price", {}))
                                    if isinstance(price_info, dict):
                                        listing.price = int(price_info.get("priceCents", 0)) // 100
                                        if not listing.price:
                                            listing.price = int(price_info.get("amount", 0))
                                    elif isinstance(price_info, (int, float)):
                                        listing.price = int(price_info)

                                    # Attributen
                                    attrs = item.get("attributes", item.get("specs", []))
                                    attrs_text = json.dumps(attrs, ensure_ascii=False) if attrs else ""

                                    listing.year = parse_year(attrs_text + " " + listing.title)
                                    listing.km = parse_km(attrs_text)

                                    full_text = listing.title + " " + attrs_text + " " + json.dumps(item.get("description", ""), ensure_ascii=False)
                                    listing.options_found = detect_options(full_text)
                                    listing.option_score = classify_options(listing.options_found, listing.title)

                                    if listing.price > 10000:
                                        listings.append(listing)
                                if listings:
                                    return listings
                    elif isinstance(val, list) and len(val) > 0:
                        print(f"    {key}: list[{len(val)}]")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"  __NEXT_DATA__ error: {e}")

    # Fallback: cards of regex
    if cards and not listings:
        for card in cards[:30]:
            text = card.get_text(separator=" ", strip=True)
            if len(text) < 15:
                continue
            listing = Listing(source="marktplaats")
            listing.title = text[:100]
            listing.price = parse_price(text)
            listing.year = parse_year(text)
            listing.km = parse_km(text)
            listing.options_found = detect_options(text)
            listing.option_score = classify_options(listing.options_found, listing.title)
            if listing.price > 10000:
                listings.append(listing)

    if not listings:
        text = soup.get_text(separator="\n", strip=True)
        print(f"  --- DEBUG TEKST (3000 chars) ---")
        print(text[:3000])
        print(f"  --- EINDE DEBUG ---")

    return listings


def analyze_model(model_key: str, all_listings: list):
    """Analyseer listings per model."""
    print(f"\n{'='*70}")
    print(f"MODEL: {model_key} ({len(all_listings)} listings)")
    print(f"{'='*70}")

    if not all_listings:
        print("  GEEN LISTINGS!")
        return {}

    # Sort by price
    all_listings.sort(key=lambda x: x.price)

    for i, lst in enumerate(all_listings, 1):
        opts = ", ".join(lst.options_found[:6]) if lst.options_found else "-"
        print(f"  {i:2d}. €{lst.price:>6,} | {lst.year} | {lst.km:>6,} km | [{lst.option_score:5s}] | {lst.source:11s} | {lst.title[:50]}")
        print(f"      Opties: {opts}")

    # Groepeer per jaar
    by_year = {}
    for lst in all_listings:
        if lst.year < 2021:
            continue
        if lst.year not in by_year:
            by_year[lst.year] = {"full": [], "mid": [], "basis": []}
        by_year[lst.year][lst.option_score].append(lst.price)

    print(f"\n  SAMENVATTING:")
    print(f"  {'Jaar':>5} | {'Basis':>20} | {'Mid':>20} | {'Full option':>20}")
    print(f"  {'-'*5}-+-{'-'*20}-+-{'-'*20}-+-{'-'*20}")

    result = {}
    for year in sorted(by_year.keys()):
        result[year] = {}
        row = f"  {year:>5} |"
        for level in ["basis", "mid", "full"]:
            prices = by_year[year][level]
            if prices:
                avg = sum(prices) // len(prices)
                result[year][level] = avg
                lo, hi = min(prices), max(prices)
                row += f" €{avg:>6,} ({len(prices):>2}x €{lo:,}-{hi:,}) |"
            else:
                row += f" {'—':>20} |"
        print(row)

    # Gemiddelden over alle jaren
    all_prices = [lst.price for lst in all_listings if lst.year >= 2021]
    if all_prices:
        print(f"\n  Totaal: {len(all_prices)} listings, mediaan €{sorted(all_prices)[len(all_prices)//2]:,}, "
              f"gem €{sum(all_prices)//len(all_prices):,}, range €{min(all_prices):,} - €{max(all_prices):,}")

    return result


def main():
    print("=" * 70)
    print("NL MARKTPRIJS RESEARCH — Gaspedaal + Marktplaats")
    print("=" * 70)
    total_requests = sum(len(urls) for urls in SEARCH_URLS.values())
    print(f"Models: {len(SEARCH_URLS)}, requests: {total_requests}")
    print(f"Credits: ~{total_requests} (basic mode, 1 credit per request)")
    print()

    all_results = {}

    for model_key, urls in SEARCH_URLS.items():
        print(f"\n{'─'*70}")
        print(f"SCRAPING: {model_key}")
        print(f"{'─'*70}")
        model_listings = []

        # Gaspedaal
        if "gaspedaal" in urls:
            print(f"\n  [Gaspedaal]")
            html = scrape_do_fetch(urls["gaspedaal"])
            if html:
                listings = parse_gaspedaal(html)
                print(f"  → {len(listings)} listings van Gaspedaal")
                model_listings.extend(listings)
            time.sleep(1)

        # Marktplaats
        if "marktplaats" in urls:
            print(f"\n  [Marktplaats]")
            html = scrape_do_fetch(urls["marktplaats"])
            if html:
                listings = parse_marktplaats(html)
                print(f"  → {len(listings)} listings van Marktplaats")
                model_listings.extend(listings)
            time.sleep(1)

        result = analyze_model(model_key, model_listings)
        all_results[model_key] = result

    # Finale output
    print("\n\n" + "=" * 70)
    print("PYTHON DICT VOOR SCRAPER:")
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
            if parts:
                print(f"        {year}: {{{', '.join(parts)}}},")
        print("    },")
    print("}")


if __name__ == "__main__":
    main()
