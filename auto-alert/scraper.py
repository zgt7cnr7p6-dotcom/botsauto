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
            "dam=false&fr=2021%3A&ft=HYBRID&isSearchRequest=true"
            "&ml=%3A80000&ms=1900%3B37%3B%3Bpano&od=down"
            "&p=%3A40000&s=Car&sb=doc&vc=Car"
        ),
        "label": "Q3 pano",
        "require_pano_in_desc": False,
    },
    {
        "url": (
            "https://suchen.mobile.de/fahrzeuge/search.html?"
            "cn=DE&dam=false&fr=2021%3A&ft=HYBRID&isSearchRequest=true"
            "&ml=%3A80000&ms=1900%3B37%3B%3Bsportback&od=down"
            "&p=%3A40000&s=Car&sb=doc&vc=Car"
        ),
        "label": "Q3 Sportback (pano check)",
        "require_pano_in_desc": True,
    },
    {
        "url": (
            "https://suchen.mobile.de/fahrzeuge/search.html?"
            "dam=false&fr=2021%3A&ft=HYBRID&isSearchRequest=true"
            "&ml=%3A80000&ms=1900%3B37&od=down"
            "&p=%3A40000&s=Car&sb=doc&vc=Car"
        ),
        "label": "Q3 Sportback catch-all (pano check)",
        "require_pano_in_desc": True,
        "require_text": "sportback",
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
]

FEATURE_DISPLAY_NAMES = {
    "panoramadak": "Panoramadak",
    "keyless": "Keyless Entry",
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
    "acc": "ACC (Abstandstempomat)",
    "lane_assist": "Lane assist",
    "travel_assist": "Travel Assist",
    "drive_select": "Drive select",
    "adaptief_onderstel": "Adaptief onderstel",
    "emergency_assist": "Noodrem-assistent",
    "side_assist": "Dodehoek-assistent",
    "ambient_lighting": "Sfeerverlichting",
    "elektrische_achterklep": "Elektrische achterklep",
    "optik_pakket_zwart": "Optik pakket zwart",
    "dynamisch_knipperlicht": "Dynamisch knipperlicht",
}

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
Je bent een auto-expert gespecialiseerd in Audi Q3 (45 TFSI e, ~2020-2024). Lees de volledige advertentietekst hieronder en bepaal welke opties aanwezig zijn.

BELANGRIJK: Detecteer zo VEEL mogelijk. Als er enige aanwijzing is dat een feature aanwezig is, zet op true. Liever een false positive dan een gemiste feature.

De tekst is vaak Duits (mobile.de). Lees ALLES: titel, Sonderausstattung, Serienausstattung, Pakete, losse vermeldingen, bullet lists, etc.

## REGELS

1. SERIENAUSSTATTUNG = AANWEZIG. Features onder "Serienausstattung" zijn standaard en AANWEZIG op de auto.

2. SAMENGESTELDE TERMEN SPLITSEN:
   "Spurwechsel- u. Spurhalteassistent" → side_assist=true EN lane_assist=true
   "Sitz- u. Spiegelheizung" → stoelverwarming=true
   "Front- u. Rückfahrkamera" → camera_achteruit=true

3. PAKKET-HERKENNING — als een pakket vermeld wordt, zijn ALLE features uit dat pakket aanwezig:
   • "Assistenzpaket Tour" / "Assistenz-Paket Tour" → acc=true, lane_assist=true, emergency_assist=true
   • "Assistenzpaket" / "Assistenz-Paket" (zonder Tour) → lane_assist=true, emergency_assist=true
   • "Assistenzpaket Parken" / "Park-Paket" → camera_360=true, camera_achteruit=true
   • "Adaptiver Fahrassistent" → acc=true, lane_assist=true, travel_assist=true
   • "Business-Paket" / "Business Paket" → stoelverwarming=true
   • "S line" / "S-Line" (zonder verdere specificatie) → s_line=true, s_line_exterieur=true
   • "S line Paket" / "S line Sportpaket" / "S-Line Paket" → s_line=true, s_line_exterieur=true (bevat altijd beide)
   • "Top View Kamera" / "Umgebungskameras" → camera_360=true, camera_achteruit=true
   • "Komfort-Paket" / "Komfortpaket" → kan keyless bevatten
   • "Ambiente Lichtpaket" → ambient_lighting=true

