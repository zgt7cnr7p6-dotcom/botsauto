#!/usr/bin/env python3
"""
Auto-Alert Scraper
Scraped mobile.de voor Audi Q3 45 TFSI e (hybrid) in Duitsland.
Stuurt Telegram alerts met feature-checklist en prijsscore.
Gebruikt Scrape.do als enige scraping provider (DataDome bypass).
"""

import os
import re
import sys
import json
import sqlite3
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs

import requests as req_lib
from bs4 import BeautifulSoup

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = "--dry-run" in sys.argv or not TELEGRAM_BOT_TOKEN

# Scrape.do API token — enige scraping provider
# Bypasses DataDome, anti-bot, CAPTCHAs automatisch
SCRAPE_DO_TOKEN = os.environ.get("SCRAPE_DO_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


SEARCH_CRITERIA = {
    "model": "Audi Q3 45 TFSI e",
    "fuel": "hybrid",
    "year_min": 2021,
    "km_max": 80_000,
    "price_max": 40_000,
    "country": "DE",
}

# mobile.de zoek-URLs
# URL 1: Q3 hybrid met "pano" in titel — alles doorsturen
# URL 2: Q3 Sportback hybrid (freetext) — alleen doorsturen als beschrijving panoramadak bevat
# URL 3: ALLE Q3 hybrids (catch-all, geen freetext) — filter op "sportback" in titel/beschrijving + pano check
#         Vangt Sportback listings die mobile.de nog niet geïndexeerd heeft voor freetext
MOBILE_DE_SEARCH_URLS = [
    {
        "url": (
            "https://suchen.mobile.de/fahrzeuge/search.html?"
            "isSearchRequest=true&s=Car&vc=Car"
            "&dam=false&fr=2021&ft=HYBRID&ml=%3A80000"
            "&ms=1900%3B37%3B%3Bpano&ms=1900%3B37%3B%3Bpanorama"
            "&ms=1900%3B37%3B%3Bp.dach&ms=1900%3B37%3B%3Bdach"
            "&p=%3A40000&od=down&sb=doc&ref=dsp"
        ),
        "label": "Q3 pano",
        "require_pano_in_desc": False,
    },
    {
        "url": (
            "https://suchen.mobile.de/fahrzeuge/search.html?"
            "isSearchRequest=true&s=Car&vc=Car"
            "&cn=DE&dam=false&fr=2021&ft=HYBRID&ml=%3A80000"
            "&ms=1900%3B37%3B%3Bsportback&ms=1900%3B37%3B%3Bspb"
            "&ms=1900%3B37%3B%3Bsport+back&ms=1900%3B37%3B%3Bsport"
            "&p=%3A40000&od=down&sb=doc&ref=dsp"
        ),
        "label": "Q3 Sportback (pano check)",
        "require_pano_in_desc": True,
    },
    {
        "url": (
            "https://suchen.mobile.de/fahrzeuge/search.html?"
            "isSearchRequest=true&s=Car&vc=Car"
            "&c=Cabrio&c=Limousine&c=OffRoad&c=SmallCar&c=SportsCar"
            "&dam=false&fr=2021&ft=HYBRID&ml=%3A90000"
            "&ms=17200%3B%3B6%3Bpano&ms=17200%3B%3B59%3Bpano"
            "&ms=3500%3B15%3B%3Bpano&ms=1900%3B8%3B%3Bpano"
            "&ms=1900%3B32%3B%3Bpano&ms=3500%3B15%3B%3Bglasdach"
            "&ms=1900%3B32%3B%3Bpanorama"
            "&p=28000%3A44000&od=down&sb=doc&ref=dsp"
        ),
        "label": "Multi-brand pano (C/GLC/330/A3/Q5)",
        "require_pano_in_desc": False,
        "min_listing_month": "2026-05",
    },
]
MOBILE_DE_SEARCH_URL = MOBILE_DE_SEARCH_URLS[0]["url"]

# ── Full-option checklist ──────────────────────────────────────────────────
FULL_OPTION_FEATURES = [
    "panoramadak",
    "keyless",
    "camera_achteruit",
    "camera_360",
    "s_line",
    "s_line_exterieur",
    "matrix_led",
    "velgen_19_20",
    "audio_premium",
    "elektrische_stoelen",
    "stoelverwarming",
    "stuurverwarming",
    "acc",
    "lane_assist",
    "travel_assist",
    "drive_select",
    "adaptief_onderstel",
    "emergency_assist",
    "side_assist",
    "ambient_lighting",
    "elektrische_achterklep",
    "optik_pakket_zwart",
    "dynamisch_knipperlicht",
    "head_up",
    "luchtvering",
]

FEATURE_DISPLAY_NAMES = {
    "panoramadak": "Panoramadak",
    "keyless": "Keyless",
    "camera_achteruit": "Achteruitrijcamera",
    "camera_360": "360° Camera",
    "s_line": "S-Line interieur",
    "s_line_exterieur": "S-Line exterieur",
    "matrix_led": "Matrix LED",
    "velgen_19_20": "19/20 inch velgen",
    "audio_premium": "Premium Audio (SONOS/B&O)",
    "elektrische_stoelen": "Elektrische stoelen",
    "stoelverwarming": "Stoelverwarming",
    "stuurverwarming": "Stuurverwarming",
    "acc": "ACC",
    "lane_assist": "Lane Assist",
    "travel_assist": "Travel Assist",
    "drive_select": "Drive Select",
    "adaptief_onderstel": "Adaptief onderstel",
    "emergency_assist": "Noodrem-assistent",
    "side_assist": "Dodehoek-assistent",
    "ambient_lighting": "Sfeerverlichting",
    "elektrische_achterklep": "Elektrische achterklep",
    "optik_pakket_zwart": "Optik pakket zwart",
    "dynamisch_knipperlicht": "Dynamisch knipperlicht",
    "head_up": "Head-Up Display",
    "luchtvering": "Luchtvering",
    "sportback": "Sportback",
}

FEATURE_DISPLAY_NAMES_MERCEDES = {
    "s_line": "AMG Line interieur",
    "s_line_exterieur": "AMG Line exterieur",
    "optik_pakket_zwart": "Night-Paket",
    "matrix_led": "DIGITAL LIGHT",
    "audio_premium": "Burmester",
    "adaptief_onderstel": "AIRMATIC (luchtvering)",
}

FEATURE_DISPLAY_NAMES_BMW = {
    "s_line": "M Sportpaket",
    "s_line_exterieur": "M Sportpaket exterieur",
    "optik_pakket_zwart": "Shadow Line",
    "matrix_led": "Adaptive LED",
    "audio_premium": "Harman Kardon",
}

DB_PATH = "listings.db"


# ── Database ────────────────────────────────────────────────────────────────


def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
    now = datetime.now(timezone.utc).isoformat()
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
    location: str = ""
    listing_date: str = ""
    color: str = ""
    score: int = 0
    features: list = field(default_factory=list)
    detail_incomplete: bool = False


# ── Hybrid detectie ───────────────────────────────────────────────────────


def is_q3_hybrid(title: str, description: str = "", fuel_type: str = "") -> bool:
    """Detecteer of een listing een Q3 (Sportback) hybrid is."""
    title_lower = title.lower()

    if "q3" not in title_lower:
        return False

    title_hybrid_patterns = [
        r"45\s*tfsi",
        r"tfsi\s*e\b",
        r"plug[\s-]?in",
        r"phev",
    ]
    if any(re.search(p, title_lower) for p in title_hybrid_patterns):
        return True

    fuel_lower = fuel_type.lower()
    if any(kw in fuel_lower for kw in ["hybrid", "elektro", "phev", "plug-in", "electric"]):
        return True

    desc_lower = description.lower()
    desc_hybrid_patterns = [
        r"plug[\s-]?in[\s-]?hybrid",
        r"\bphev\b",
        r"45\s*tfsi",
    ]
    return any(re.search(p, desc_lower) for p in desc_hybrid_patterns)


# ── HTML cleaning ─────────────────────────────────────────────────────────


def clean_detail_html(soup: BeautifulSoup) -> str:
    """Strip cookie consent, navigatie, footer en andere rommel uit detail page HTML.

    Retourneert schone platte tekst met alleen de advertentie-inhoud.
    """
    # Verwijder script, style en onzichtbare elementen
    for tag in soup.find_all(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    # Verwijder bekende rommel-elementen
    selectors_to_remove = [
        # Cookie / consent banners
        "[id*='usercentrics']", "[id*='cookie']", "[class*='cookie']",
        "[id*='consent']", "[class*='consent']",
        "[id*='gdpr']", "[class*='gdpr']",
        "[id*='onetrust']", "[class*='onetrust']",
        "[id*='CybotCookiebot']",
        # Navigatie & header
        "nav", "header",
        "[role='navigation']", "[role='banner']",
        "[class*='nav-bar']", "[class*='navbar']",
        "[class*='topbar']", "[class*='top-bar']",
        "[class*='breadcrumb']",
        # Footer
        "footer", "[role='contentinfo']", "[class*='footer']",
        # Advertenties / banners
        "[class*='ad-banner']", "[class*='advert']", "[id*='sponsored']",
        "[class*='banner']",
        # mobile.de specifiek
        "[class*='seller-info']", "[class*='dealer-info']",
        "[class*='financing']", "[class*='leasing']", "[class*='insurance']",
        "[class*='similar-cars']", "[class*='recommendation']",
        "[class*='cross-sell']", "[class*='upsell']",
        "[data-testid='sharing']", "[data-testid='social']",
    ]
    for selector in selectors_to_remove:
        for el in soup.select(selector):
            el.decompose()

    body_text = soup.get_text(separator=" ", strip=True)

    # Verwijder GDPR/consent tekstblokken die soms in de tekst achterblijven
    gdpr_patterns = [
        r"wir benötigen ihre einwilligung.*?(?:einverstanden|ablehnen|einstellungen verwalten)",
        r"impressum\s+datenschutz\s+cookie-erklärung.*?(?:anmelden|parkplatz|meine suchen)",
        r"suchen\s+leasing\s+auto\s+leasen.*?(?:anmelden|parkplatz)",
        r"wir arbeiten mit \d+ partnern zusammen.*?(?:einverstanden|ablehnen)",
        r"speichern von oder zugriff auf informationen.*?(?:verarbeitungszwecke|verbesserung von angeboten)",
        r"personalisierte werbung und inhalte.*?(?:verbesserung von angeboten|entwicklung und verbesserung)",
        # Navigatie-tekst die soms overblijft
        r"(?:auto|fahrzeug)bewertung\s+mehr erfahren.*?(?:anmelden|parkplatz)",
    ]
    for pat in gdpr_patterns:
        body_text = re.sub(pat, " ", body_text, flags=re.IGNORECASE | re.DOTALL)

    # Meerdere spaties samenvoegen
    body_text = re.sub(r"\s{2,}", " ", body_text).strip()

    return body_text


# ── Feature scoring ────────────────────────────────────────────────────────



# ── Claude AI scoring ─────────────────────────────────────────────────────

AI_SCORING_PROMPT = """\
Je bent een auto-expert. Lees de advertentietekst en bepaal welke opties aanwezig zijn.

Dit kan een Audi (Q3/Q5/A3), Mercedes-Benz (C-Klasse/GLC), of BMW (3er/330e) zijn. Detecteer het merk uit de tekst en gebruik de juiste terminologie.

BELANGRIJK: Wees NAUWKEURIG. Markeer een feature ALLEEN als true als het DUIDELIJK en EXPLICIET vermeld staat in de tekst (als optie, Sonderausstattung, of pakket). Bij twijfel: false.
LET OP: Een auto kan "sportief" of "sport" in de beschrijving hebben zonder dat het een sport-pakket (S line / AMG Line / M Sport) betreft. Markeer sport-pakketten alleen als ze LETTERLIJK als optie/uitrusting genoemd worden.

De tekst is vaak Duits (mobile.de). Lees ALLES grondig — mis NIETS:
   • Titel en subtitel
   • "Technische Daten" sectie (motor, vermogen, versnellingsbak, etc.)
   • "Ausstattung" / "Sonderausstattung" / "Serienausstattung" (VOLLEDIGE lijst doornemen!)
   • "Pakete" / gebundelde opties
   • Losse vermeldingen, bullet lists, lopende beschrijvingstekst
   • Afkortingen en samengestelde termen (bijv. "Sitz-u.Spiegelheizung")

BELANGRIJK: De "Ausstattung" lijst kan HEEL lang zijn (50+ items). Lees ELKE regel — features staan vaak onderaan!

## REGELS

1. SERIENAUSSTATTUNG = AANWEZIG.

2. SAMENGESTELDE TERMEN SPLITSEN:
   "Spurwechsel- u. Spurhalteassistent" → side_assist=true EN lane_assist=true
   "Sitz- u. Spiegelheizung" → stoelverwarming=true
   "Front- u. Rückfahrkamera" → camera_achteruit=true

3. PAKKET-HERKENNING PER MERK:

   AUDI (Q3 / Q5 / A3):
   • "S line Interieur" / "S line innen" → s_line=true (NIET automatisch s_line_exterieur)
   • "S line Exterieur" / "S line außen" → s_line_exterieur=true (NIET automatisch s_line)
   • "S line" / "S-Line" (zonder int/ext specificatie) → bepaal uit context welke variant(en). Check of interieur EN/OF exterieur apart vermeld worden
   • "S line Paket" / "S line Sportpaket" → s_line=true, s_line_exterieur=true (volledig pakket = beide)
   • "Assistenzpaket Tour" / "Assistenz-Paket Tour" → acc=true, lane_assist=true, emergency_assist=true, side_assist=true
   • "Assistenzpaket Sicherheit" / "Sicherheitspaket" → emergency_assist=true, side_assist=true
   • "Assistenzpaket Parken" / "Park-Paket" / "Assistenzpaket Stadt" / "Stadtpaket" → camera_360=true, camera_achteruit=true
   • "Adaptiver Fahrassistent" → acc=true, lane_assist=true, travel_assist=true
   • "Business-Paket" / "Business Paket" / "Businesspaket" → stoelverwarming=true, acc=true (Q3), soms keyless
   • "Komfort-Paket" / "Komfortpaket" → elektrische_stoelen=true, soms keyless
   • "Komfortschlüssel" / "Keyless" / "KESSY" / "Komfortschlüssel mit sensorgesteuerter Gepäckraumentriegelung" → keyless=true (+ elektrische_achterklep als "Gepäckraumentriegelung" vermeld)
   • "Top View Kamera" / "Umgebungskameras" / "Area View" → camera_360=true, camera_achteruit=true
   • "Ambiente Lichtpaket" / "Ambiente-Lichtpaket plus" / "Ambiente Lichtpaket plus" → ambient_lighting=true
   • "Optikpaket Schwarz" / "Optik Paket Schwarz" / "Black Style" / "Schwarz-Paket" → optik_pakket_zwart=true
   • "Audi pre sense front" / "pre sense city" / "pre sense basic" / "pre sense rear" → emergency_assist=true
   • "Bang & Olufsen" / "B&O" / "B&O 3D" / "Bang & Olufsen 3D" / "Bang & Olufsen Premium Sound" → audio_premium=true
   • "Sonos" / "SONOS" → audio_premium=true
   • "Matrix LED" / "Matrix-LED-Scheinwerfer" / "HD Matrix LED" → matrix_led=true
   • "Elektrische Heckklappe" / "elektr. Heckkl." / "Heckklappe elektrisch" / "Fußgesteuerte Heckklappe" / "Heckklappe mit Fußsensor" → elektrische_achterklep=true
   • "adaptive Dämpferregelung" / "Dämpferregelung" / "Adaptives Fahrwerk" → adaptief_onderstel=true (NIET "Sportfahrwerk" — dat is vast, niet adaptief)
   • "Audi drive select" / "drive select" → drive_select=true
   • "Dynamische Blinker" / "dynamisches Blinklicht" → dynamisch_knipperlicht=true
   • "Audi Side Assist" / "Side Assist" / "Spurwechselwarnung" → side_assist=true
   • Q3: "Sportback" in titel/model → sportback=true
   • Q3: "Technikpaket" / "Technik-Paket" → acc=true, lane_assist=true, vaak ook camera_achteruit
   • A3: "Assistenzpaket Fahren und Parken plus" → acc=true, lane_assist=true, camera_achteruit=true, emergency_assist=true
   • A3: "Businesspaket plus" → acc=true, lane_assist=true, camera_achteruit=true, elektrische_stoelen=true
   • Q5: "Sportback" in titel/model → sportback=true
   • Q5: "Luftfederung" / "adaptive Luftfederung" / "adaptive air suspension" → luchtvering=true, adaptief_onderstel=true
   • Q5: "Assistenzpaket Tour" → acc=true, lane_assist=true, emergency_assist=true, side_assist=true, travel_assist=true
   • Q5: "Assistenzpaket Stadt" / "Assistenzpaket Parken" → camera_360=true, camera_achteruit=true
   • Q5: "Technik" / "Technikpaket" → acc=true, lane_assist=true, head_up=true
   • Q5: "Ambiente Lichtpaket plus" → ambient_lighting=true
   • Q5: "Komfortpaket plus" → keyless=true, elektrische_stoelen=true, stoelen_memory=true
   • Q5: "Dynamikpaket plus" → adaptief_onderstel=true, drive_select=true
   • Q5: "Panorama-Glasdach" / "Panoramadach" → panoramadak=true

   MERCEDES-BENZ (C-Klasse / GLC):
   • "AMG Line Interieur" → s_line=true (NIET automatisch s_line_exterieur)
   • "AMG Line Exterieur" → s_line_exterieur=true (NIET automatisch s_line)
   • "AMG Line" / "AMG-Line" (zonder int/ext) → bepaal uit context. Als BEIDE vermeld of als compleet pakket → s_line=true, s_line_exterieur=true
   • "AMG Paket" / "AMG Sportpaket" → s_line=true, s_line_exterieur=true (volledig pakket = beide)
   • "Night-Paket" / "Night Paket" / "Nightpaket" → optik_pakket_zwart=true (vereist AMG Line)
   • "DISTRONIC" / "Aktiver Abstands-Assistent" → acc=true
   • "Fahrassistenz-Paket" / "Fahrassistenzpaket" → acc=true, lane_assist=true, travel_assist=true, emergency_assist=true
   • "Fahrassistenz-Paket Plus" → acc=true, lane_assist=true, travel_assist=true, side_assist=true, emergency_assist=true
   • "KEYLESS-GO Komfort-Paket" / "Keyless-Go" / "KEYLESS-GO" → keyless=true, elektrische_achterklep=true (HANDS-FREE ACCESS)
   • "Burmester" (Surround/3D) → audio_premium=true
   • "DIGITAL LIGHT" → matrix_led=true
   • "EASY-PACK Heckklappe" → elektrische_achterklep=true
   • "Totwinkel-Assistent" / "Totwinkelassistent" → side_assist=true
   • "Aktiver Brems-Assistent" / "PRE-SAFE" → emergency_assist=true
   • "Aktiver Spurhalte-Assistent" → lane_assist=true
   • "Aktiver Lenk-Assistent" → lane_assist=true, travel_assist=true (als ook DISTRONIC aanwezig)
   • "Panorama-Schiebedach" → panoramadak=true
   • "Park-Paket mit 360°-Kamera" / "360°-Kamera" / "Surroundview" → camera_360=true, camera_achteruit=true
   • "Park-Paket mit Rückfahrkamera" → camera_achteruit=true
   • "DYNAMIC SELECT" → drive_select=true
   • "AIRMATIC" / "Luftfederung" → adaptief_onderstel=true, luchtvering=true
   • "Ambientebeleuchtung Plus" / "64 Farben" / "Aktives Ambientelicht" → ambient_lighting=true
   • "Memory-Paket" → elektrische_stoelen=true, stoelen_memory=true
   • "Sitzkomfort-Paket" → elektrische_stoelen=true, stoelen_memory=true, stoelverwarming=true
   • "Advanced Plus" (pakket-tier) → ambient_lighting=true, side_assist=true, elektrische_stoelen=true, stoelen_memory=true
   • "Premium" (pakket-tier) → camera_360=true, camera_achteruit=true, matrix_led=true
   • "Premium Plus" (pakket-tier) → keyless=true, camera_360=true, matrix_led=true, ambient_lighting=true

   BMW (3er / 330e):
   • "M Sportpaket" / "M Sport Paket" / "M Paket" → s_line=true, s_line_exterieur=true, optik_pakket_zwart=true (volledig pakket = beide + Shadow Line)
   • "M Sportpaket Pro" / "M Sport Pro" → s_line=true, s_line_exterieur=true, optik_pakket_zwart=true
   • "M Sport" / "M-Sport" (zonder "Paket") → bepaal uit context welke variant(en). Kan alleen exterieur of alleen interieur zijn
   • "Shadow Line" / "Shadowline" / "Shadow-Line" / "Hochglanz Shadow Line" → optik_pakket_zwart=true
   • "Harman Kardon" / "Harman/Kardon" / "H/K" / "H+K" / "HK" → audio_premium=true
   • "Adaptive LED" / "Adaptive LED-Scheinwerfer" → matrix_led=true
   • "BMW Laserlicht" / "Laser" → matrix_led=true
   • "Comfort Access" / "Komfortzugang" → keyless=true
   • "Active Cruise Control" / "ACC" / "Aktive Geschwindigkeitsregelung" → acc=true
   • "Driving Assistant Professional" / "DAP" / "DA Professional" → acc=true, lane_assist=true, travel_assist=true, emergency_assist=true, side_assist=true
   • "Driving Assistant Plus" / "DA+" → acc=true, lane_assist=true
   • "Driving Assistant" (zonder Plus/Professional) → lane_assist=true, emergency_assist=true
   • "Innovationspaket" → keyless=true, matrix_led=true, acc=true
   • "Komfortpaket" → keyless=true
   • "Business Paket" → stoelverwarming=true
   • "Lenk- und Spurführungsassistent" → lane_assist=true
   • "Surround View" / "360°" / "360-Grad-Kamera" → camera_360=true, camera_achteruit=true
   • "Parking Assistant Plus" / "PA+" → camera_360=true, camera_achteruit=true
   • "Panorama-Glasdach" / "Glasdach" / "GSD" / "Pano" → panoramadak=true
   • "Adaptives Fahrwerk" / "Adaptives M Fahrwerk" → adaptief_onderstel=true
   • "Luftfederung" / "air suspension" → luchtvering=true
   • "Head-Up Display" / "HUD" → head_up=true
   • "Driving Experience Control" → drive_select=true
   • "Sportsitze" → (niet hetzelfde als elektrische stoelen)
   • "Memory" / "Memory-Sitze" → elektrische_stoelen=true, stoelen_memory=true

4. UNIVERSELE GERMAN-DUTCH MAPPINGS (alle merken):
   • Sitzheizung / beheizbare Sitze / Sitz-u.Spiegelheizung → stoelverwarming
   • Lenkradheizung / beheizbares Lenkrad → stuurverwarming
   • Spurhalteassistent / Spurführungsassistent / Spurverlassenswarnung → lane_assist
   • Spurwechselassistent / Side Assist / Totwinkelassistent → side_assist (dodehoek)
   • Abstandstempomat / adaptive Geschwindigkeitsregelung / ACC / DISTRONIC → acc
   • Rückfahrkamera / Heckkamera → camera_achteruit
   • Umgebungskameras / 360-Grad / Top View / Area View / Surround View → camera_360
   • Panorama-Glasdach / Glasdach / Panoramadach / Panorama-Schiebedach / Schiebedach → panoramadak
   • Elektrische Heckklappe / elektr. Heckklappe / EASY-PACK Heckklappe → elektrische_achterklep
   • Ambientebeleuchtung / Ambiente-Licht / Konturfarbenes Ambiente-Licht → ambient_lighting
   • Dynamischer Blinker / dynamisches Blinklicht / Lauflicht → dynamisch_knipperlicht
   • Luftfederung / adaptive Luftfederung / air suspension / AIRMATIC → luchtvering

5. IMPLICIETE FEATURES:
   • camera_360=true → camera_achteruit=true automatisch
   • travel_assist=true → acc=true EN lane_assist=true automatisch
   • luchtvering=true → adaptief_onderstel=true automatisch
   • s_line en s_line_exterieur zijn ONAFHANKELIJK — een auto kan alleen interieur, alleen exterieur, of beide hebben. Bepaal elk apart op basis van wat er EXPLICIET vermeld staat

6. ZOEK BREED — features kunnen overal staan:
   • In de titel ("S line", "AMG", "M-Sport", "Panorama")
   • In bullet lists ("• Sitzheizung")
   • In lopende tekst ("mit Panoramadach und Sitzheizung")
   • In code/afkortingen ("ACC", "LED", "PDC", "HUD", "DAP", "H/K")
   • In afgekorte vorm ("elektr. Heckkl.", "Komfortschl.", "Rückfahrk.")

Bepaal voor elk true of false:

1. panoramadak — Panoramadak/schuifdak/glasdak
2. keyless — Audi: Komfortschlüssel/KESSY | Mercedes: KEYLESS-GO | BMW: Comfort Access
3. camera_achteruit — Rückfahrkamera (ook als 360° camera aanwezig is)
4. camera_360 — 360°/Umgebungskameras/Surround View/Top View
5. s_line — Sportpakket interieur: Audi S-Line | Mercedes AMG Line | BMW M Sportpaket
6. s_line_exterieur — Sportpakket exterieur: Audi S-Line ext. | Mercedes AMG Line ext. | BMW M Sportpaket
7. matrix_led — Audi: Matrix LED | Mercedes: DIGITAL LIGHT | BMW: Adaptive LED/Laser (gewone LED NIET)
8. velgen_19_20 — 19/20 inch velgen
9. audio_premium — Audi: B&O/Sonos | Mercedes: Burmester | BMW: Harman Kardon (standaard audio NIET)
10. elektrische_stoelen — Elektrisch verstelbare stoelen
11. stoelen_memory — Memory stoelen
12. stoelverwarming — Sitzheizung/stoelverwarming
13. stuurverwarming — Lenkradheizung/stuurverwarming
14. acc — Audi: Abstandstempomat | Mercedes: DISTRONIC | BMW: Active Cruise Control (gewone tempomat NIET)
15. lane_assist — Spurhalteassistent/Spurverlassenswarnung/lane assist (dodehoek=side_assist, NIET hier)
16. travel_assist — TRUE als:
    a) Audi: "Adaptiver Fahrassistent" | Mercedes: "Fahrassistenz-Paket" + DISTRONIC | BMW: "Driving Assistant Professional"
    b) OF de COMBINATIE van ACC EN actieve stuursturing (lane keeping + steering assist) is aanwezig
    BELANGRIJK: Lane assist alleen ≠ Travel Assist. ACC alleen ≠ Travel Assist. Pas als BEIDE aanwezig zijn = Travel Assist
17. drive_select — Audi: drive select | Mercedes: DYNAMIC SELECT | BMW: Driving Experience Control
18. adaptief_onderstel — Adaptives Fahrwerk/Dämpferregelung/AIRMATIC (Sportfahrwerk NIET)
19. emergency_assist — Audi: pre sense | Mercedes: Aktiver Brems-Assistent/PRE-SAFE | BMW: Frontkollisionswarnung
20. side_assist — Audi: Side Assist | Mercedes: Totwinkel-Assistent | BMW: Spurwechselwarnung (dodehoek)
21. ambient_lighting — Ambientebeleuchtung/sfeerverlichting (alle merken)
22. elektrische_achterklep — Elektrische Heckklappe / EASY-PACK Heckklappe
23. optik_pakket_zwart — Audi: Optikpaket Schwarz | Mercedes: Night-Paket | BMW: Shadow Line
24. dynamisch_knipperlicht — Dynamischer Blinker/dynamisches Blinklicht/Lauflicht
25. head_up — Head-Up Display / HUD (alle merken)
26. luchtvering — Luftfederung / adaptive Luftfederung / AIRMATIC / air suspension (NIET gewoon adaptief onderstel/Sportfahrwerk)
27. sportback — true als het een Sportback variant is (Q3 Sportback, Q5 Sportback). Alleen op basis van modelnaam, NIET op uiterlijk

28. color — Exterieur kleur van de auto (bijv. "Navarrablau", "Obsidianschwarz", "Alpinweiß"). Zoek naar: Farbe, Außenfarbe, Lackierung. Geef de exacte kleur terug als string, of "" als niet gevonden.

Antwoord ALLEEN met JSON, geen uitleg:
{
  "panoramadak": false,
  "keyless": false,
  "camera_achteruit": false,
  "camera_360": false,
  "s_line": false,
  "s_line_exterieur": false,
  "matrix_led": false,
  "velgen_19_20": false,
  "audio_premium": false,
  "elektrische_stoelen": false,
  "stoelen_memory": false,
  "stoelverwarming": false,
  "stuurverwarming": false,
  "acc": false,
  "lane_assist": false,
  "travel_assist": false,
  "drive_select": false,
  "adaptief_onderstel": false,
  "emergency_assist": false,
  "side_assist": false,
  "ambient_lighting": false,
  "elektrische_achterklep": false,
  "optik_pakket_zwart": false,
  "dynamisch_knipperlicht": false,
  "head_up": false,
  "luchtvering": false,
  "sportback": false,
  "color": ""
}"""


def score_listing_ai(listing: Listing) -> Listing | None:
    """Score een listing via Claude AI. Retourneert None als het niet lukt."""
    if not HAS_ANTHROPIC or not ANTHROPIC_API_KEY:
        return None

    text = f"{listing.title}\n\n{listing.description}"
    desc_len = len(listing.description)
    log.info("[AI] Beschrijving lengte: %d chars voor %s", desc_len, listing.id[:30])

    if desc_len <= 500:
        log.warning("[AI] KORTE beschrijving (%d chars) — detail page waarschijnlijk niet opgehaald! %s",
                     desc_len, listing.id[:30])

    # Beperk tekst tot ~12000 chars (meer context = betere detectie)
    if len(text) > 12000:
        text = text[:12000]

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": f"{AI_SCORING_PROMPT}\n\n---\nTITEL: {listing.title}\n\nBESCHRIJVING:\n{text}",
                }
            ],
        )

        result_text = response.content[0].text.strip()
        # Verwijder eventuele markdown code blocks
        if result_text.startswith("```"):
            result_text = re.sub(r"^```(?:json)?\s*", "", result_text)
            result_text = re.sub(r"\s*```$", "", result_text)

        features_dict = json.loads(result_text)

        # 360° camera impliceert altijd achteruitrijcamera
        if features_dict.get("camera_360", False):
            features_dict["camera_achteruit"] = True

        # Luchtvering impliceert adaptief onderstel
        if features_dict.get("luchtvering", False):
            features_dict["adaptief_onderstel"] = True

        # Travel Assist: als ACC + lane_assist beide true → travel_assist ook true
        if features_dict.get("acc", False) and features_dict.get("lane_assist", False):
            if not features_dict.get("travel_assist", False):
                log.info("[AI] Travel Assist afgeleid: ACC + lane_assist beide aanwezig")
            features_dict["travel_assist"] = True

        # Travel Assist impliceert acc + lane_assist
        if features_dict.get("travel_assist", False):
            features_dict["acc"] = True
            features_dict["lane_assist"] = True

        found = [f for f in FULL_OPTION_FEATURES if features_dict.get(f, False)]
        # stoelen_memory is niet in FULL_OPTION_FEATURES maar wel relevant voor display
        if features_dict.get("stoelen_memory", False):
            found.append("stoelen_memory")
        if features_dict.get("sportback", False):
            found.append("sportback")
        listing.features = found
        listing.score = len([f for f in found if f in FULL_OPTION_FEATURES])

        if not listing.color:
            listing.color = features_dict.get("color", "")

        missing = [f for f in FULL_OPTION_FEATURES if f not in found]
        log.info(
            "[AI] Score %d/%d %s: FOUND=%s, MISSING=%s",
            listing.score, len(FULL_OPTION_FEATURES),
            listing.id[:30], found, missing,
        )
        return listing

    except json.JSONDecodeError as e:
        log.warning("[AI] JSON parse fout: %s — response: %s", e, result_text[:200])
        return None
    except Exception as e:
        log.warning("[AI] Claude API fout: %s", e)
        return None


