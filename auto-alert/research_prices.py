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

    # Gaspedaal body tekst formaat per listing:
    # <prijs>  (bijv. "37.900" of "€ 37.900")
    # <titel>  (bijv. "Audi Q3 - 45 TFSI e S edition | PANO | ...")
    # Bouwjaar:
    # <jaar>   (bijv. "2022")
    # Km.stand:
    # <km>     (bijv. "55.456")
    # km
    body = soup.find("body")
    if not body:
        return []

    body_text = body.get_text(separator="\n", strip=True)
    lines = body_text.split("\n")
    print(f"  Body: {len(lines)} regels, {len(body_text):,} chars")

    i = 0
    while i < len(lines) - 5:
        line = lines[i].strip()

        # Zoek prijsregel: getal in formaat XX.XXX (NL notatie, punt als scheidingsteken)
        price = 0
        # Probeer met €
        price = parse_price(line)
        # Probeer zonder € — raw getal als XX.XXX
        if not price:
            m = re.match(r"^([\d.]+)$", line)
            if m:
                val_str = m.group(1).replace(".", "")
                try:
                    val = int(val_str)
                    if 10000 < val < 200000:
                        price = val
                except ValueError:
                    pass

        if not price or price < 10000 or price > 150000:
            i += 1
            continue

        # Gevonden prijs — pak de volgende regels als context
        # Titel = volgende niet-lege regel (vaak merk + model + opties)
        title = ""
        year = 0
        km = 0

        context_lines = lines[i+1:min(i+25, len(lines))]
        context = "\n".join(context_lines)

        # Titel: eerste substantiële regel na de prijs
        for cl in context_lines[:5]:
            cl = cl.strip()
            if len(cl) > 15 and not cl.startswith("Bouwjaar") and not cl.startswith("Km"):
                title = cl[:120]
                break

        # Bouwjaar: zoek "Bouwjaar:" gevolgd door jaargetal
        for j, cl in enumerate(context_lines):
            if "Bouwjaar" in cl:
                for next_line in context_lines[j:j+3]:
                    yr = parse_year(next_line)
                    if yr:
                        year = yr
                        break
                break

        # Km: zoek "Km.stand:" of "Km.stand" gevolgd door getal
        for j, cl in enumerate(context_lines):
            if "Km" in cl and "stand" in cl.lower():
                for next_line in context_lines[j:j+3]:
                    km_val = parse_km(next_line)
                    if km_val:
                        km = km_val
                        break
                break

        listing = Listing(source="gaspedaal")
        listing.price = price
        listing.title = title
        listing.year = year
        listing.km = km
        listing.options_found = detect_options(title + " " + context)
        listing.option_score = classify_options(listing.options_found, listing.title)
        listings.append(listing)

        # Skip voorbij deze listing
        i += 15
        continue

    print(f"  Listings gevonden: {len(listings)}")

    # Dedup op prijs+km
    seen = set()
    unique = []
    for lst in listings:
        key = (lst.price, lst.km)
        if key not in seen:
            seen.add(key)
            unique.append(lst)

    print(f"  Na dedup: {len(unique)}")
    return unique




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

    # Groepeer per jaar (als beschikbaar)
    by_year = {}
    no_year = []
    for lst in all_listings:
        if lst.year >= 2021:
            if lst.year not in by_year:
                by_year[lst.year] = []
            by_year[lst.year].append(lst.price)
        else:
            no_year.append(lst.price)

    if by_year:
        print(f"\n  PER JAAR:")
        for year in sorted(by_year.keys()):
            prices = sorted(by_year[year])
            median = prices[len(prices) // 2]
            print(f"    {year}: {len(prices)}x, mediaan €{median:,}, range €{min(prices):,} - €{max(prices):,}")

    if no_year:
        print(f"  Zonder jaar: {len(no_year)}x")

    # Alle prijzen (URL filtert al op bmin=2021)
    all_prices = sorted([lst.price for lst in all_listings])
    median = all_prices[len(all_prices) // 2]
    p25 = all_prices[len(all_prices) // 4]
    p75 = all_prices[3 * len(all_prices) // 4]
    avg = sum(all_prices) // len(all_prices)

    print(f"\n  TOTAAL: {len(all_prices)} listings")
    print(f"  Mediaan:  €{median:,}")
    print(f"  Gem:      €{avg:,}")
    print(f"  P25-P75:  €{p25:,} - €{p75:,}")
    print(f"  Range:    €{min(all_prices):,} - €{max(all_prices):,}")

    result = {
        "count": len(all_prices),
        "median": median,
        "avg": avg,
        "p25": p25,
        "p75": p75,
        "min": min(all_prices),
        "max": max(all_prices),
        "by_year": {y: sorted(p)[len(p)//2] for y, p in by_year.items()},
    }
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
    print("PYTHON DICT VOOR SCRAPER — NL MARKTWAARDEN")
    print("=" * 70)
    print("NL_MARKET_PRICES = {")
    for model_key, data in all_results.items():
        if not data:
            print(f'    "{model_key}": {{"median": 0, "count": 0}},')
            continue
        by_year_str = ", ".join(f"{y}: {p}" for y, p in sorted(data.get("by_year", {}).items()))
        print(f'    "{model_key}": {{"median": {data["median"]}, "p25": {data["p25"]}, "p75": {data["p75"]}, "count": {data["count"]}, "by_year": {{{by_year_str}}}}},')
    print("}")


if __name__ == "__main__":
    main()