4. GERMAN-DUTCH MAPPINGS (herken alle varianten!):
   • Sitzheizung / beheizbare Sitze / Sitz-u.Spiegelheizung → stoelverwarming
   • Lenkradheizung / beheizbares Lenkrad → stuurverwarming
   • Spurhalteassistent / Spurführungsassistent / Spurverlassenswarnung → lane_assist
   • Spurwechselassistent / Side Assist → side_assist (dodehoek)
   • Abstandstempomat / adaptive Geschwindigkeitsregelung / ACC → acc
   • Audi drive select / Fahrmodus → drive_select
   • Komfortschlüssel / Keyless → keyless
   • Rückfahrkamera / Heckkamera → camera_achteruit
   • Umgebungskameras / 360-Grad / Top View / Area View → camera_360
   • Panorama-Glasdach / Panoramadach / Schiebedach → panoramadak
   • Elektrische Heckklappe / elektr. Heckklappe → elektrische_achterklep
   • Ambientebeleuchtung / Ambiente-Licht / Konturfarbenes Ambiente-Licht → ambient_lighting
   • Dynamischer Blinker / dynamisches Blinklicht / Lauflicht → dynamisch_knipperlicht
   • Einparkhilfe / Park Assist → (niet in score, maar helpt bij camera detectie)

5. IMPLICIETE FEATURES:
   • camera_360=true → camera_achteruit=true automatisch
   • travel_assist=true → acc=true EN lane_assist=true automatisch
   • "S line" zonder specificatie → s_line=true EN s_line_exterieur=true
   • "S line Paket" of "S line Sportpaket" → s_line=true EN s_line_exterieur=true (tenzij expliciet alleen interieur of exterieur vermeld)

6. ZOEK BREED — features kunnen overal staan:
   • In de titel ("S line", "Panorama")
   • In bullet lists ("• Sitzheizung")
   • In lopende tekst ("mit Panoramadach und Sitzheizung")
   • In code/afkortingen ("ACC", "LED", "PDC")
   • In afgekorte vorm ("elektr. Heckkl.", "Komfortschl.", "Rückfahrk.")
   • Als onderdeel van langere woorden ("Abstandstempomat", "Panoramaglasdach")

Bepaal voor elk true of false:

1. panoramadak — Panoramadak/schuifdak/glasdak
2. keyless — Keyless/Komfortschlüssel/sleutelloos
3. camera_achteruit — Rückfahrkamera (ook als 360° camera aanwezig is)
4. camera_360 — 360°/Umgebungskameras/Top View Kamera/Area View (ook "Umgebungskameras" zonder 360° vermelding = TRUE)
5. s_line — S-Line interieur (ook als alleen "S line" staat)
6. s_line_exterieur — S-Line exterieur (ook als alleen "S line" staat)
7. matrix_led — Matrix LED koplampen (gewone LED NIET)
8. velgen_19_20 — 19/20 inch velgen
9. audio_premium — B&O/Sonos/Bose (standaard Audi sound NIET)
10. elektrische_stoelen — Elektrisch verstelbare stoelen
11. stoelen_memory — Memory stoelen
12. stoelverwarming — Sitzheizung/stoelverwarming/Business-Paket
13. stuurverwarming — Lenkradheizung/stuurverwarming
14. acc — Abstandstempomat/adaptive cruise (gewone tempomat NIET)
15. lane_assist — Spurhalteassistent/Spurverlassenswarnung/lane assist (dodehoek=side_assist, NIET hier)
16. travel_assist — TRUE als één van deze geldt:
    a) "Adaptiver Fahrassistent" wordt genoemd
    b) OF de COMBINATIE van ACC (Abstandstempomat/adaptive cruise/Stop&Go) EN actieve stuursturing (Spurhalteassistent/Spurführungsassistent/Lenk- und Spurführungsassistent/Lane assist) is aanwezig
    BELANGRIJK: Lane assist alleen ≠ Travel Assist. ACC alleen ≠ Travel Assist. Pas als BEIDE aanwezig zijn = Travel Assist
17. drive_select — Audi drive select/Fahrmodus/rijmodi
18. adaptief_onderstel — Adaptives Fahrwerk/Dämpferregelung (Sportfahrwerk NIET)
19. emergency_assist — Pre sense/Front Assist/Notbremsassistent
20. side_assist — Spurwechselassistent/Side Assist/dodehoek
21. ambient_lighting — Ambientebeleuchtung/sfeerverlichting/Ambiente Lichtpaket
22. elektrische_achterklep — Elektrische Heckklappe
23. optik_pakket_zwart — Optikpaket Schwarz/Black Style/Black Edition
24. dynamisch_knipperlicht — Dynamischer Blinker/dynamisches Blinklicht/Lauflicht

