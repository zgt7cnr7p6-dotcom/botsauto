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
# URL 2: Q3 Sportback hybrid — alleen doorsturen als beschrijving panoramadak/glasdach/schuifdak bevat
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


# ── Feature scoring ────────────────────────────────────────────────────────


FEATURE_PATTERNS = {
    "panoramadak": [
        r"panorama\s*d[ao][ck]h?",
        r"panoramadach",
        r"pano\b",
        r"panoramic\s*(?:roof|glass|sun\s*roof|glas\s*(?:dach|roof))?",
        r"panorama\s*glas",
        r"panorama\s*(?:glass\s*)?roof",
        r"panorama\s*schie?be?\s*dach",
        r"panorama\s*dak",
        r"panoramaverglasung",
        r"panorama[\-\s]schiebedach",
        r"panorama[\-\s]glasdach",
        r"glas\s*dach",
        r"glasdach",
        r"glass\s*roof",
        r"schie?be?\s*dach",
        r"schiebedach",
        r"sun\s*roof",
        r"sunroof",
        r"schuif\s*dak",
        r"schuifdak",
        r"glas\s*dak",
    ],
    "keyless": [
        r"keyless",
        r"komfort\s*schl[üu]ssel",
        r"komfortschl[üu]ssel",
        r"schl[üu]ssel\s*los",
        r"convenience\s*key",
        r"sleutel\s*loos",
        r"kessy",
        r"schl[üu]ssellose",
        r"komfort\s*zugang",
        r"komfortzugang",
    ],
    "camera_achteruit": [
        r"r[üu]ckfahr\s*kamera",
        r"r[üu]ckfahrkamera",
        r"rear\s*view\s*camera",
        r"rear\s*camera",
        r"achteruit\s*rij\s*camera",
        r"parking\s*camera",
        r"einpark\s*kamera",
        r"r[üu]ck\s*kamera",
        r"heck\s*kamera",
    ],
    "camera_360": [
        r"360[\s°]*camera",
        r"360[\s°]?grad[\s-]*kamera",
        r"rundum[\s-]*kamera",
        r"surround\s*view",
        r"umgebungs\s*kamera",
        r"umgebungskamera",
        r"4\s*kamera",
        r"area\s*view",
        r"audi\s*area\s*view",
        r"assist[ae]nz.?paket\s*park",
        r"top\s*view",
    ],
    "s_line": [
        r"s[\s-]?line",
        r"s-line",
        r"sline",
    ],
    "s_line_exterieur": [
        r"s[\s-]?line\s*ext",
        r"s-line\s*ext",
        r"s[\s-]?line.*au[ßs]en",
        r"s[\s-]?line.*paket\s*ext",
        r"ext.*s[\s-]?line",
    ],
    "matrix_led": [
        r"matrix[\s-]*led",
        r"matrix[\s-]*licht",
        r"matrix[\s-]*scheinwerfer",
        r"led[\s-]*matrix",
        r"matrixbeam",
        r"digital.*matrix",
    ],
    "velgen_19_20": [
        r"(?:19|20)[\s-]*zoll",
        r"(?:19|20)[\s-]*inch",
        r"(?:19|20)\s*\"",
        r"alufelgen\s*(?:19|20)",
        r"felgen\s*(?:19|20)",
        r"(?:19|20)['″\"]?\s*alu",
        r"leichtmetallfelgen\s*(?:19|20)",
        r"(?:19|20).*leichtmetall",
    ],
    "audio_premium": [
        r"bang[\s&+]*olufsen",
        r"b[\s&+]*o\b",
        r"b&o",
        r"sonos",
        r"premium\s*sound",
        r"sonos\s*premium",
    ],
    "elektrische_stoelen": [
        r"elektrische?\s*stoel",
        r"elektr.*sitz.*verstellung",
        r"el\.\s*sitz\s*verstellung",
        r"sitzverstellung.*elektr",
        r"power\s*seat",
        r"electric\s*seat",
        r"elektrisch\s*verstelba",
        r"sitze?\s*elektr",
        r"komfort\s*sitze?",
        r"elektr\.\s*sitz\s*einstellung",
        r"elektrische?\s*sitz\s*einstellung",
    ],
    "stoelen_memory": [
        r"memory\s*(?:sitze?|paket|funktion|seat)?",
        r"memory\s*stoel",
        r"sitz.*memory",
        r"stoel.*memory",
    ],
    "stoelverwarming": [
        r"stoel\s*verwarming",
        r"sitz\s*heizung",
        r"sitzheizung",
        r"sitzheizung\s*vorn",
        r"verwarmde?\s*stoel",
        r"beheizbare?\s*sitz",
        r"beheizt.*sitz",
        r"sitz.*beheizt",
        r"heated\s*seat",
        r"business.?paket",
    ],
    "stuurverwarming": [
        r"lenkrad\s*beheizt",
        r"lenkrad\s*heizung",
        r"lenkradheizung",
        r"stuur\s*verwarming",
        r"verwarm.*stuur",
        r"heated\s*steering",
        r"beheizbares?\s*lenkrad",
        r"beheizbar.*lenkrad",
        r"lenkrad.*beheiz",
        r"multifunktionslenkrad.*heiz",
    ],
    "acc": [
        r"abstands?\s*tempo\s*mat",
        r"abstandstempomat",
        r"adaptive?\s*cruise",
        r"acc\b",
        r"adaptieve?\s*cruise",
        r"distronic",
        r"abstands\s*regel\s*tempomat",
        r"adaptive?\s*geschwindigkeits\s*regel",
        r"adaptiv.*fahr\s*assist",
        r"adaptiver\s*fahr\s*assistent",
        r"geschwindigkeits\s*regel\s*anlage",
        r"assist[ae]nz.?paket\s*tour",
    ],
    "lane_assist": [
        r"spur\s*halte\s*assist",
        r"spurhalteassist",
        r"lane\s*assist",
        r"spur\s*assistent",
        r"spurassistent",
        r"spur\s*f[üu]hrungs?\s*assist",
        r"spurf[üu]hrungsassist",
        r"adaptiv.*fahr\s*assist",
        r"adaptiver\s*fahr\s*assistent",
        r"fahrassistent",
        r"assist[ae]nz.?paket\s*tour",
    ],
    "travel_assist": [
        r"adaptiver\s*fahr\s*assistent",
        r"adaptiv.*fahr\s*assist",
        r"adaptive\s*cruise\s*assist",
        r"adaptiver\s*fahr\s*assistent\s*inkl",
    ],
    "drive_select": [
        r"drive\s*select",
        r"audi\s*drive\s*select",
        r"fahrmodus",
        r"rijmodus",
        r"fahrprofil",
        r"fahrdynamik\s*regelung",
        r"modi.*individuell",
        r"dynamic\s*mode",
    ],
    "adaptief_onderstel": [
        r"adaptie[fv].*onderstel",
        r"adaptiv.*fahrwerk",
        r"adaptive[s]?\s*fahrwerk",
        r"damper\s*control",
        r"magnetic\s*ride",
        r"dynamic\s*chassis",
        r"d[äa]mpfer\s*regelung",
        r"d[äa]mpferregelung",
        r"adaptive\s*d[äa]mpfer",
        r"DCC",
    ],
    "emergency_assist": [
        r"notbrems?\s*assist",
        r"notbremsassist",
        r"emergency\s*assist",
        r"pre\s*sense",
        r"audi\s*pre\s*sense",
        r"front\s*assist",
        r"nood\s*rem\s*assist",
        r"assist[ae]nz.?paket(?!\s*park)",
        r"assist[ae]nz.?paket\s*tour",
    ],
    "side_assist": [
        r"side\s*assist",
        r"audi\s*side\s*assist",
        r"spur\s*wechsel\s*assist",
        r"spurwechselassist",
        r"totwinkel",
        r"tot[\s-]*winkel",
        r"dode\s*hoek",
        r"blind\s*spot",
    ],
    "ambient_lighting": [
        r"ambient[ae]?\s*beleuchtung",
        r"ambientebeleuchtung",
        r"ambient\s*light",
        r"sfeer\s*verlichting",
        r"sfeerverlichting",
        r"contour\s*verlichting",
        r"innenraum\s*beleuchtung\s*plus",
        r"ambiente\s*beleuchtung",
        r"ambient\s*lighting",
        r"kontur.*ambiente.*licht",
        r"ambiente.*licht.*paket",
        r"licht\s*paket.*ambiente",
        r"ambiente\s*licht\s*paket\s*plus",
        r"mehrfarbig.*ambiente",
        r"ambiente.*mehrfarbig",
    ],
    "elektrische_achterklep": [
        r"elektr.*heckklappe",
        r"heckklappe.*elektr",
        r"elektrische?\s*achterklep",
        r"power\s*tailgate",
        r"elektr.*kofferraum.*klappe",
        r"automatische?\s*heckklappe",
        r"sensorgesteuerte?\s*heckklappe",
        r"komfort\s*heckklappe",
        r"heck.*klappe.*komfort",
    ],
    "optik_pakket_zwart": [
        r"optik\s*paket\s*schwarz",
        r"schwarz.*optik\s*paket",
        r"black\s*(?:optic|style)\s*(?:paket|pack)",
        r"optik\s*pakket\s*zwart",
        r"zwart\s*optik",
        r"s[\s-]?line\s*black",
        r"black\s*edition",
        r"black\s*paket",
        r"schwarzpaket",
        r"schwarz.*anbauteile",
    ],
}