AI_SCORE_RETRIES = 3
AI_SCORE_RETRY_WAIT = [3, 8]  # seconden wachten tussen retries


def score_listing(listing: Listing) -> Listing:
    """Score listing via Claude AI met retry."""
    for attempt in range(1, AI_SCORE_RETRIES + 1):
        result = score_listing_ai(listing)
        if result is not None:
            return result
        if attempt < AI_SCORE_RETRIES:
            wait = AI_SCORE_RETRY_WAIT[attempt - 1]
            log.warning("[AI] Poging %d/%d mislukt voor %s — retry over %ds",
                        attempt, AI_SCORE_RETRIES, listing.id[:30], wait)
            time.sleep(wait)

    log.error("AI scoring mislukt na %d pogingen voor %s", AI_SCORE_RETRIES, listing.id[:30])
    listing.score = -1  # Markeer als niet-gescoord
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


# Opties die er echt toe doen — als deze er allemaal op zitten is het een must buy
MUST_HAVE_FEATURES = [
    "panoramadak",
    "audio_premium",
    "s_line",
    "s_line_exterieur",
    "lane_assist",
    "acc",
    "ambient_lighting",
    "stoelverwarming",
    "camera_achteruit",
    "keyless",
]


def _buy_advice(price: int, score: int, max_score: int, features: list, km: int) -> str:
    """Genereer koopadvies op basis van prijs + must-haves + km-stand."""
    pct = score / max_score if max_score else 0

    # Tel hoeveel must-haves aanwezig zijn
    must_have_count = sum(1 for f in MUST_HAVE_FEATURES if f in features)
    must_have_total = len(MUST_HAVE_FEATURES)
    must_have_pct = must_have_count / must_have_total
    has_all_musts = must_have_count == must_have_total
    missing_musts = [FEATURE_DISPLAY_NAMES.get(f, f) for f in MUST_HAVE_FEATURES if f not in features]

    if not price:
        if has_all_musts:
            return "🔥 ALLE must-haves aanwezig — check de prijs!"
        return ""

    # km-correctie: lage km = meer waard
    # ≤30k km = premium, 30-50k = normaal, 50k+ = moet goedkoper
    km_bonus = ""
    if km and km <= 30_000:
        km_bonus = " + lage km!"
    elif km and km <= 20_000:
        km_bonus = " + zeer lage km!"

    # Hoofdlogica: must-haves + prijs + km
    if has_all_musts:
        if price <= 33_000:
            return f"🔥🔥🔥 DIRECT BELLEN — alles erop + scherpe prijs{km_bonus}"
        if price <= 35_000:
            if km and km <= 40_000:
                return f"🔥🔥 MOET KOPEN — alles erop + nette km{km_bonus}"
            return "🔥🔥 MOET KOPEN — alle must-haves aanwezig"
        if price <= 37_000:
            if km and km <= 30_000:
                return f"🔥🔥 MOET KOPEN — alles erop + lage km"
            return "🔥 MOET KOPEN — alles erop, prijs is fair"
        if price <= 38_500:
            if km and km <= 30_000:
                return "🔥 Duurder maar alles erop + lage km"
            return "👍 Alles erop maar aan de bovenkant qua prijs"
        return "⚠️ Boven budget — alles erop maar te duur"

    if must_have_pct >= 0.78:  # 7 van 9 must-haves
        missing_str = ", ".join(missing_musts[:3])
        if price <= 32_000:
            return f"🔥🔥 Topprijs! Mist alleen: {missing_str}"
        if price <= 35_000:
            if km and km <= 40_000:
                return f"🔥 Goede deal! Mist: {missing_str}"
            return f"👍 Redelijk. Mist: {missing_str}"
        if price <= 37_000:
            return f"👍 OK voor de prijs. Mist: {missing_str}"
        return f"⚠️ Boven budget + mist: {missing_str}"

    if must_have_pct >= 0.56:  # 5-6 van 9
        if price <= 30_000:
            return "👍 Scherpe prijs, mist een paar must-haves"
        if price <= 33_000 and km and km <= 40_000:
            return "👍 Redelijke deal voor de prijs"
        return ""

    # Weinig must-haves
    if price <= 28_000 and pct >= 0.50:
        return "👍 Goedkoop maar mist veel must-haves"
    return ""