25. color — Exterieur kleur van de auto (bijv. "Navarrablau Metallic", "Mythos Schwarz", "Glacier Weiß"). Zoek naar: Farbe, Außenfarbe, Lackierung, Metallic-Lackierung. Geef de exacte kleur terug als string, of "" als niet gevonden.

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

        # Travel Assist: als ACC + lane_assist beide true → travel_assist ook true
        if features_dict.get("acc", False) and features_dict.get("lane_assist", False):
            if not features_dict.get("travel_assist", False):
                log.info("[AI] Travel Assist afgeleid: ACC + lane_assist beide aanwezig")
            features_dict["travel_assist"] = True

        # Travel Assist impliceert acc + lane_assist
        if features_dict.get("travel_assist", False):
            features_dict["acc"] = True
            features_dict["lane_assist"] = True

        # S-line Paket/Sportpaket → altijd beide interieur + exterieur
        text_lower = text.lower()
        if re.search(r"s[\s-]?line\s*(?:sport)?paket", text_lower, re.IGNORECASE):
            if not features_dict.get("s_line", False) or not features_dict.get("s_line_exterieur", False):
                log.info("[AI] S-line Paket gedetecteerd → beide interieur + exterieur")
            features_dict["s_line"] = True
            features_dict["s_line_exterieur"] = True

        found = [f for f in FULL_OPTION_FEATURES if features_dict.get(f, False)]
        # stoelen_memory is niet in FULL_OPTION_FEATURES maar wel relevant voor display
        if features_dict.get("stoelen_memory", False):
            found.append("stoelen_memory")
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


def score_listing(listing: Listing) -> Listing:
    """Score listing via Claude AI."""
    result = score_listing_ai(listing)
    if result is not None:
        return result

    log.error("AI scoring mislukt voor %s — listing wordt overgeslagen", listing.id[:30])
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
    if "sportback" in title_lower:
        model_tag = "Q3 Sportback"
    else:
        model_tag = "Q3"

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
        name = FEATURE_DISPLAY_NAMES.get(f, f)
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
        f"<b>Audi {model_tag} 45 TFSI e</b>\n"
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