def parse_color(text: str) -> str:
    """Extraheer de exterieur kleur uit een beschrijving."""
    for pat in [
        r"Au[ßs]en\s*farbe[:\s]+([A-ZÄÖÜa-zäöüß][\w\s-]{2,30})",
        r"Farbe[:\s]+([A-ZÄÖÜa-zäöüß][\w\s-]{2,30})",
        r"Exterieur\s*farbe[:\s]+([A-ZÄÖÜa-zäöüß][\w\s-]{2,30})",
        r"Lack(?:ierung)?[:\s]+([A-ZÄÖÜa-zäöüß][\w\s-]{2,30})",
        r"colour?[:\s]+([A-Za-z][\w\s-]{2,30})",
        r"color[:\s]+([A-Za-z][\w\s-]{2,30})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            color = m.group(1).strip().rstrip(".,;")
            if len(color) > 2 and not any(w in color.lower() for w in ["fahrzeug", "ausstattung", "technisch"]):
                return color

    known_colors = [
        "Mythos Schwarz", "Nano Grau", "Chronos Grau", "Glacier Wei",
        "Turbo Blau", "Pulse Orange", "Atoll Blau", "Florett Silber",
        "Manhattangrau", "Manhattan Grau", "Daytona Grau", "Perleffekt",
        "Schwarz", "Weiss", "Weiß", "Grau", "Silber", "Blau", "Rot",
        "Grün", "Braun", "Orange", "Metallic",
    ]
    text_lower = text.lower()
    for color in known_colors:
        if color.lower() in text_lower:
            idx = text_lower.index(color.lower())
            snippet = text[max(0, idx):idx + 40].strip()
            words = snippet.split()[:4]
            result = " ".join(words).rstrip(".,;")
            if len(result) > 2:
                return result
    return ""


def score_listing_regex(listing: Listing) -> Listing:
    """Regex-based scoring (fallback als Claude API niet beschikbaar is)."""
    text = f"{listing.title} {listing.description}".lower()
    found = []
    for feature, patterns in FEATURE_PATTERNS.items():
        if feature not in FULL_OPTION_FEATURES:
            continue
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                found.append(feature)
                log.info("[REGEX] Feature '%s' gevonden via '%s' => '%s'", feature, pat, m.group())
                break
    # 360° camera impliceert altijd achteruitrijcamera
    if "camera_360" in found and "camera_achteruit" not in found:
        found.append("camera_achteruit")

    # Detecteer stoelen_memory apart (niet in score, maar wel in display)
    has_memory = False
    for pat in FEATURE_PATTERNS.get("stoelen_memory", []):
        if re.search(pat, text, re.IGNORECASE):
            has_memory = True
            break
    if has_memory:
        found.append("stoelen_memory")

    listing.features = found
    listing.score = len([f for f in found if f in FULL_OPTION_FEATURES])

    if not listing.color:
        listing.color = parse_color(listing.description)

    missing = [f for f in FULL_OPTION_FEATURES if f not in found]
    if missing:
        log.info(
            "[REGEX] Score %d/%d %s: MISSING=%s",
            listing.score, len(FULL_OPTION_FEATURES),
            listing.id[:30], missing,
        )
    return listing


# ── Claude AI scoring ─────────────────────────────────────────────────────

AI_SCORING_PROMPT = """\
Je bent een auto-expert gespecialiseerd in Audi Q3 (45 TFSI e, ~2020-2024). Lees de volledige advertentietekst hieronder (titel + beschrijving) en bepaal welke van de volgende opties aanwezig zijn.

De tekst is vaak in het Duits (Sonderausstattung, Serienausstattung, etc.) — je begrijpt Duits, dus lees gewoon alles en begrijp wat er staat. ELKE sectie telt mee, ook Serienausstattung.

KRITISCH — SERIENAUSSTATTUNG (standaarduitrusting):
Features die onder "Serienausstattung" staan ZIJN AANWEZIG op de auto. Dit is standaarduitrusting die erbij zit.
Voorbeeld: "Audi drive select" staat vaak onder Serienausstattung → drive_select=true.
Negeer deze sectie NOOIT. Alles wat daar staat, is aanwezig.

KRITISCH — SAMENGESTELDE DUITSE TERMEN SPLITSEN:
Audi combineert vaak meerdere features in één zin met "u." (und) of komma's. Je MOET deze splitsen en ELKE feature apart herkennen.

Voorbeeld: "Spurwechsel- u. Spurhalteassistent (Side Assist und Lane Assist)"
→ Dit zijn TWEE features:
  1. Spurwechselassistent = side_assist (dodehoek) = true
  2. Spurhalteassistent = lane_assist = true
→ NIET als één feature behandelen!

Meer voorbeelden van splitsen:
• "Sitz- u. Spiegelheizung" → stoelverwarming=true + spiegelverwarming
• "Komfort- u. S line-Paket" → beide pakketten herkennen
• "Front- u. Rückfahrkamera" → camera_achteruit=true

BELANGRIJK — PAKKET-HERKENNING:
Audi verkoopt veel opties als pakket. Je MOET pakketten herkennen en de individuele features daaruit afleiden:

• "Assistenzpaket Tour" / "Assistenz-Paket Tour" → bevat: ACC (adaptieve cruise) + Lane Assist + Traffic Sign Recognition + Emergency Assist. Dus als dit pakket staat: acc=true, lane_assist=true, emergency_assist=true
• "Assistenzpaket" / "Assistenz-Paket" (basis) → bevat: Lane Assist + Front Assist (pre sense). Dus: lane_assist=true, emergency_assist=true
• "Assistenzpaket Parken" / "Assistenz-Paket Parken" → bevat: Park assist + 360° camera + parkeer sensoren. Dus: camera_360=true, camera_achteruit=true
• "Adaptiver Fahrassistent" → bevat: ACC + Lane Assist + Travel Assist (gecombineerd systeem). Dus: acc=true, lane_assist=true, travel_assist=true
• "Business-Paket" / "Business Paket" → bevat: MMI Navigation plus + Audi phone box + Stoelverwarming (Sitzheizung) + spiegel functies. Dus: stoelverwarming=true
• "S line Paket" / "S line" / "S-Line" → bevat: Sportstoelen + S-line interieur + S-line exterieur. Dus: s_line=true, s_line_exterieur=true
• "Optikpaket Schwarz" / "Black Style" / "Black Edition" → optik_pakket_zwart=true
• "Ambiente Lichtpaket plus" / "Ambiente-Beleuchtung" / "Ambiente Lichtpaket" → ambient_lighting=true
• "Matrix LED" / "Matrix LED-Scheinwerfer" → matrix_led=true (gewone LED NIET)
• "Komfortschlüssel" → keyless=true
• "Komfort-Paket" → kan keyless entry bevatten, check context

BELANGRIJKE GERMAN-DUTCH MAPPINGS:
• "Sitzheizung" / "Sitzheizung vorn" = stoelverwarming
• "Lenkradheizung" / "beheizbares Lenkrad" = stuurverwarming
• "Spurhalteassistent" / "Spurführungsassistent" = lane_assist
• "Spurwechselassistent" / "Spurwechselwarnung" / "Side Assist" = side_assist (dodehoek)
• "Audi drive select" / "Fahrmodus" = drive_select
• "Adaptives Fahrwerk" / "Dämpferregelung" = adaptief_onderstel (ALLEEN deze termen! "Sportfahrwerk" of "Sport-Fahrwerk" is NIET adaptief)
• "Abstandstempomat" / "Adaptive Geschwindigkeitsregelung" / "ACC" = acc
• "Elektrische Heckklappe" = elektrische_achterklep
• "Rückfahrkamera" = camera_achteruit
• "Panorama-Glasdach" / "Panoramadach" = panoramadak

Bepaal voor elk van deze opties true of false:

1. panoramadak — Panoramadak of schuifdak (heel glasdak)
2. keyless — Keyless entry / comfort access / Komfortschlüssel (sleutelloos openen)
3. camera_achteruit — Achteruitrijcamera (ook true als er een 360° camera is, of Assistenzpaket Parken)
4. camera_360 — 360°-rondomzichtcamera / Audi Area View (ook via Assistenzpaket Parken)
5. s_line — S-Line interieur of exterieurpakket (als er S-Line staat zonder specificatie, zet beide op true)
6. s_line_exterieur — Specifiek S-Line exterieurpakket (als er S-Line staat zonder specificatie, zet beide op true)
7. matrix_led — Matrix LED-koplampen (gewone LED zonder "Matrix" telt NIET)
8. velgen_19_20 — 19 of 20 inch velgen (18 inch of kleiner telt NIET)
9. audio_premium — Premium audiosysteem van een merk: Bang & Olufsen, Sonos of Bose (een standaard "Sound-System" of "Audi Sound-System" zonder merknaam telt NIET)
10. elektrische_stoelen — Elektrisch verstelbare voorstoelen (zonder memory)
11. stoelen_memory — Memory-functie voor stoelen (specifiek "Memory" bij de stoelen)
12. stoelverwarming — Stoelverwarming (voor) / Sitzheizung / ook via Business-Paket
13. stuurverwarming — Verwarmbaar stuur / Lenkradheizung
14. acc — Adaptive cruise control (Abstandstempomat / adaptieve afstandsregeling). Ook via "Adaptiver Fahrassistent" of "Assistenzpaket Tour". Gewone cruise/tempomat ZONDER "adaptive"/"Abstand" telt NIET
15. lane_assist — Rijstrookassistent. Ook via "Adaptiver Fahrassistent" of "Assistenzpaket Tour" of "Assistenzpaket". Spurhalteassistent / Spurführungsassistent. NIET de dodehoek (die staat apart bij side_assist)
16. travel_assist — Travel Assist / Adaptiver Fahrassistent: dit is een GECOMBINEERD SYSTEEM (ACC + actieve lane assist + lane centering + semi-autonoom rijden). ALLEEN true als er "Adaptiver Fahrassistent" of "Adaptive cruise assist" of "Adaptiver Fahrassistent inkl. Notfallassistent" staat. NIET verwarren met: "Adaptive Geschwindigkeitsassistent" (alleen ACC), "Spurverlassenswarnung" (basis lane assist), "Spurhalteassistent" (geen volledig systeem). Als travel_assist=true, dan ook acc=true en lane_assist=true
17. drive_select — Rijmodi / Audi drive select / Fahrmodus
18. adaptief_onderstel — ALLEEN "Adaptives Fahrwerk" of "Dämpferregelung" of "DCC". "Sportfahrwerk" / "Sport-Fahrwerk" is NIET adaptief en telt NIET
19. emergency_assist — Noodremassistent / pre sense / Front Assist. Ook via Assistenzpaket of Assistenzpaket Tour
20. side_assist — Dodehoekassistent / Side Assist / Spurwechselassistent / Spurwechselwarnung
21. ambient_lighting — Sfeer-/ambienteverlichting / Ambiente Lichtpaket (plus)
22. elektrische_achterklep — Elektrische achterklep / elektrische Heckklappe
23. optik_pakket_zwart — Zwart optiekpakket (Optikpaket Schwarz / Black Style / Black Edition)

WERKWIJZE:
1. Lees ALLE tekst inclusief ELKE sectie: Sonderausstattung, Serienausstattung, Pakete, Komfort, Technik, etc.
2. Herken pakketten en leid features af (zie mapping hierboven)
3. SPLITS samengestelde Duitse termen (met "u.", "und", komma's) en herken elke feature apart
4. Zoek ook naar losse vermeldingen van features
5. Wees NIET te streng — als een pakket of serienausstattung een feature bevat, is die feature aanwezig
6. Doe SEMANTISCHE analyse — begrijp de BETEKENIS, niet alleen exacte woorden

Antwoord ALLEEN met een JSON object, geen uitleg:
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
  "optik_pakket_zwart": false
}"""


def score_listing_ai(listing: Listing) -> Listing | None:
    """Score een listing via Claude AI. Retourneert None als het niet lukt."""
    if not HAS_ANTHROPIC or not ANTHROPIC_API_KEY:
        return None

    text = f"{listing.title}\n\n{listing.description}"
    log.info("[AI] Beschrijving lengte: %d chars voor %s", len(listing.description), listing.id[:30])
    # Beperk tekst tot ~8000 chars om kosten laag te houden
    if len(text) > 8000:
        text = text[:8000]

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
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

        found = [f for f in FULL_OPTION_FEATURES if features_dict.get(f, False)]
        # stoelen_memory is niet in FULL_OPTION_FEATURES maar wel relevant voor display
        if features_dict.get("stoelen_memory", False):
            found.append("stoelen_memory")
        listing.features = found
        listing.score = len([f for f in found if f in FULL_OPTION_FEATURES])

        if not listing.color:
            listing.color = parse_color(listing.description)

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
    """Score listing via Claude AI, met regex als fallback."""
    # Probeer eerst AI scoring
    result = score_listing_ai(listing)
    if result is not None:
        return result

    # Fallback naar regex
    if ANTHROPIC_API_KEY:
        log.warning("AI scoring mislukt, fallback naar regex voor %s", listing.id[:30])
    return score_listing_regex(listing)


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


def scrape_do_fetch(url: str, render: bool = False, retries: int = 1, super_mode: bool = True, timeout: int = 45) -> str | None:
    """Haal een pagina op via Scrape.do met retry logica.

    super=true activeert geavanceerde anti-bot bypass (10 credits per request).
    super=false gebruikt standaard modus (1 credit per request).
    render=true activeert JS rendering (extra credits).
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
            if render:
                params["render"] = "true"
                params["wait"] = "5000"

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
    html = scrape_do_fetch(search_url, super_mode=True, retries=1)

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

        def _fetch_detail(idx_url):
            idx, url = idx_url
            # render=True zodat "Show more" / features volledig geladen worden
            html = scrape_do_fetch(url, render=True, super_mode=True, retries=1, timeout=60)
            return idx, html

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_fetch_detail, item): item for item in urls_to_fetch.items()}
            for future in as_completed(futures):
                try:
                    idx, detail_html = future.result()
                    lst = listings[idx]
                    if detail_html and len(detail_html) > 5000:
                        detail_soup = BeautifulSoup(detail_html, "html.parser")
                        body_text = detail_soup.get_text(separator=" ", strip=True)
                        if len(body_text) > 100:
                            lst.description = body_text[:20000]
                            log.info("Detail OK: %s (%d chars)", lst.title[:40], len(lst.description))
                        else:
                            log.warning("Detail geblokkeerd: %s (body %d chars)", lst.title[:40], len(body_text))
                    elif detail_html:
                        log.warning("Detail te klein: %s (%d bytes)", lst.title[:40], len(detail_html))
                except Exception as e:
                    log.warning("Detail fetch fout: %s", e)

        # Log welke listings GEEN detail page kregen
        for lst in listings:
            if len(lst.description) <= 500:
                log.warning("GEEN detail page voor: %s (desc=%d chars) — scoring onbetrouwbaar!", lst.title[:50], len(lst.description))
    return listings


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    # Quiet hours: niet scrapen tussen 20:00 en 08:00 CET
    force = "--force" in sys.argv or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not force:
        now_cet = datetime.now(ZoneInfo("Europe/Amsterdam"))
        hour = now_cet.hour
        if hour >= 20 or hour < 8:
            log.info("Quiet hours (%02d:%02d CET) — overslaan (actief 08:00-20:00)", hour, now_cet.minute)
            return
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

    pano_patterns = FEATURE_PATTERNS["panoramadak"]

    def has_pano_in_text(text: str) -> bool:
        text_lower = text.lower()
        return any(re.search(p, text_lower, re.IGNORECASE) for p in pano_patterns)

    # ── mobile.de ──
    for search_cfg in MOBILE_DE_SEARCH_URLS:
        search_url = search_cfg["url"]
        url_label = search_cfg["label"]
        require_pano = search_cfg["require_pano_in_desc"]

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
        # Restore _require_pano flags
        for orig, sc in zip(all_listings, scored):
            sc._require_pano = getattr(orig, '_require_pano', False)
        all_listings = scored

    for listing in all_listings:
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
