#!/usr/bin/env python3
"""NL-marktprijzen via Gaspedaal — live database i.p.v. statische tabel.

Wat dit doet:
  * 1x per dag alle NL-auto's (onze modellen, hybride, automaat, panoramadak)
    van Gaspedaal ophalen en ALLES opslaan in tabel `nl_listings` (in de
    bestaande listings.db).
  * Bij een alert: de goedkoopste vergelijkbare NL-auto opzoeken -> "NL vanaf"
    + marge, plus een klikbare Gaspedaal-zoeklink met exact dezelfde filters.

Filters (afgestemd met eigenaar):
  * brandstof = hybride (in het pad)
  * bouwjaar exact bij de query (bmin=bmax=jaar in de link)
  * km-stand: auto's km + 20.000 als plafond
  * transmissie = AUTOMATISCH  (trns=AUTOMATISCH)
  * panoramadak = Gaspedaal optie-filter opt=361
  * sortering prijs oplopend (srt=pr-a)

De brede dag-scrape haalt bmin=2021 (alle km) op zodat we lokaal per auto
kunnen filteren zonder opnieuw te scrapen.

Gebruik als los script (test):  python nl_prices.py [--force]
Vereist dan SCRAPE_DO_TOKEN + import van scrape_do_fetch uit scraper.py.
"""

import re
import json
import hashlib
import logging
import sqlite3
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup

log = logging.getLogger("nl_prices")

# Gaspedaal optie-id voor panoramadak (uit echte filter-URL van eigenaar)
GASPEDAAL_PANO_OPT = "361"
GASPEDAAL_BASE = "https://www.gaspedaal.nl"

# Vaste basis-filters voor elke Gaspedaal-URL
_YEAR_MIN = 2021          # onze zoekondergrens
_KM_MARGIN = 20_000       # NL-auto mag tot (Duitse km + dit) hebben

# model_key (uit scraper.send_telegram) -> (gaspedaal merk/model-slug, body_type-filter)
# body_type "" = geen bodyfilter; anders wordt binnen dezelfde slug op body_type gefilterd.
# Slugs met (?) zijn nog te verifieren tijdens de eerste testrun (0 resultaten = fout).
GASPEDAAL_BY_MODEL = {
    "q3":              ("audi/q3",               ""),
    "q3_sportback":    ("audi/q3-sportback",     ""),
    "q5":              ("audi/q5",               ""),
    "q5_sportback":    ("audi/q5-sportback",     ""),
    "q8":              ("audi/q8",               ""),          # (?)
    "q8_sportback":    ("audi/q8",               "sportback"), # (?) zelfde slug, bodyfilter
    "a3":              ("audi/a3",               ""),          # (?)
    "a4":              ("audi/a4",               "sedan"),     # (?)
    "a4_avant":        ("audi/a4",               "touring"),   # (?) avant ~ touring
    "c_klasse_sedan":  ("mercedes-benz/c-klasse", "sedan"),
    "c_klasse_touring":("mercedes-benz/c-klasse", "touring"),
    "glc":             ("mercedes-benz/glc-klasse", ""),      # glc (niet /glc) = juiste slug
    "cla":             ("mercedes-benz/cla-klasse", ""),
    "e_klasse_sedan":  ("mercedes-benz/e-klasse", "sedan"),
    "e_klasse_touring":("mercedes-benz/e-klasse", "touring"),
    "330e_sedan":      ("bmw/3-serie",           "sedan"),     # gedeelde slug + bodyfilter
    "330e_touring":    ("bmw/3-serie",           "touring"),   # (3-serie-touring bestaat niet)
    "formentor":       ("cupra/formentor",       ""),          # (?)
}


# ── Database ────────────────────────────────────────────────────────────────


def init_nl_db(conn):
    """Maak de NL-tabellen aan (in de bestaande listings.db)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nl_listings (
            id TEXT PRIMARY KEY,
            model_slug TEXT,
            merk TEXT,
            model TEXT,
            title TEXT,
            price INTEGER,
            year INTEGER,
            km INTEGER,
            transmissie TEXT,
            brandstof TEXT,
            body_type TEXT,
            pano INTEGER,
            options TEXT,
            source_site TEXT,
            url TEXT,
            raw TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
        """
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS nl_meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nl_lookup ON nl_listings (model_slug, year, km, pano)"
    )
    conn.commit()


