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
        "gaspedaal": "https://www.gaspedaal.nl/audi/q3-sportback/hybride?bmin=2021&kmax=100000&trefw=pano&srt=df-a",
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

    # Methode 1: zoek __NEXT_DATA__ of inline JSON met listing data
    for script in soup.select("script"):
        script_text = script.string or ""
        if len(script_text) < 100:
            continue
        # Zoek JSON blobs met prijzen en auto-data
        if any(kw in script_text for kw in ['"price"', '"mileage"', '"kilometer"', '"prijs"', '"occasions"', '"results"', '"listings"']):
            print(f"  JSON script gevonden: {len(script_text):,} chars, type={script.get('type','')}, id={script.get('id','')}")
            try:
                data = json.loads(script_text)
                gp_listings = _extract_gaspedaal_json(data)
                if gp_listings:
                    print(f"  → {len(gp_listings)} listings uit JSON")
                    listings.extend(gp_listings)
            except json.JSONDecodeError:
                # Probeer embedded JSON objecten te vinden
                for m in re.finditer(r'\{[^{}]{200,}\}', script_text):
                    try:
                        obj = json.loads(m.group())
                        gp_listings = _extract_gaspedaal_json(obj)
                        listings.extend(gp_listings)
                    except (json.JSONDecodeError, ValueError):
                        pass

    # Methode 2: zoek <a> tags naar externe autosites met prijs erbij
    if not listings:
        external_links = soup.select("a[href*='autotrack'], a[href*='autoscout'], a[href*='viabovag'], a[href*='autoweek'], a[href*='occasion']")
        print(f"  Externe autosite links: {len(external_links)}")
        for link in external_links[:40]:
            parent = link.parent
            if parent:
                block = parent.parent if parent.parent else parent
                text = block.get_text(separator=" ", strip=True)
                if len(text) > 20:
                    price = parse_price(text)
                    if 10000 < price < 150000:
                        listing = Listing(source="gaspedaal")
                        listing.title = text[:100]
                        listing.price = price
                        listing.year = parse_year(text)
                        listing.km = parse_km(text)
                        listing.options_found = detect_options(text)
                        listing.option_score = classify_options(listing.options_found, listing.title)
                        listings.append(listing)

    # Methode 3: zoek alle elementen met € teken en context
    if not listings:
        price_elements = [el for el in soup.find_all(string=re.compile(r'€\s*[\d.]')) if el.parent]
        print(f"  Elementen met €: {len(price_elements)}")
        for el in price_elements[:50]:
            block = el.parent
            for _ in range(3):
                if block.parent and len(block.get_text(strip=True)) < 300:
                    block = block.parent
            text = block.get_text(separator=" ", strip=True)
            price = parse_price(text)
            if 10000 < price < 150000:
                listing = Listing(source="gaspedaal")
                listing.title = text[:100]
                listing.price = price
                listing.year = parse_year(text)
                listing.km = parse_km(text)
                listing.options_found = detect_options(text)
                listing.option_score = classify_options(listing.options_found, listing.title)
                listings.append(listing)

    # Methode 4: regex fallback op hele tekst
    if not listings:
        print("  Fallback: regex op tekst...")
        text = soup.get_text(separator="\n", strip=True)
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            price = parse_price(lines[i])
            if 10000 < price < 150000:
                context = " ".join(lines[max(0, i-5):min(len(lines), i+5)])
                listing = Listing(source="gaspedaal")
                listing.price = price
                listing.year = parse_year(context)
                listing.km = parse_km(context)
                listing.title = context[:100]
                listing.options_found = detect_options(context)
                listing.option_score = classify_options(listing.options_found, listing.title)
                if listing.year >= 2020:
                    listings.append(listing)
                i += 5
                continue
            i += 1

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
        # Dump HTML structuur voor debugging
        all_scripts = soup.select("script")
        print(f"  DEBUG: {len(all_scripts)} script tags, page size {len(html):,} chars")
        for i, s in enumerate(all_scripts[:10]):
            stype = s.get("type", "")
            sid = s.get("id", "")
            slen = len(s.string or "")
            preview = (s.string or "")[:100].replace("\n", " ")
            print(f"    script[{i}] type={stype} id={sid} len={slen} → {preview}")
        # Dump stuk van de HTML zelf (niet text) om structuur te zien
        print(f"  --- DEBUG HTML BODY (2000 chars) ---")
        body = soup.find("body")
        if body:
            body_str = str(body)[:2000]
            print(body_str)
        print(f"  --- EINDE DEBUG ---")

    return unique


def _extract_gaspedaal_json(data, depth=0) -> list:
    """Recursief listings uit Gaspedaal JSON data halen."""
    if depth > 5:
        return []
    listings = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                # Check of dit een auto-listing is
                has_price = any(k in item for k in ["price", "prijs", "askingPrice", "amount"])
                has_title = any(k in item for k in ["title", "name", "titel", "make", "brand"])
                if has_price and has_title:
                    listing = Listing(source="gaspedaal")
                    listing.title = str(item.get("title", item.get("name", item.get("titel", ""))))[:100]
                    for pk in ["price", "prijs", "askingPrice", "amount"]:
                        if pk in item:
                            val = item[pk]
                            if isinstance(val, dict):
                                listing.price = int(val.get("amount", val.get("value", 0)))
                            elif isinstance(val, (int, float)):
                                listing.price = int(val)
                            if listing.price > 0:
                                break
                    for yk in ["year", "jaar", "buildYear", "constructionYear", "registrationYear"]:
                        if yk in item:
                            listing.year = int(item[yk])
                            break
                    for mk in ["mileage", "km", "kilometer", "kilometres"]:
                        if mk in item:
                            val = item[mk]
                            listing.km = int(val) if isinstance(val, (int, float)) else parse_km(str(val))
                            break
                    full_text = json.dumps(item, ensure_ascii=False)
                    listing.options_found = detect_options(full_text)
                    listing.option_score = classify_options(listing.options_found, listing.title)
                    if listing.price > 10000:
                        listings.append(listing)
                else:
                    listings.extend(_extract_gaspedaal_json(item, depth + 1))
            elif isinstance(item, list):
                listings.extend(_extract_gaspedaal_json(item, depth + 1))

    elif isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, (dict, list)):
                listings.extend(_extract_gaspedaal_json(val, depth + 1))

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