def scrape_do_fetch(url: str, render: bool = False, retries: int = 1, super_mode: bool = True, timeout: int = 45, render_wait: int = 5000, geo_code: str = "", wait_selector: str = "", play_with_browser: list | None = None) -> str | None:
    """Haal een pagina op via Scrape.do met retry logica.

    super=true activeert geavanceerde anti-bot bypass (10 credits per request).
    super=false gebruikt standaard modus (1 credit per request).
    render=true activeert JS rendering (extra credits).
    geo_code=de routeert via een Duits IP (belangrijk voor mobile.de).
    wait_selector=".class" wacht tot een CSS element geladen is.
    play_with_browser=[...] voert browser acties uit (bijv. consent klikken).
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
            if render:
                params["render"] = "true"
                params["wait"] = str(render_wait)
                params["blockResources"] = "true"
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
            if "gesponsert" in card_full_text.lower()[:50]:
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

            title = re.sub(r"^Gesponsert\s*", "", title, flags=re.IGNORECASE)

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

    # ── Detail pages parallel ophalen ──
    if fetch_details and listings:
        urls_to_fetch = {i: lst.url for i, lst in enumerate(listings) if lst.url}
        log.info("Detail pages ophalen voor %d listings (parallel) ...", len(urls_to_fetch))

        def _clean_detail_url(url: str) -> str:
            """Strip zoekparameters van detail URL — alleen id behouden."""
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if "id" in params:
                return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?id={params['id'][0]}"
            return url

        # Cookie consent klik-acties voor mobile.de (Usercentrics GDPR wall)
        # Usercentrics gebruikt Shadow DOM — gewone CSS selectors werken niet.
        # We moeten via JavaScript in de Shadow DOM de accept button klikken.
        CONSENT_ACTIONS = [
            {"Action": "Wait", "Timeout": 1500},
            {"Action": "Execute", "Execute": "const host = document.getElementById('usercentrics-root'); if (host && host.shadowRoot) { const btn = host.shadowRoot.querySelector('button[data-testid=\"uc-accept-all-button\"]'); if (btn) btn.click(); }"},
            {"Action": "Wait", "Timeout": 1000},
        ]

        def _fetch_detail(idx_url):
            idx, url = idx_url
            clean_url = _clean_detail_url(url)
            if clean_url != url:
                log.info("Detail URL gecleaned: %s", clean_url[:80])
            html = scrape_do_fetch(
                clean_url, render=True, super_mode=True, retries=1, timeout=45,
                render_wait=3000, geo_code="de", play_with_browser=CONSENT_ACTIONS,
            )
            # Als response te klein is (consent wall niet weg), max 2 retries
            retry_config = [(2, 5000), (4, 8000)]
            for attempt, (wait, rw) in enumerate(retry_config, 1):
                if not html or len(html) >= 5000:
                    break
                log.info("Detail te klein (%d bytes), retry %d/%d na %ds (render_wait=%dms) ... %s",
                         len(html), attempt, len(retry_config), wait, rw, clean_url[:80])
                time.sleep(wait)
                html = scrape_do_fetch(
                    clean_url, render=True, super_mode=True, retries=0, timeout=60,
                    render_wait=rw, geo_code="de", play_with_browser=CONSENT_ACTIONS,
                )
            return idx, html

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_detail, item): item for item in urls_to_fetch.items()}
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
                            log.warning("Detail geblokkeerd: %s (body %d chars)", lst.title[:40], len(body_text))
                    elif detail_html:
                        log.warning("Detail te klein: %s (%d bytes)", lst.title[:40], len(detail_html))
                        lst.detail_incomplete = True
                except Exception as e:
                    log.warning("Detail fetch fout: %s", e)

        # Log welke listings GEEN detail page kregen
        for lst in listings:
            if len(lst.description) <= 500:
                lst.detail_incomplete = True
                log.warning("GEEN detail page voor: %s (desc=%d chars) — scoring onbetrouwbaar!", lst.title[:50], len(lst.description))
    return listings


# ── Main ────────────────────────────────────────────────────────────────────


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

    if not SCRAPE_DO_TOKEN:
        log.error("SCRAPE_DO_TOKEN niet geconfigureerd — kan niet scrapen")
        return

    log.info("Methode: Scrape.do API (super=true, DataDome bypass)")

    conn = init_db()

    all_listings: list[Listing] = []
    seen_ids: set[str] = set()

    # ── mobile.de ──
    for search_cfg in MOBILE_DE_SEARCH_URLS:
        search_url = search_cfg["url"]
        url_label = search_cfg["label"]
        require_pano = search_cfg["require_pano_in_desc"]
        require_text = search_cfg.get("require_text", "")

        log.info("━━━ %s ━━━", url_label)

        # Altijd details ophalen voor AI scoring + pano check
        mobile_listings = scrape_mobile_de(conn, search_url=search_url, fetch_details=True)

        if mobile_listings:
            log.info("[%s] %d listings gevonden", url_label, len(mobile_listings))
        else:
            log.warning("[%s] 0 listings", url_label)

        for lst in mobile_listings:
            if lst.id in seen_ids:
                log.info("[%s] Overgeslagen (al in andere URL): %s", url_label, lst.title[:40])
                continue
            # Filter op vereiste tekst in titel of beschrijving
            if require_text and require_text.lower() not in (lst.title + " " + lst.description).lower():
                log.info("[%s] Overgeslagen (geen '%s' in titel/beschrijving): %s", url_label, require_text, lst.title[:40])
                continue
            seen_ids.add(lst.id)
            # Tag listing met require_pano flag voor AI check later
            lst._require_pano = require_pano
            all_listings.append(lst)

        log.info("[%s] %d listings toegevoegd", url_label, len(mobile_listings))

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

    for listing in all_listings:
        # Skip listings waar AI scoring mislukt is
        if listing.score < 0:
            log.warning("Listing overgeslagen (AI scoring mislukt): %s", listing.title[:40])
            continue

        is_new = not listing_exists(conn, listing.id)

        # URL 2 pano check: laat Claude AI bepalen of panoramadak aanwezig is
        require_pano = getattr(listing, '_require_pano', False)
        if require_pano and "panoramadak" not in listing.features:
            log.info("[pano-AI] Overgeslagen (AI zegt geen pano): %s | features=%s",
                     listing.title[:40], listing.features)
            save_listing(conn, listing)  # wel opslaan zodat we 'm niet opnieuw checken
            continue

        if is_new:
            new_count += 1
            sent = send_telegram(listing)
            if sent:
                # Alleen opslaan NA succesvolle Telegram send
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


if __name__ == "__main__":
    main()