def _get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM nl_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO nl_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def _stable_id(model_slug, year, km, price, title):
    raw = f"{model_slug}|{year}|{km}|{price}|{title[:40]}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


# ── Parsing ─────────────────────────────────────────────────────────────────

# Opties die we in de titel/tekst herkennen (breed; puur ter info-opslag)
_NL_OPTIONS = [
    "panorama", "pano", "panoramadak", "schuifdak", "glasdach",
    "s line", "s-line", "sline", "s edition", "amg", "amg line", "m sport",
    "m-sport", "msport", "vz", "night", "shadow", "black",
    "sonos", "b&o", "bang", "olufsen", "burmester", "harman", "kardon", "beats",
    "matrix", "laser", "digital light",
    "keyless", "kessy", "comfort access", "keyless-go", "komfortschlüssel",
    "360", "surround", "camera", "achteruitrij",
    "head-up", "headup", "hud",
    "acc", "distronic", "adaptive cruise",
    "leder", "leer", "alcantara", "memory",
    "trekhaak", "ahk", "luchtvering", "airmatic",
    "19", "20", "21", "stoelverwarming", "stuurverwarming", "ambient",
    "elektrische achterklep", "travel assist", "lane assist",
]


def _parse_price(text):
    text = text.replace("\xa0", " ")
    m = re.search(r"€?\s*([\d.]{5,})", text)
    if m:
        val = m.group(1).replace(".", "")
        try:
            v = int(val)
            if 5000 < v < 200000:
                return v
        except ValueError:
            pass
    return 0


def _parse_year(text):
    m = re.search(r"\b(20[12]\d)\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\b\d{2}[/-](20[12]\d)\b", text)
    if m:
        return int(m.group(1))
    return 0


def _detect_body(text):
    t = text.lower()
    if "sportback" in t:
        return "sportback"
    if any(w in t for w in ["touring", "estate", "avant", "kombi", "stationwagon", "station"]):
        return "touring"
    if any(w in t for w in ["sedan", "limousine", "berline"]):
        return "sedan"
    if any(w in t for w in ["suv", "coupé", "coupe"]):
        return "suv"
    return ""


def _detect_options(text):
    t = text.lower()
    return sorted({opt for opt in _NL_OPTIONS if opt in t})


def _detect_source_site(text):
    """Probeer te zien van welke NL-site de listing komt (indien vermeld)."""
    t = text.lower()
    for site in ["autoscout24", "autotrack", "viabovag", "bovag", "autoweek",
                 "gaspedaal", "marktplaats", "anwb", "autowereld"]:
        if site in t:
            return site
    return ""


def _norm_body(body_type, name=""):
    """Normaliseer Gaspedaal bodyType/naam -> sedan/touring/sportback/suv/''."""
    t = f"{body_type} {name}".lower()
    if "sportback" in t:
        return "sportback"
    if any(w in t for w in ["station", "combi", "touring", "avant", "estate", "kombi"]):
        return "touring"
    if any(w in t for w in ["sedan", "limousine", "berline"]):
        return "sedan"
    if any(w in t for w in ["suv", "terreinwagen", "terrein"]):
        return "suv"
    return ""


def _source_from_image(image_url):
    """Haal de bron-site uit de Gaspedaal-image (?source=https://cdn.autowereld.nl/..)."""
    if not image_url:
        return ""
    m = re.search(r"[?&]source=https?://([^/&]+)", image_url)
    host = m.group(1) if m else ""
    return host.replace("cdn.", "").replace("www.", "").replace("i.", "")


def _first_ld_itemlist(html):
    """Pak het ItemList JSON-LD blok uit de pagina (de zoekresultaten)."""
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL):
        chunk = m.group(1).strip()
        if '"ItemList"' not in chunk:
            continue
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if data.get("@type") == "ItemList":
            return data
    return None