def send_telegram(listing: Listing):
    max_score = len(FULL_OPTION_FEATURES)

    title_lower = listing.title.lower()
    if "mercedes" in title_lower or "benz" in title_lower:
        if "glc" in title_lower:
            model_tag = "Mercedes GLC"
        else:
            model_tag = "Mercedes C-Klasse"
        display_names = {**FEATURE_DISPLAY_NAMES, **FEATURE_DISPLAY_NAMES_MERCEDES}
    elif "bmw" in title_lower or "330e" in title_lower:
        model_tag = "BMW 330e"
        display_names = {**FEATURE_DISPLAY_NAMES, **FEATURE_DISPLAY_NAMES_BMW}
    elif "a3" in title_lower or "a 3" in title_lower:
        if "sportback" in title_lower:
            model_tag = "Audi A3 Sportback"
        else:
            model_tag = "Audi A3"
        display_names = FEATURE_DISPLAY_NAMES
    elif "q5" in title_lower:
        if "sportback" in title_lower:
            model_tag = "Audi Q5 Sportback"
        else:
            model_tag = "Audi Q5"
        display_names = FEATURE_DISPLAY_NAMES
    elif "sportback" in title_lower:
        model_tag = "Audi Q3 Sportback"
        display_names = FEATURE_DISPLAY_NAMES
    else:
        model_tag = "Audi Q3"
        display_names = FEATURE_DISPLAY_NAMES

    pct = listing.score / max_score if max_score else 0
    if pct >= 0.85:
        verdict_line = "🟢 TOPPER — bijna full option!"
    elif pct >= 0.70:
        verdict_line = "🟢 High spec"
    elif pct >= 0.50:
        verdict_line = "🟡 Redelijk uitgerust"
    elif pct >= 0.35:
        verdict_line = "🟠 Basis uitvoering"
    else:
        verdict_line = "🔴 Kaal"

    # Prijs formatting
    price_str = f"€{listing.price:,.0f}".replace(",", ".") if listing.price else "Prijs onbekend"

    # Info regels
    info_parts = [price_str]
    if listing.km:
        info_parts.append(f"{listing.km:,} km".replace(",", "."))
    if listing.year:
        info_parts.append(str(listing.year))
    info_line = " · ".join(info_parts)

    # Koopadvies
    advice = _buy_advice(listing.price, listing.score, max_score, listing.features, listing.km)

    # Feature check — groepeer in aanwezig / afwezig
    # Must-haves krijgen een ⭐ markering
    # ⭐ items worden bovenaan gesorteerd
    found_star = []
    found_normal = []
    missing_star = []
    missing_normal = []
    for f in FULL_OPTION_FEATURES:
        name = display_names.get(f, f)
        # Dynamische naam voor elektrische stoelen: + memory als dat gedetecteerd is
        if f == "elektrische_stoelen" and "stoelen_memory" in listing.features:
            name = "Elektrische stoelen + memory"
        star = " ⭐" if f in MUST_HAVE_FEATURES else ""
        if f in listing.features:
            if f in MUST_HAVE_FEATURES:
                found_star.append(f"  ✅ {name}{star}")
            else:
                found_normal.append(f"  ✅ {name}{star}")
        else:
            if f in MUST_HAVE_FEATURES:
                missing_star.append(f"  ❌ {name}{star}")
            else:
                missing_normal.append(f"  ❌ {name}{star}")
    found_lines = found_star + found_normal
    missing_lines = missing_star + missing_normal

    # Tijdstip van plaatsing
    date_line = ""
    if listing.listing_date:
        date_line = f"📅 Online sinds {listing.listing_date}\n"

    # Gevonden op tijdstip
    now_cet = datetime.now(ZoneInfo("Europe/Amsterdam"))
    found_time = now_cet.strftime("%d-%m-%Y %H:%M")

    text = (
        f"<b>{listing.title}</b>\n"
        f"{info_line}\n"
    )

    if advice:
        text += f"\n<b>{advice}</b>\n"

    if listing.detail_incomplete:
        text += (
            f"\n⚠️ <b>Detail page niet geladen — score onbetrouwbaar!</b>\n"
            f"Score is waarschijnlijk hoger dan hieronder getoond.\n"
        )

    text += (
        f"\n"
        f"{verdict_line}\n"
        f"<b>{listing.score}/{max_score}</b> opties gevonden\n"
        f"\n"
    )

    if found_lines:
        text += "\n".join(found_lines) + "\n"
    if missing_lines:
        if found_lines:
            text += "\n"
        text += "\n".join(missing_lines) + "\n"

    location_line = ""
    if listing.location:
        location_line = f"📍 {listing.location}\n"

    text += (
        f"\n"
        f"{date_line}"
        f"{location_line}"
        f"🕐 Gevonden op {found_time}\n"
        f"\n"
        f"<a href=\"{listing.url}\">👉 BEKIJK ADVERTENTIE</a>"
    )

    if DRY_RUN:
        log.info("[DRY-RUN] Telegram alert:\n%s", text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = req_lib.post(
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
        return True
    else:
        log.error("Telegram fout: %s %s", resp.status_code, resp.text)
        return False


# ── Scrape.do fetch ─────────────────────────────────────────────────────────


def scrape_do_fetch(url: str, render: bool = False, retries: int = 1, super_mode: bool = True, timeout: int = 45, render_wait: int = 5000, geo_code: str = "", regional_geo_code: str = "", wait_selector: str = "", play_with_browser: list | None = None, set_cookies: str = "", wait_until: str = "", block_resources: bool | None = None) -> str | None:
    """Haal een pagina op via Scrape.do met retry logica.

    Scrape.do docs: https://scrape.do/documentation/

    super=true   — residential/mobile proxy, anti-bot bypass (10 credits).
    render=true  — headless Chromium voor JS rendering.
    geoCode=de   — Duits IP (belangrijk voor mobile.de).
    waitSelector — wacht tot CSS element geladen is (max 10s).
    playWithBrowser — browser acties: Click, Wait, Execute.
    setCookies   — cookies meesturen naar target site (bijv. GDPR consent).
    """
    if not SCRAPE_DO_TOKEN:
        log.error("SCRAPE_DO_TOKEN niet geconfigureerd")
        return None

    for attempt in range(retries + 1):
        try:
            params = {
                "token": SCRAPE_DO_TOKEN,
                "url": url,
            }
            if super_mode:
                params["super"] = "true"
            if geo_code:
                params["geoCode"] = geo_code
            if regional_geo_code:
                params["regionalGeoCode"] = regional_geo_code
            if set_cookies:
                params["setCookies"] = set_cookies
            if render:
                params["render"] = "true"
                # customWait is the documented param for post-load delay (scrape.do/documentation/headless-browser/wait/)
                params["customWait"] = str(render_wait)
                # blockResources default = true (CSS/images/fonts). Set to false when JS-heavy consent flows need everything.
                if block_resources is False:
                    params["blockResources"] = "false"
                elif block_resources is True:
                    params["blockResources"] = "true"
                if wait_until:
                    params["waitUntil"] = wait_until
                if wait_selector:
                    params["waitSelector"] = wait_selector
                if play_with_browser:
                    params["playWithBrowser"] = json.dumps(play_with_browser)

            resp = req_lib.get("https://api.scrape.do", params=params, timeout=timeout)
            log.info("Scrape.do: status=%d, size=%d bytes voor %s",
                     resp.status_code, len(resp.content), url[:80])

            if resp.status_code == 200:
                html = resp.text
                if len(html) > 500:
                    return html
                log.warning("Scrape.do: te kleine response (%d chars)", len(html))
            elif resp.status_code == 401:
                log.error("Scrape.do: ongeldige API token")
                return None  # geen retry bij auth fout
            elif resp.status_code == 429:
                log.warning("Scrape.do: rate limit / credits op")
                if attempt < retries:
                    wait = 2 ** (attempt + 1)
                    log.info("Wacht %ds voor retry ...", wait)
                    time.sleep(wait)
                    continue
            else:
                log.warning("Scrape.do: fout status %d — %s", resp.status_code, resp.text[:200])

            if attempt < retries and resp.status_code != 200:
                wait = 2 ** (attempt + 1)
                log.info("Retry %d/%d na %ds ...", attempt + 1, retries, wait)
                time.sleep(wait)
                continue

            return None

        except req_lib.RequestException as e:
            log.error("Scrape.do: request mislukt: %s", e)
            if attempt < retries:
                wait = 2 ** (attempt + 1)
                log.info("Retry %d/%d na %ds ...", attempt + 1, retries, wait)
                time.sleep(wait)
                continue
            return None

    return None


# ── Helpers ─────────────────────────────────────────────────────────────────


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
    ez_match = re.search(r"EZ\s*(\d{2}/)?(20[12]\d)", text, re.IGNORECASE)
    if ez_match:
        return int(ez_match.group(2))

    matches = [int(m) for m in re.findall(r"(20[12]\d)", text)]
    if not matches:
        return 0

    current_year = datetime.now().year
    past_years = [y for y in matches if y < current_year]
    if past_years:
        return max(past_years)
    return matches[0]


def extract_listing_id(href: str, source: str, fallback: str = "") -> str:
    """Extract een stabiel listing ID uit een URL."""
    if not href:
        return f"{source}_{fallback}" if fallback else ""

    parsed = urlparse(href)

    if source == "mobile":
        params = parse_qs(parsed.query)
        if "id" in params:
            return f"mobile_{params['id'][0]}"
        nums = re.findall(r"(\d{6,})", parsed.path)
        if nums:
            return f"mobile_{nums[-1]}"

    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return f"{source}_{abs(hash(clean_url)) % 10**12}"


# ── mobile.de scraper ───────────────────────────────────────────────────────


def scrape_mobile_de(conn, search_url: str = "", fetch_details: bool = False) -> list:
    """Scrape mobile.de zoekpagina via Scrape.do + BeautifulSoup.

    fetch_details=False: alleen zoekpagina, supersnel (URL 1).
    fetch_details=True: ook detail pagina's ophalen voor feature-detectie (URL 2).
    """
    listings = []

    if not SCRAPE_DO_TOKEN:
        log.info("mobile.de: SCRAPE_DO_TOKEN niet geconfigureerd, overslaan")
        return listings

    if not search_url:
        search_url = MOBILE_DE_SEARCH_URL

    # Direct super mode — standaard modus geeft altijd 502 op mobile.de
    log.info("mobile.de: zoekpagina ophalen ...")
    html = scrape_do_fetch(search_url, super_mode=True, retries=1, geo_code="de")

    if not html:
        log.error("mobile.de: geen HTML ontvangen")
        return listings

    # Detecteer block (ook na super mode)
    if "zugriff verweigert" in html.lower() or "access denied" in html.lower():
        log.error("mobile.de: geblokkeerd (Zugriff verweigert / Access denied)")
        try:
            with open("debug_mobile.html", "w", encoding="utf-8") as f:
                f.write(html[:500_000])
        except Exception:
            pass
        return listings

    # Debug HTML opslaan
    try:
        with open("debug_mobile.html", "w", encoding="utf-8") as f:
            f.write(html[:500_000])
    except Exception:
        pass

    soup = BeautifulSoup(html, "html.parser")

    page_title = soup.title.text if soup.title else ""
    log.info("mobile.de: page title = '%s'", page_title)

    # Zoek listing cards
    cards = soup.select("a[data-testid*='result'], a[data-testid*='listing']")
    if not cards:
        cards = soup.select("a.result-item, div.cBox-body--resultitem")
    if not cards:
        cards = soup.select("a[href*='/fahrzeuge/details']")

    log.info("mobile.de: %d cards gevonden", len(cards))

    if not cards:
        body = soup.get_text(separator=" ", strip=True)
        log.info("mobile.de body (2000 chars): %s", body[:2000])
        return listings

    for card in cards[:50]:
        try:
            card_full_text = card.get_text(separator=" ", strip=True)

            # Skip gesponsorde advertenties
            first_50 = card_full_text.lower()[:50]
            if "gesponsert" in first_50 or "sponsored" in first_50:
                log.info("Gesponsorde advertentie overgeslagen")
                continue

            # Title
            title = ""
            for tag in card.select("h2, span.h3, .u-text-break-word"):
                title = tag.get_text(strip=True)
                if title:
                    break
            if not title:
                title = card_full_text.split("\n")[0][:100]

            title = re.sub(r"^(Gesponsert|Sponsored|NEU|NEW)\s*", "", title, flags=re.IGNORECASE)

            if not title:
                continue

            # URL
            href = card.get("href", "")
            if not href:
                link = card.select_one("a[href*='/fahrzeuge/']")
                if link:
                    href = link.get("href", "")
            if href and not href.startswith("http"):
                href = "https://suchen.mobile.de" + href

            listing_id = extract_listing_id(href, "mobile", fallback=str(abs(hash(title))))

            if listing_exists(conn, listing_id):
                log.info("Bekende listing overgeslagen: %s", title[:50])
                continue

            # Price
            card_text = card_full_text
            price = 0
            for price_el in card.select("span[data-testid*='price'], .price-block span"):
                price = parse_price(price_el.get_text())
                if price:
                    break
            if not price:
                price_match = re.search(r"€\s*([\d.]+)", card_text)
                if price_match:
                    price = parse_price(price_match.group(1))

            # KM
            km = 0
            km_match = re.search(r"([\d.]+)\s*km", card_text, re.IGNORECASE)
            if km_match:
                km = parse_km(km_match.group(0))

            # Year
            year = parse_year(card_text)

            # Locatie (dealer stad)
            location = ""
            loc_match = re.search(r"(\d{4,5})\s+([A-ZÄÖÜa-zäöüß][\w\s\-äöüßÄÖÜ]{2,40})", card_text)
            if loc_match:
                location = loc_match.group(0).strip()

            # Listing datum (bijv. "13.3.2026, 10:50")
            listing_date = ""
            date_match = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4}),?\s*(\d{1,2}:\d{2})", card_text)
            if date_match:
                listing_date = date_match.group(0).strip()

            listing = Listing(
                id=listing_id,
                source="mobile.de",
                title=title,
                price=price,
                year=year,
                km=km,
                url=href,
                description=card_text[:500],
                location=location,
                listing_date=listing_date,
            )

            log.info("MATCH: %s — €%s — %d km — %d", title[:50], f"{price:,}" if price else "?", km, year)
            listings.append(listing)

        except Exception as e:
            log.warning("mobile.de card: %s", e)
            continue

    log.info("mobile.de: %d nieuwe listings gevonden", len(listings))
    return listings


