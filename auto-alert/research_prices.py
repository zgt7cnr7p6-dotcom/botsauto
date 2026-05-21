#!/usr/bin/env python3
"""Research NL marktprijzen via Gaspedaal.

Scraped zoekpagina's via Scrape.do (render=true) en parsed prijzen, km, bouwjaar en opties.
Output: gestructureerde prijstabel per model/jaar/optieniveau.

Gaspedaal = meta-zoekmachine die alle NL autosites aggregeert
(AutoTrack, AutoScout24, viaBOVAG, Autoweek, etc. — alles behalve Marktplaats)

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


# Gaspedaal: /merk/model/brandstof?bmin=2021&kmax=100000&trefw=pano&srt=df-a
# Alleen Gaspedaal — aggregeert alle NL autosites behalve Marktplaats
SEARCH_URLS = {
    "q3_sportback_45_tfsi_e": {
        "gaspedaal": "https://www.gaspedaal.nl/audi/q3/hybride?bmin=2021&kmax=100000&trefw=pano&srt=df-a",
        "gaspedaal_sportback": "https://www.gaspedaal.nl/audi/q3-sportback/hybride?bmin=2021&kmax=100000&trefw=pano&srt=df-a",
    },
    "q5_50_tfsi_e": {
        "gaspedaal": "https://www.gaspedaal.nl/audi/q5/hybride?bmin=2021&kmax=100000&trefw=pano&srt=df-a",
        "gaspedaal_sportback": "https://www.gaspedaal.nl/audi/q5-sportback/hybride?bmin=2021&kmax=100000&trefw=pano&srt=df-a",
    },
    "mercedes_glc_300e": {
        "gaspedaal": "https://www.gaspedaal.nl/mercedes-benz/hybride?model=2814,5595&bmin=2021&kmax=100000&trefw=pano&srt=df-a",
    },
    "mercedes_c_300e": {
        "gaspedaal": "https://www.gaspedaal.nl/mercedes-benz/c-klasse/hybride?bmin=2021&kmax=100000&trefw=pano&srt=df-a",
        "gaspedaal_estate": "https://www.gaspedaal.nl/mercedes-benz/c-klasse-estate/hybride?bmin=2021&kmax=100000&trefw=pano&srt=df-a",
    },
    "bmw_330e": {
        "gaspedaal": "https://www.gaspedaal.nl/bmw/3-serie/hybride?bmin=2021&kmax=100000&trefw=pano&srt=df-a",
        "gaspedaal_touring": "https://www.gaspedaal.nl/bmw/3-serie-touring/hybride?bmin=2021&kmax=100000&trefw=pano&srt=df-a",
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
            if resp.status_code in (403, 404, 429, 502):
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

    title = soup.title.string if soup.title else "geen titel"
    print(f"  Page title: {title}")

    # Methode 1: JSON-LD schema.org data (application/ld+json)
    for script in soup.select("script[type='application/ld+json']"):
        script_text = script.string or ""
        if len(script_text) < 500:
            continue
        print(f"  JSON-LD gevonden: {len(script_text):,} chars")
        try:
            data = json.loads(script_text)
            ld_listings = _parse_jsonld(data)
            if ld_listings:
                print(f"  → {len(ld_listings)} listings uit JSON-LD")
                listings.extend(ld_listings)
        except json.JSONDecodeError:
            pass

    # Methode 2: zoek in Next.js RSC payloads (self.__next_f.push)
    if not listings:
        for script in soup.select("script"):
            script_text = script.string or ""
            if "self.__next_f.push" not in script_text:
                continue
            # Extract JSON strings embedded in RSC payloads
            for m in re.finditer(r'"@type"\s*:\s*"Car"', script_text):
                start = script_text.rfind("{", 0, m.start())
                if start < 0:
                    continue
                # Find matching closing brace
                depth_count = 0
                end = start
                for ci, ch in enumerate(script_text[start:], start):
                    if ch == "{":
                        depth_count += 1
                    elif ch == "}":
                        depth_count -= 1
                        if depth_count == 0:
                            end = ci + 1
                            break
                if end > start:
                    try:
                        car_json = script_text[start:end]
                        # Unescape JSON strings (RSC double-escapes)
                        car_json = car_json.replace('\\"', '"').replace('\\\\', '\\')
                        car_data = json.loads(car_json)
                        ld_listings = _parse_jsonld(car_data)
                        listings.extend(ld_listings)
                    except (json.JSONDecodeError, ValueError):
                        pass

    # Dedup
    seen = set()
    unique = []
    for lst in listings:
        key = (lst.price, lst.year, lst.km)
        if key not in seen:
            seen.add(key)
            unique.append(lst)

    print(f"  Totaal na dedup: {len(unique)}")

    if len(unique) < 3:
        # Debug: toon JSON-LD preview
        for script in soup.select("script[type='application/ld+json']"):
            script_text = script.string or ""
            if len(script_text) > 500:
                print(f"  --- DEBUG JSON-LD (eerste 2000 chars) ---")
                print(script_text[:2000])
                print(f"  --- EINDE DEBUG ---")
                break

    return unique


def _parse_jsonld(data) -> list:
    """Parse schema.org JSON-LD auto listings."""
    listings = []

    if isinstance(data, list):
        for item in data:
            listings.extend(_parse_jsonld(item))
        return listings

    if not isinstance(data, dict):
        return []

    item_type = data.get("@type", "")

    # ItemList met itemListElement
    if item_type == "ItemList" or "itemListElement" in data:
        elements = data.get("itemListElement", [])
        print(f"  ItemList: {len(elements)} elementen")
        for elem in elements:
            item = elem.get("item", elem)
            listings.extend(_parse_jsonld(item))
        return listings

    # Car of Vehicle
    if item_type in ("Car", "Vehicle", "Product", "Offer"):
        listing = _parse_car_jsonld(data)
        if listing and listing.price > 10000:
            listings.append(listing)
            if len(listings) == 1:
                # Debug: toon structuur van eerste auto
                print(f"  Eerste Car object keys: {list(data.keys())}")
                print(f"  → title='{listing.title}', price={listing.price}, year={listing.year}, km={listing.km}")
                # Toon eerste 500 chars van het object
                preview = json.dumps(data, ensure_ascii=False)[:500]
                print(f"  → {preview}")
        return listings

    # Recursief zoeken in nested dicts
    for key, val in data.items():
        if isinstance(val, (dict, list)) and key not in ("@context",):
            listings.extend(_parse_jsonld(val))

    return listings


def _parse_car_jsonld(car: dict) -> Listing:
    """Parse een schema.org Car/Vehicle object."""
    listing = Listing(source="gaspedaal")

    listing.title = str(car.get("name", car.get("model", "")))[:100]

    # Prijs uit offers
    offers = car.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict):
        price_val = offers.get("price", offers.get("priceSpecification", {}).get("price", 0))
        try:
            listing.price = int(float(str(price_val).replace(",", "").replace(".", "")))
            if listing.price > 1000000:
                listing.price = listing.price // 100
        except (ValueError, TypeError):
            listing.price = 0
    # Prijs direct op item
    if not listing.price and "price" in car:
        try:
            listing.price = int(float(str(car["price"]).replace(",", "").replace(".", "")))
        except (ValueError, TypeError):
            pass

    # Bouwjaar
    for yk in ["vehicleModelDate", "modelDate", "productionDate", "dateVehicleFirstRegistered"]:
        if yk in car:
            yr = parse_year(str(car[yk]))
            if yr:
                listing.year = yr
                break

    # Kilometerstand
    mileage = car.get("mileageFromOdometer", {})
    if isinstance(mileage, dict):
        km_val = mileage.get("value", 0)
        try:
            listing.km = int(float(str(km_val).replace(".", "").replace(",", "")))
        except (ValueError, TypeError):
            pass
    elif "mileageFromOdometer" in car:
        listing.km = parse_km(str(car["mileageFromOdometer"]))

    # Opties uit description, name en andere velden
    desc = str(car.get("description", ""))
    full_text = listing.title + " " + desc + " " + json.dumps(car, ensure_ascii=False)
    listing.options_found = detect_options(full_text)
    listing.option_score = classify_options(listing.options_found, listing.title)

    return listing



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
    print("NL MARKTPRIJS RESEARCH — Gaspedaal")
    print("=" * 70)
    total_urls = sum(len(urls) for urls in SEARCH_URLS.values())
    print(f"Models: {len(SEARCH_URLS)}, URLs: {total_urls}")
    print(f"Credits: ~{total_urls * 5} (render=true, 5 credits per URL)")
    print()

    all_results = {}

    for model_key, urls in SEARCH_URLS.items():
        print(f"\n{'─'*70}")
        print(f"SCRAPING: {model_key}")
        print(f"{'─'*70}")
        model_listings = []

        # Gaspedaal (JS-rendered meta-zoekmachine, render=true nodig)
        for url_key in [k for k in urls if k.startswith("gaspedaal")]:
            label = url_key.replace("gaspedaal_", "Gaspedaal ") if "_" in url_key else "Gaspedaal"
            print(f"\n  [{label}]")
            html = scrape_do_fetch(urls[url_key], render=True)
            if html:
                listings = parse_gaspedaal(html)
                print(f"  → {len(listings)} listings van {label}")
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