def _breadcrumb(html):
    """Namen uit de BreadcrumbList JSON-LD (bijv. ['Gaspedaal.nl','Audi','Q5','Hybride'])."""
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL):
        chunk = m.group(1)
        if '"BreadcrumbList"' not in chunk:
            continue
        try:
            data = json.loads(chunk.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        return [i.get("item", {}).get("name", "") for i in data.get("itemListElement", [])]
    return []


def parse_gaspedaal(html, model_slug):
    """Parse een Gaspedaal-zoekpagina -> lijst dicts met ALLE info per listing.

    Primair via de JSON-LD `ItemList` (schoon + volledig: prijs, jaar, km,
    carrosserie, transmissie, kleur, vermogen, foto, bron-site, detail-id).
    Valt terug op de tekst-parser als de JSON-LD ontbreekt.

    VANGNET: herkent Gaspedaal niet de slug, dan valt hij terug op een
    merk-brede lijst (breadcrump = alleen 'Gaspedaal.nl'). Die pagina bevat
    verkeerde modellen -> we slaan hem over i.p.v. rommel op te slaan.
    """
    crumbs = _breadcrumb(html)
    if len(crumbs) < 3:
        log.warning("NL: slug %s viel terug op merk-breed (breadcrumb=%s) — OVERGESLAGEN. "
                    "Slug controleren.", model_slug, crumbs or "leeg")
        return []

    data = _first_ld_itemlist(html)
    if not data:
        log.warning("NL: geen JSON-LD ItemList voor %s — val terug op tekst-parser", model_slug)
        return _parse_gaspedaal_text(html, model_slug)

    merk = model_slug.split("/")[0]
    model = model_slug.split("/")[-1]
    results = []
    for el in data.get("itemListElement", []):
        it = el.get("item", {})
        if not isinstance(it, dict):
            continue

        offers = it.get("offers") or {}
        try:
            price = int(offers.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        if price <= 0:
            continue

        try:
            year = int(it.get("productionDate") or it.get("vehicleModelDate") or 0)
        except (TypeError, ValueError):
            year = 0

        km = 0
        mo = it.get("mileageFromOdometer") or {}
        if isinstance(mo, dict):
            try:
                km = int(mo.get("value") or 0)
            except (TypeError, ValueError):
                km = 0

        name = it.get("name") or ""
        atid = it.get("@id") or ""
        # detail-id = fragment na '#'
        detail_id = atid.split("#")[-1] if "#" in atid else ""
        config = it.get("vehicleConfiguration") or ""

        results.append({
            "model_slug": model_slug,
            "merk": merk,
            "model": model,
            "title": name[:180],
            "price": price,
            "year": year,
            "km": km,
            "transmissie": (it.get("vehicleTransmission") or "automatisch").lower(),
            "brandstof": (it.get("fuelType") or "hybride").lower(),
            "body_type": _norm_body(it.get("bodyType") or "", name),
            "pano": 1,                       # gegarandeerd door opt=361 filter
            "options": _detect_options(f"{name} {config}"),
            "source_site": _source_from_image(it.get("image") or ""),
            "url": atid,                     # zoek-URL geankerd op #detail_id
            "raw": json.dumps({
                "detail_id": detail_id,
                "brand": it.get("brand"), "model": it.get("model"),
                "bodyType": it.get("bodyType"), "fuelType": it.get("fuelType"),
                "transmission": it.get("vehicleTransmission"),
                "color": it.get("color"), "doors": it.get("numberOfDoors"),
                "config": config, "image": it.get("image"),
            }, ensure_ascii=False)[:2000],
        })

    log.info("NL: JSON-LD %s -> %d/%d items (totaal in NL: %s)",
             model_slug, len(results), len(data.get("itemListElement", [])),
             data.get("numberOfItems"))
    return results


def _parse_gaspedaal_text(html, model_slug):
    """FALLBACK-parser op platte tekst (als de JSON-LD ontbreekt).

    Gaspedaal rendert per listing een tekstblok:
        <prijs>
        <titel>
        Bouwjaar:
        <jaar>
        Km.stand:
        <km>
        km
    We lezen prijs als anker en pakken de context-regels eromheen.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    if not body:
        return []

    lines = body.get_text(separator="\n", strip=True).split("\n")
    results = []
    i = 0
    while i < len(lines) - 5:
        line = lines[i].strip()

        price = _parse_price(line) if ("€" in line or re.match(r"^[\d.]+$", line)) else 0
        if not price:
            i += 1
            continue

        context_lines = lines[i + 1: min(i + 25, len(lines))]
        context = "\n".join(context_lines)

        title = ""
        for cl in context_lines[:5]:
            cl = cl.strip()
            if len(cl) > 12 and not cl.startswith("Bouwjaar") and not cl.lower().startswith("km"):
                title = cl[:140]
                break

        year = 0
        year_abs = 0
        for j, cl in enumerate(context_lines):
            if "Bouwjaar" in cl:
                for k, nx in enumerate(context_lines[j:j + 3]):
                    yr = _parse_year(nx)
                    if yr:
                        year = yr
                        year_abs = (i + 1) + j + k
                        break
                break

        km = 0
        km_abs = 0
        for j, cl in enumerate(context_lines):
            if "Km" in cl and "stand" in cl.lower():
                for k, nx in enumerate(context_lines[j + 1:j + 4]):
                    m = re.match(r"^([\d.]+)$", nx.strip())
                    if m:
                        try:
                            v = int(m.group(1).replace(".", ""))
                            if 0 < v < 500000:
                                km = v
                                km_abs = (i + 1) + (j + 1) + k
                        except ValueError:
                            pass
                        break
                break

        blob = (title + " " + context)
        merk = model_slug.split("/")[0]
        model = model_slug.split("/")[-1]
        results.append({
            "model_slug": model_slug,
            "merk": merk,
            "model": model,
            "title": title,
            "price": price,
            "year": year,
            "km": km,
            "transmissie": "automatisch",   # gegarandeerd door trns=AUTOMATISCH filter
            "brandstof": "hybride",          # gegarandeerd door /hybride pad
            "body_type": _detect_body(blob),
            "pano": 1,                       # gegarandeerd door opt=361 filter
            "options": _detect_options(blob),
            "source_site": _detect_source_site(context),
            "url": "",                       # detail-URL: later verrijken uit echte HTML
            "raw": context[:1500],
        })

        # Spring tot net ná de laatst-verwerkte regel (km-getal), zodat km-getallen
        # niet als losse "prijs" opnieuw worden opgepakt. Robuust voor blokken van
        # variabele lengte (was voorheen een blinde i += 15).
        i = max(km_abs + 1, year_abs + 1, i + 1)

    return results


# ── URL-bouw ────────────────────────────────────────────────────────────────


def _broad_scrape_url(model_slug):
    """Brede dag-scrape URL: alle km, bouwjaar 2021+, pano + automaat, prijs oplopend."""
    return (f"{GASPEDAAL_BASE}/{model_slug}/hybride"
            f"?bmin={_YEAR_MIN}&trns=AUTOMATISCH&opt={GASPEDAAL_PANO_OPT}&srt=pr-a")


def gaspedaal_link(model_key, year=0, km=0):
    """Klikbare Gaspedaal-zoeklink voor precies deze auto (exact jaar, km+marge)."""
    entry = GASPEDAAL_BY_MODEL.get(model_key)
    if not entry:
        return ""
    slug = entry[0]
    params = [f"bmin={_YEAR_MIN}", "trns=AUTOMATISCH", f"opt={GASPEDAAL_PANO_OPT}", "srt=pr-a"]
    if year:
        params = [f"bmin={year}", f"bmax={year}", "trns=AUTOMATISCH",
                  f"opt={GASPEDAAL_PANO_OPT}", "srt=pr-a"]
    if km:
        params.insert(2, f"kmax={km + _KM_MARGIN}")
    return f"{GASPEDAAL_BASE}/{slug}/hybride?" + "&".join(params)


# ── Refresh (1x per dag) ────────────────────────────────────────────────────


def refresh_due(conn, max_age_hours=24):
    """True als er >max_age_hours geleden (of nooit) is ververst, of als de
    NL-tabel nog leeg is (self-healing na een mislukte eerdere refresh)."""
    # Lege tabel -> altijd proberen (bv. na eerdere fail met kapot token)
    try:
        row = conn.execute("SELECT 1 FROM nl_listings LIMIT 1").fetchone()
        if row is None:
            return True
    except sqlite3.OperationalError:
        return True  # tabel bestaat nog niet

    last = _get_meta(conn, "last_refresh")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last_dt > timedelta(hours=max_age_hours)


def refresh_nl_prices(conn, fetch, force=False, debug_dir=None):
    """Scrape alle unieke Gaspedaal-modellen en sla alles op.

    fetch: callable(url, render=True, ...) -> html-string (scraper.scrape_do_fetch).
    Gebeurt alleen als er >24u geleden is ververst, tenzij force=True.
    """
    init_nl_db(conn)
    if not force and not refresh_due(conn):
        log.info("NL-prijzen nog vers (<24u) — refresh overgeslagen")
        return {"skipped": True}

    unique_slugs = sorted({slug for slug, _ in GASPEDAAL_BY_MODEL.values()})
    log.info("NL-refresh: %d Gaspedaal-modellen ophalen ...", len(unique_slugs))

    now = datetime.now(timezone.utc).isoformat()
    total_saved = 0
    html_ok = 0          # hoeveel modellen gaven bruikbare HTML terug
    per_model = {}

    for slug in unique_slugs:
        url = _broad_scrape_url(slug)
        html = fetch(url, render=True, super_mode=False, geo_code="nl")
        if not html:
            log.warning("NL-refresh: geen HTML voor %s", slug)
            per_model[slug] = 0
            continue
        html_ok += 1

        if debug_dir:
            safe = slug.replace("/", "_")
            try:
                with open(f"{debug_dir}/debug_gaspedaal_{safe}.html", "w", encoding="utf-8") as f:
                    f.write(html)
            except OSError:
                pass

        listings = parse_gaspedaal(html, slug)
        for lst in listings:
            lid = _stable_id(lst["model_slug"], lst["year"], lst["km"], lst["price"], lst["title"])
            exists = conn.execute("SELECT 1 FROM nl_listings WHERE id = ?", (lid,)).fetchone()
            if exists:
                conn.execute(
                    "UPDATE nl_listings SET last_seen = ?, price = ? WHERE id = ?",
                    (now, lst["price"], lid),
                )
            else:
                conn.execute(
                    """INSERT INTO nl_listings
                       (id, model_slug, merk, model, title, price, year, km, transmissie,
                        brandstof, body_type, pano, options, source_site, url, raw,
                        first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (lid, lst["model_slug"], lst["merk"], lst["model"], lst["title"],
                     lst["price"], lst["year"], lst["km"], lst["transmissie"],
                     lst["brandstof"], lst["body_type"], lst["pano"],
                     json.dumps(lst["options"]), lst["source_site"], lst["url"],
                     lst["raw"], now, now),
                )
        conn.commit()
        per_model[slug] = len(listings)
        total_saved += len(listings)
        log.info("NL-refresh: %-28s -> %d listings", slug, len(listings))

    # Alleen als vandaag-verse markeren wanneer minstens één model bruikbare HTML
    # gaf. Faalt alles (bv. ongeldig Scrape.do-token), dan last_refresh NIET zetten
    # zodat de volgende run het opnieuw probeert (self-healing).
    if html_ok == 0:
        log.error("NL-refresh MISLUKT: geen enkele Gaspedaal-pagina opgehaald "
                  "(check SCRAPE_DO_TOKEN). last_refresh niet gezet — retry volgende run.")
        return {"skipped": False, "failed": True, "total": 0, "per_model": per_model}

    _set_meta(conn, "last_refresh", now)
    log.info("NL-refresh klaar: %d listings over %d modellen (%d/%d modellen met HTML)",
             total_saved, len(unique_slugs), html_ok, len(unique_slugs))
    return {"skipped": False, "total": total_saved, "html_ok": html_ok, "per_model": per_model}


# ── Query: goedkoopste vergelijkbare NL-auto ────────────────────────────────


def nl_price_for(conn, model_key, year, km):
    """Goedkoopste vergelijkbare NL-auto uit de database.

    Returns (price, count, cheapest_url) of (0, 0, "") als niets gevonden.
    Filter: zelfde model_slug, exact bouwjaar, km <= (km + 20.000), pano.
    """
    entry = GASPEDAAL_BY_MODEL.get(model_key)
    if not entry:
        return 0, 0, ""
    slug, body = entry

    init_nl_db(conn)
    q = ("SELECT price, url FROM nl_listings "
         "WHERE model_slug = ? AND pano = 1 AND price > 0")
    args = [slug]
    if year:
        q += " AND year = ?"
        args.append(year)
    if km:
        q += " AND (km = 0 OR km <= ?)"
        args.append(km + _KM_MARGIN)
    if body:
        q += " AND body_type = ?"
        args.append(body)
    q += " ORDER BY price ASC"

    rows = conn.execute(q, args).fetchall()
    if not rows:
        return 0, 0, ""
    return rows[0][0], len(rows), rows[0][1] or ""


if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not os.environ.get("SCRAPE_DO_TOKEN"):
        print("ERROR: SCRAPE_DO_TOKEN niet gezet")
        sys.exit(1)

    # Los draaien: hergebruik de robuuste Scrape.do-client uit scraper.py
    from scraper import scrape_do_fetch, DB_PATH

    force = "--force" in sys.argv
    conn = sqlite3.connect(DB_PATH)
    result = refresh_nl_prices(conn, scrape_do_fetch, force=force, debug_dir=".")
    print(json.dumps(result, indent=2, ensure_ascii=False))