# ── Detail page fetcher ──────────────────────────────────────────────────────


def _clean_detail_url(url: str) -> str:
    """Strip zoekparameters van detail URL — alleen id behouden."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "id" in params:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?id={params['id'][0]}"
    return url


# GDPR consent bypass voor mobile.de detail pages.
#
# Achtergrond: mobile.de toont een Usercentrics CMP wall vóór de auto-details.
# Eerdere aanpak (setCookies=usercentrics-cmp-consent=true) werkte NIET — verkeerde
# cookie naam/format, mobile.de honoreert hem niet. Resultaat: 100% van detail
# fetches kwam terug als pure consent-wall HTML.
#
# Nieuwe aanpak: actief de accept-knop klikken via playWithBrowser.
# Usercentrics rendert in Shadow DOM, dus geen CSS selector op de top-level
# document — we moeten via JS in shadowRoot graven.
#
# Volgorde:
#  1. Wait 1500ms zodat usercentrics-root in DOM staat
#  2. Execute: klik accept-button binnen shadow DOM (probeer meerdere selectors)
#  3. WaitSelector: wacht tot <h1> verschijnt = detail page geladen
#
# Daarnaast:
#  - waitUntil=networkidle2   → wachten tot max 2 connections over (sneller, minder timeouts)
#  - blockResources=false     → Usercentrics CSS/fonts moeten kunnen laden
#  - customWait=3000          → kleine extra buffer na network idle
CONSENT_CLICK_JS = (
    "const click = (root) => {"
    "  if (!root) return false;"
    "  const sels = ["
    "    'button[data-testid=\"uc-accept-all-button\"]',"
    "    'button[data-testid=\"uc-accept-button\"]',"
    "    '[data-testid=\"uc-accept-all-button\"]',"
    "    'button[mode=\"primary\"]',"
    "  ];"
    "  for (const s of sels) {"
    "    const el = root.querySelector(s);"
    "    if (el) { el.click(); return true; }"
    "  }"
    "  return false;"
    "};"
    "const host = document.getElementById('usercentrics-root') || document.querySelector('#usercentrics-cmp-ui');"
    "if (host && host.shadowRoot) { click(host.shadowRoot); }"
    "else { click(document); }"
)

CONSENT_ACTIONS = [
    {"Action": "Wait", "Timeout": 1500},
    {"Action": "Execute", "Execute": CONSENT_CLICK_JS},
    {"Action": "WaitSelector", "WaitSelector": "h1", "Timeout": 8000},
]


def _fetch_single_detail(idx_url: tuple) -> tuple:
    """Haal één detail page op met actieve consent-click via playWithBrowser."""
    idx, url = idx_url
    clean_url = _clean_detail_url(url)
    if clean_url != url:
        log.info("Detail URL gecleaned: %s", clean_url[:80])

    # Eén poging met de juiste params — playWithBrowser klikt consent weg en
    # wacht tot de echte page (h1) gerenderd is.
    html = scrape_do_fetch(
        clean_url, render=False, super_mode=True, retries=2, timeout=45,
        geo_code="de",
        set_cookies="usercentrics-cmp-consent=true",
    )
    return idx, html


def _fetch_detail_pages(listings: list) -> list:
    """Haal detail pages op voor listings (parallel, max 8 threads).

    Detail pages gebruiken render ZONDER super mode (5 credits i.p.v. 25).
    Zoekpagina's hebben super nodig voor DataDome, detail pages niet.

    Muteert listings in-place: zet description + detail_incomplete.
    """
    urls_to_fetch = {i: lst.url for i, lst in enumerate(listings) if lst.url}
    if not urls_to_fetch:
        return listings

    log.info("Detail pages ophalen voor %d listings (parallel) ...", len(urls_to_fetch))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_single_detail, item): item for item in urls_to_fetch.items()}
        for future in as_completed(futures):
            try:
                idx, detail_html = future.result()
                lst = listings[idx]
                if detail_html and len(detail_html) > 5000:
                    detail_soup = BeautifulSoup(detail_html, "html.parser")
                    body_text = clean_detail_html(detail_soup)
                    if len(body_text) > 100:
                        lst.description = body_text[:20000]
                        log.info("Detail OK: %s (%d chars)", lst.title[:40], len(lst.description))
                    else:
                        lst.detail_incomplete = True
                        log.warning("Detail geblokkeerd: %s (body %d chars, raw %d) — dumping HTML", lst.title[:40], len(body_text), len(detail_html))
                        try:
                            with open(f"debug_detail_{lst.id}.html", "w", encoding="utf-8") as fh:
                                fh.write(detail_html)
                        except Exception as dump_err:
                            log.warning("Kon detail HTML niet dumpen: %s", dump_err)
                elif detail_html:
                    log.warning("Detail te klein: %s (%d bytes) — dumping HTML", lst.title[:40], len(detail_html))
                    lst.detail_incomplete = True
                    try:
                        with open(f"debug_detail_{lst.id}.html", "w", encoding="utf-8") as fh:
                            fh.write(detail_html)
                    except Exception as dump_err:
                        log.warning("Kon detail HTML niet dumpen: %s", dump_err)
            except Exception as e:
                log.warning("Detail fetch fout: %s", e)

    # Log welke listings GEEN detail page kregen
    for lst in listings:
        if len(lst.description) <= 500:
            lst.detail_incomplete = True
            log.warning("GEEN detail page voor: %s (desc=%d chars) — scoring onbetrouwbaar!", lst.title[:50], len(lst.description))
    return listings


# ── Main ────────────────────────────────────────────────────────────────────


def send_failure_alert(error_msg: str, attempt: int, max_attempts: int):
    """Stuur Telegram alert als de scraper faalt."""
    if DRY_RUN or not TELEGRAM_BOT_TOKEN:
        return
    now_cet = datetime.now(ZoneInfo("Europe/Amsterdam"))
    text = (
        "🚨 <b>SCRAPER GEFAALD</b>\n\n"
        f"De auto-alert scraper is gecrasht na {attempt}/{max_attempts} pogingen.\n\n"
        f"<b>Fout:</b> <code>{error_msg[:500]}</code>\n\n"
        f"🕐 {now_cet.strftime('%d-%m-%Y %H:%M:%S')} CET\n\n"
        "⚠️ Er worden GEEN nieuwe listings gemonitord tot de volgende scheduled run!"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        req_lib.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
    except Exception:
        log.error("Kon ook geen failure alert sturen via Telegram")


def _run_scrape():
    """Core scrape logica — kan geretried worden bij fouten."""
    if not SCRAPE_DO_TOKEN:
        log.error("SCRAPE_DO_TOKEN niet geconfigureerd — kan niet scrapen")
        return

    log.info("Methode: Scrape.do API (super=true, DataDome bypass, setCookies GDPR)")

    conn = init_db()

    all_listings: list[Listing] = []
    seen_ids: set[str] = set()

    # ── mobile.de: zoekpagina's PARALLEL ophalen ──
    search_results: dict[int, list[Listing]] = {}

    def _fetch_search_page(idx_cfg):
        idx, cfg = idx_cfg
        return idx, scrape_mobile_de(conn, search_url=cfg["url"], fetch_details=False)

    log.info("Zoekpagina's parallel ophalen (%d URLs) ...", len(MOBILE_DE_SEARCH_URLS))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_search_page, (i, cfg)): i for i, cfg in enumerate(MOBILE_DE_SEARCH_URLS)}
        for future in as_completed(futures):
            try:
                idx, listings = future.result()
                search_results[idx] = listings
            except Exception as e:
                idx = futures[future]
                log.error("Zoekpagina %d fout: %s", idx, e)
                search_results[idx] = []

    # ── Verwerk resultaten (sequentieel voor dedup) ──
    for i, search_cfg in enumerate(MOBILE_DE_SEARCH_URLS):
        url_label = search_cfg["label"]
        require_pano = search_cfg["require_pano_in_desc"]
        require_text = search_cfg.get("require_text", "")
        min_listing_month = search_cfg.get("min_listing_month", "")

        log.info("━━━ %s ━━━", url_label)
        mobile_listings = search_results.get(i, [])

        if mobile_listings:
            log.info("[%s] %d listings gevonden", url_label, len(mobile_listings))
        else:
            log.warning("[%s] 0 listings", url_label)

        # Stap 2: Filter op require_text en seen_ids VOORDAT we dure detail pages ophalen
        filtered = []
        for lst in mobile_listings:
            if lst.id in seen_ids:
                log.info("[%s] Overgeslagen (al in andere URL): %s", url_label, lst.title[:40])
                continue
            # Filter op vereiste tekst in titel (card-tekst bevat description=card_text[:500])
            if require_text and require_text.lower() not in (lst.title + " " + lst.description).lower():
                log.info("[%s] Overgeslagen (geen '%s' in titel/beschrijving): %s", url_label, require_text, lst.title[:40])
                if not listing_exists(conn, lst.id):
                    save_listing(conn, lst)
                    log.info("[%s] Opgeslagen in DB (zonder alert) om herhaling te voorkomen", url_label)
                continue
            if min_listing_month and lst.listing_date:
                try:
                    dt = datetime.strptime(lst.listing_date.split(",")[0].strip(), "%d.%m.%Y")
                    listing_ym = dt.strftime("%Y-%m")
                    if listing_ym < min_listing_month:
                        log.info("[%s] Overgeslagen (te oud: %s < %s): %s", url_label, listing_ym, min_listing_month, lst.title[:40])
                        if not listing_exists(conn, lst.id):
                            save_listing(conn, lst)
                        continue
                except (ValueError, IndexError):
                    pass
            seen_ids.add(lst.id)
            lst._require_pano = require_pano
            filtered.append(lst)

        # Stap 3: Alleen voor gefilterde listings detail pages ophalen
        if filtered:
            log.info("[%s] %d listings na filter, detail pages ophalen ...", url_label, len(filtered))
            filtered = _fetch_detail_pages(filtered)

        all_listings.extend(filtered)
        log.info("[%s] %d listings toegevoegd", url_label, len(filtered))

    # ── Score (parallel) en alert ──
    log.info("Totaal: %d listings, nu scoren (parallel) ...", len(all_listings))

    new_count = 0
    alert_count = 0

    # Score alle listings parallel via ThreadPool (inclusief URL 2)
    if all_listings:
        with ThreadPoolExecutor(max_workers=5) as pool:
            scored = list(pool.map(score_listing, all_listings))
        # Restore _require_pano en detail_incomplete flags
        for orig, sc in zip(all_listings, scored):
            sc._require_pano = getattr(orig, '_require_pano', False)
            sc.detail_incomplete = getattr(orig, 'detail_incomplete', False)
        all_listings = scored

    # Seed mode: alleen als de DB helemaal leeg is (eerste run ooit / cache verloren)
    # Voorkomt 70+ alerts tegelijk. Bij niet-lege DB: gewoon alerten.
    seed_mode = False
    if conn:
        row = conn.execute("SELECT COUNT(*) FROM listings").fetchone()
        db_count = row[0] if row else 0
        if db_count == 0 and len(all_listings) > 5:
            seed_mode = True
            log.info("SEED MODE: DB is leeg, %d listings opslaan zonder alerts (eerste run)", len(all_listings))

    for listing in all_listings:
        # Als AI scoring mislukt is na alle retries: NIET opslaan zodat volgende run opnieuw probeert
        if listing.score < 0:
            log.warning("Listing overgeslagen (AI scoring mislukt na retries, wordt volgende run opnieuw geprobeerd): %s", listing.title[:40])
            continue

        is_new = not listing_exists(conn, listing.id)

        # URL 2 pano check: laat Claude AI bepalen of panoramadak aanwezig is
        require_pano = getattr(listing, '_require_pano', False)
        if require_pano and "panoramadak" not in listing.features:
            if getattr(listing, 'detail_incomplete', False):
                log.warning("[pano-AI] Detail page incompleet, NIET opslaan (retry volgende run): %s", listing.title[:40])
                continue
            log.info("[pano-AI] Overgeslagen (AI zegt geen pano): %s | features=%s",
                     listing.title[:40], listing.features)
            save_listing(conn, listing)
            continue

        if is_new:
            new_count += 1

            if seed_mode:
                save_listing(conn, listing)
                log.info("SEED: %s — opgeslagen zonder alert (DB was leeg)", listing.title[:50])
            else:
                sent = send_telegram(listing)
                if sent:
                    save_listing(conn, listing)
                    alert_count += 1
                    log.info(
                        "ALERT: %s — score %d/%d — €%s — %s",
                        listing.title,
                        listing.score,
                        len(FULL_OPTION_FEATURES),
                        f"{listing.price:,}" if listing.price else "?",
                        listing.url,
                    )
                else:
                    log.error("NIET opgeslagen (Telegram mislukt, retry volgende run): %s", listing.title[:40])
        else:
            save_listing(conn, listing)
            log.info("Bekende listing bijgewerkt: %s", listing.id)

    conn.close()

    log.info(
        "=== Klaar: %d totaal, %d nieuw, %d alerts verstuurd ===",
        len(all_listings),
        new_count,
        alert_count,
    )


MAX_RETRIES = 3
RETRY_BACKOFF = [10, 30, 60]  # seconden wachten tussen retries


def main():
    # Quiet hours: niet scrapen tussen 20:00 en 08:00 CET, behalve om 00:30
    force = "--force" in sys.argv or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not force:
        now_cet = datetime.now(ZoneInfo("Europe/Amsterdam"))
        hour = now_cet.hour
        minute = now_cet.minute
        is_night_scan = (hour == 0 and 25 <= minute <= 40)
        if not is_night_scan and (hour >= 20 or hour < 8):
            log.info("Quiet hours (%02d:%02d CET) — overslaan (actief 08:00-20:00 + 00:30)", hour, minute)
            return
        if is_night_scan:
            log.info("Nachtscan (%02d:%02d CET)", hour, minute)
    else:
        log.info("--force: quiet hours overgeslagen")

    log.info("=== Auto-Alert Scraper gestart ===")
    log.info(
        "Zoekcriteria: %s (%s), max €%s, land: %s",
        SEARCH_CRITERIA["model"],
        SEARCH_CRITERIA["fuel"],
        f"{SEARCH_CRITERIA['price_max']:,}",
        SEARCH_CRITERIA["country"],
    )

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _run_scrape()
            return  # succes — klaar
        except Exception as exc:
            last_error = exc
            log.error("Poging %d/%d GEFAALD: %s", attempt, MAX_RETRIES, exc, exc_info=True)
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[attempt - 1]
                log.info("Retry over %d seconden...", wait)
                time.sleep(wait)

    # Alle pogingen gefaald
    log.error("ALLE %d POGINGEN GEFAALD — scraper run mislukt!", MAX_RETRIES)
    send_failure_alert(str(last_error), MAX_RETRIES, MAX_RETRIES)
    sys.exit(1)


if __name__ == "__main__":
    main()
