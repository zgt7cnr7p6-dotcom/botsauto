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
import random
import sqlite3
import logging
import time
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

# Max aantal detail pagina's per run (beperkt API credits)
MAX_DETAIL_PAGES_PER_RUN = int(os.environ.get("MAX_DETAIL_PAGES", "15"))
_detail_page_count = 0

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
    "matrix_led",
    "velgen_20",
    "audio_premium",
    "elektrische_stoelen",
    "stoelverwarming",
    "stuurverwarming",
    "acc",
    "lane_assist",
    "drive_select",
    "adaptief_onderstel",
    "emergency_assist",
    "side_assist",
    "ambient_lighting",
]

FEATURE_DISPLAY_NAMES = {
    "panoramadak": "Panoramadak",
    "keyless": "Keyless Entry",
    "camera_achteruit": "Achteruitrijcamera",
    "camera_360": "360° Camera",
    "s_line": "S-Line interieur",
    "matrix_led": "Matrix LED",
    "velgen_20": "20 inch velgen",
    "audio_premium": "Premium Audio (SONOS/B&O)",
    "elektrische_stoelen": "Elektrische stoelen + memory",
    "stoelverwarming": "Stoelverwarming",
    "stuurverwarming": "Stuurverwarming",
    "acc": "ACC (Abstandstempomat)",
    "lane_assist": "Lane assist + dodehoek",
    "drive_select": "Drive select",
    "adaptief_onderstel": "Adaptief onderstel",
    "emergency_assist": "Noodrem-assistent",
    "side_assist": "Dodehoek-assistent",
    "ambient_lighting": "Sfeerverlichting",
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
    ],
    "s_line": [
        r"s[\s-]?line",
        r"s-line",
        r"sline",
    ],
    "matrix_led": [
        r"matrix[\s-]*led",
        r"matrix[\s-]*licht",
        r"matrix[\s-]*scheinwerfer",
        r"led[\s-]*matrix",
        r"matrixbeam",
        r"digital.*matrix",
    ],
    "velgen_20": [
        r"20[\s-]*zoll",
        r"20[\s-]*inch",
        r"20\s*\"",
        r"alufelgen\s*20",
        r"felgen\s*20",
        r"20['″\"]?\s*alu",
        r"leichtmetallfelgen\s*20",
        r"20.*leichtmetall",
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
        r"memory\s*(?:sitze?|paket|funktion|seat)?",
        r"sitze?\s*elektr",
        r"komfort\s*sitze?",
        r"elektr\.\s*sitz\s*einstellung",
        r"elektrische?\s*sitz\s*einstellung",
    ],
    "stoelverwarming": [
        r"stoel\s*verwarming",
        r"sitz\s*heizung",
        r"sitzheizung",
        r"verwarmde?\s*stoel",
        r"beheizbare?\s*sitz",
        r"beheizt.*sitz",
        r"sitz.*beheizt",
        r"heated\s*seat",
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
        r"geschwindigkeitsregel",
        r"adaptiv.*fahr\s*assist",
    ],
    "lane_assist": [
        r"spur\s*halte\s*assist",
        r"spurhalteassist",
        r"lane\s*assist",
        r"totwinkel",
        r"tot[\s-]*winkel[\s-]*assist",
        r"blind\s*spot",
        r"dode\s*hoek",
        r"audi\s*side\s*assist",
        r"side\s*assist",
        r"spur\s*wechsel",
        r"spurwechsel",
        r"audi\s*pre\s*sense",
        r"pre\s*sense",
    ],
    "drive_select": [
        r"drive\s*select",
        r"audi\s*drive\s*select",
        r"fahrmodus",
        r"rijmodus",
    ],
    "adaptief_onderstel": [
        r"adaptie[fv].*onderstel",
        r"sport\s*onderstel",
        r"adaptiv.*fahrwerk",
        r"sport[\s-]*fahrwerk",
        r"damper\s*control",
        r"magnetic\s*ride",
        r"dynamic\s*chassis",
        r"fahrwerk\s*sport",
        r"d[äa]mpfer\s*regelung",
        r"d[äa]mpferregelung",
    ],
    "emergency_assist": [
        r"notbrems?\s*assist",
        r"notbremsassist",
        r"emergency\s*assist",
        r"pre\s*sense\s*front",
        r"audi\s*pre\s*sense\s*front",
        r"front\s*assist",
        r"nood\s*rem\s*assist",
        r"assist[ae]nz\s*paket",
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
    listing.features = found
    listing.score = len(found)

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
Je bent een auto-expert die advertenties analyseert voor een Audi Q3 Sportback 45 TFSI e (plug-in hybrid).
Analyseer de titel en beschrijving hieronder en bepaal welke opties aanwezig zijn.

De beschrijving kan in het Duits, Nederlands of Engels zijn. Let op synoniemen en variaties:
- "Sitzheizung" / "Sitzheizung vorne" = stoelverwarming
- "Audi Side Assist" / "Spurwechselassistent" = dodehoek-assistent (onderdeel van lane_assist)
- "Spurhalteassistent" / "Active lane Assist" = rijstrookassistent (onderdeel van lane_assist)
- "Audi Drive Select" / "drive select" = drive_select
- "elektrische Sitzverstellung" = elektrische stoelen (elektrische_stoelen)
- "Memory" bij stoelen = memory functie (onderdeel van elektrische_stoelen)
- "Panorama-Schiebedach" / "Panoramadach" / "Glasdach" = panoramadak
- "Matrix LED" / "Matrix-LED-Scheinwerfer" = matrix_led (LET OP: gewone "LED-Scheinwerfer" is GEEN matrix LED)
- "Bang & Olufsen" / "B&O" / "Sonos" = audio_premium
- "Rückfahrkamera" = camera_achteruit
- "Komfortschlüssel" / "Keyless" / "kessy" = keyless
- "Lenkradheizung" / "beheizbares Lenkrad" = stuurverwarming
- "Abstandstempomat" / "ACC" / "adaptive cruise" = acc
- "adaptives Fahrwerk" / "Sportfahrwerk" / "DCC" = adaptief_onderstel
- "Notbremassistent" / "pre sense front" / "Front Assist" / "Emergency Assist" / "Assistenzpaket" = emergency_assist
- "Audi Side Assist" / "Spurwechselassistent" / "Totwinkelassistent" / "blind spot" = side_assist (LET OP: dit is APART van lane_assist)
- "Ambiente Beleuchtung" / "Ambientebeleuchtung" / "ambient lighting" = ambient_lighting

BELANGRIJK:
- "LED-Scheinwerfer" alleen (zonder "Matrix") is GEEN matrix_led
- "Einparkhilfe" (parkeersensoren) is GEEN camera
- Stoelverwarming en stuurverwarming zijn APART
- lane_assist is ALLEEN aanwezig als er ZOWEL rijstrookassistent (lane assist/Spurhalteassistent) ALS dodehoek-assistent (Side Assist/Spurwechselassistent/Totwinkelassistent) wordt genoemd
- elektrische_stoelen: "elektrische Sitzverstellung" telt, maar memory hoeft er niet bij

Geef je antwoord ALLEEN als een JSON object met exact deze keys, elk true of false:
{
  "panoramadak": false,
  "keyless": false,
  "camera_achteruit": false,
  "camera_360": false,
  "s_line": false,
  "matrix_led": false,
  "velgen_20": false,
  "audio_premium": false,
  "elektrische_stoelen": false,
  "stoelverwarming": false,
  "stuurverwarming": false,
  "acc": false,
  "lane_assist": false,
  "drive_select": false,
  "adaptief_onderstel": false,
  "emergency_assist": false,
  "side_assist": false,
  "ambient_lighting": false
}

Geen uitleg, alleen het JSON object."""


def score_listing_ai(listing: Listing) -> Listing | None:
    """Score een listing via Claude AI. Retourneert None als het niet lukt."""
    if not HAS_ANTHROPIC or not ANTHROPIC_API_KEY:
        return None

    text = f"{listing.title}\n\n{listing.description}"
    # Beperk tekst tot ~8000 chars om kosten laag te houden
    if len(text) > 8000:
        text = text[:8000]

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
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

        found = [f for f in FULL_OPTION_FEATURES if features_dict.get(f, False)]
        listing.features = found
        listing.score = len(found)

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


def send_telegram(listing: Listing):
    max_score = len(FULL_OPTION_FEATURES)

    title_lower = listing.title.lower()
    if "sportback" in title_lower:
        model_tag = "Q3 Sportback"
    else:
        model_tag = "Q3"

    pct = listing.score / max_score if max_score else 0
    if pct >= 0.90:
        verdict_line = "🟢 TOPPER — bijna full option!"
    elif pct >= 0.75:
        verdict_line = "🟢 High spec"
    elif pct >= 0.55:
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

    # Feature check — groepeer in aanwezig / afwezig
    found_lines = []
    missing_lines = []
    for f in FULL_OPTION_FEATURES:
        name = FEATURE_DISPLAY_NAMES.get(f, f)
        if f in listing.features:
            found_lines.append(f"  ✅ {name}")
        else:
            missing_lines.append(f"  ❌ {name}")

    date_line = f"📅 {listing.listing_date}\n" if listing.listing_date else ""

    text = (
        f"<b>Audi {model_tag} 45 TFSI e</b>\n"
        f"{info_line}\n"
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

    text += (
        f"\n"
        f"{date_line}"
        f"📍 {listing.source}\n"
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
    else:
        log.error("Telegram fout: %s %s", resp.status_code, resp.text)


# ── Scrape.do fetch ─────────────────────────────────────────────────────────


def scrape_do_fetch(url: str, render: bool = False, retries: int = 1, super_mode: bool = True) -> str | None:
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

            resp = req_lib.get("https://api.scrape.do", params=params, timeout=90)
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


def scrape_mobile_de(conn, search_url: str = "") -> list:
    """Scrape mobile.de via Scrape.do + BeautifulSoup.

    Haalt zoekpagina op en detail pagina's voor feature-detectie.
    """
    listings = []

    if not SCRAPE_DO_TOKEN:
        log.info("mobile.de: SCRAPE_DO_TOKEN niet geconfigureerd, overslaan")
        return listings

    if not search_url:
        search_url = MOBILE_DE_SEARCH_URL

    # Zoekpagina eerst zonder super mode (1 credit ipv 10)
    # Fallback naar super mode als we geblokkeerd worden
    log.info("mobile.de: zoekpagina ophalen (standaard modus) ...")
    html = scrape_do_fetch(search_url, super_mode=False, retries=0)

    blocked = False
    if html and ("zugriff verweigert" in html.lower() or "access denied" in html.lower()):
        log.warning("mobile.de: geblokkeerd zonder super mode, retry met super=true ...")
        blocked = True
        html = None

    if not html:
        if not blocked:
            log.warning("mobile.de: standaard modus mislukt, retry met super=true ...")
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

    known_streak = 0
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
                known_streak += 1
                log.info("Bekende listing: %s", title[:50])
                if known_streak >= 5:
                    log.info("5 bekende op rij — stoppen")
                    break
                continue
            known_streak = 0

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

            # Detail pagina ophalen voor feature-detectie
            global _detail_page_count
            description = card_text[:500]
            location = ""
            listing_date = ""
            if href and _detail_page_count < MAX_DETAIL_PAGES_PER_RUN:
                _detail_page_count += 1
                log.info("mobile.de: detail ophalen [%d/%d] voor %s ...",
                         _detail_page_count, MAX_DETAIL_PAGES_PER_RUN, title[:50])
                time.sleep(random.uniform(0.3, 0.8))
                detail_html = scrape_do_fetch(href, render=True)
                if detail_html:
                    # Sla eerste detail page op als debug
                    if _detail_page_count <= 2:
                        debug_fn = f"debug_detail_{_detail_page_count}.html"
                        with open(debug_fn, "w", encoding="utf-8") as dbg:
                            dbg.write(detail_html)
                        log.info("Debug HTML opgeslagen: %s (%d bytes)", debug_fn, len(detail_html))

                    detail_soup = BeautifulSoup(detail_html, "html.parser")
                    description = ""

                    # 1. Titel
                    for sel in ["h1", "#rbt-ad-title", "[data-testid='ad-title']"]:
                        el = detail_soup.select_one(sel)
                        if el:
                            description += " " + el.get_text(separator=" ", strip=True)
                            break

                    # 2. Technische data
                    for sel in [
                        "div.cBox-body.cBox-body--technical-data",
                        ".cBox--technicalData",
                        "#rbt-td",
                        "[data-testid='ad-detail-technical-data']",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            description += " " + el.get_text(separator=" ", strip=True)

                    # 3. Features / Ausstattung
                    for sel in [
                        ".cBox--features", "#features",
                        "[data-testid='ad-detail-features']",
                        "[data-testid='ad-detail-equipment']",
                        "[data-testid='equipment']",
                        "[data-testid='features']",
                        "[class*='FeatureList']",
                        "[class*='featureList']",
                        "[class*='equipment']",
                        "[class*='Equipment']",
                        "[class*='Ausstattung']",
                        "[class*='ausstattung']",
                        "#rbt-features",
                        ".cBox--equipment",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            description += " " + el.get_text(separator=" ", strip=True)

                    # 4. Vehicle Description (seller beschrijving met feature-lijsten)
                    for sel in [
                        "#description", ".cBox--vehicleDescription",
                        "[data-testid='ad-detail-description']",
                        "[data-testid='description']",
                        "[data-testid='seller-description']",
                        "[class*='VehicleDescription']",
                        "[class*='vehicleDescription']",
                        "[class*='description-text']",
                        "[class*='DescriptionText']",
                        "[class*='seller-description']",
                        "[class*='SellerDescription']",
                        "[class*='listing-description']",
                        ".cBox--description",
                        "#rbt-description",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            description += " " + el.get_text(separator=" ", strip=True)

                    # Altijd volledige body text toevoegen voor feature-detectie
                    # CSS selectors zijn fragiel en mobile.de wijzigt regelmatig hun DOM
                    # Seller beschrijvingen staan vaak diep in de pagina, dus ruime limiet
                    body_text = detail_soup.get_text(separator=" ", strip=True)
                    if len(description.strip()) < 100:
                        # Selectors faalden, gebruik volledige body text
                        description = body_text[:50000]
                        log.info("Detail: selectors faalden, body text gebruikt (%d chars)", len(description))
                    else:
                        # Voeg body text toe zodat features niet gemist worden
                        description += " " + body_text[:40000]
                        log.info("Detail: selectors + body text (%d chars)", len(description))

                    # Locatie
                    for sel in [
                        "#dealer-hp-link-bottom",
                        ".seller-info .seller-address",
                        "[data-testid='seller-info'] p",
                        ".cBox--seller .u-text-break-word",
                        ".cBox--seller",
                        "#rbt-seller",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            loc_text = el.get_text(separator=" ", strip=True)
                            loc_match = re.search(r"(\d{5}\s+\S+(?:\s+\S+)?)", loc_text)
                            if loc_match:
                                location = loc_match.group(1).strip()
                            elif len(loc_text) < 100:
                                location = loc_text
                            break

                    # Listing datum
                    for sel in [
                        "[data-testid='creation-date']",
                        ".cBox--attributes .u-text-break-word",
                    ]:
                        el = detail_soup.select_one(sel)
                        if el:
                            date_text = el.get_text(strip=True)
                            # "Ad online since 3/11/2026, 12:55" of "11.03.2026"
                            date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4},?\s*\d{1,2}:\d{2})", date_text)
                            if date_match:
                                listing_date = date_match.group(1)
                            else:
                                date_match = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})", date_text)
                                if date_match:
                                    listing_date = date_match.group(1)
                            if listing_date:
                                break
                    if not listing_date:
                        full_text = detail_soup.get_text()
                        # "Ad online since 3/11/2026, 12:55"
                        date_match = re.search(r"[Aa]d\s+online\s+since\s+(\d{1,2}/\d{1,2}/\d{4},?\s*\d{1,2}:\d{2})", full_text)
                        if date_match:
                            listing_date = date_match.group(1)
                        if not listing_date:
                            date_match = re.search(r"Inseriert?\s*(?:am\s*)?:?\s*(\d{1,2}\.\d{1,2}\.\d{4})", full_text)
                            if date_match:
                                listing_date = date_match.group(1)
                            else:
                                date_match = re.search(r"seit\s+(\d{1,2}\.\d{1,2}\.\d{4})", full_text, re.IGNORECASE)
                                if date_match:
                                    listing_date = date_match.group(1)

                    log.info("mobile.de: detail %d chars, locatie=%s, datum=%s",
                             len(description), location or "?", listing_date or "?")

            listing = Listing(
                id=listing_id,
                source="mobile.de",
                title=title,
                price=price,
                year=year,
                km=km,
                url=href,
                description=description,
                location=location,
                listing_date=listing_date,
            )

            log.info("MATCH: %s — €%s — %d km — %d", title[:50], f"{price:,}" if price else "?", km, year)
            listings.append(listing)

        except Exception as e:
            log.warning("mobile.de card: %s", e)
            continue

    log.info("mobile.de: %d listings gevonden", len(listings))
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

        global _detail_page_count
        _detail_page_count = 0

        mobile_listings = scrape_mobile_de(conn, search_url=search_url)

        if mobile_listings:
            log.info("[%s] %d listings gevonden", url_label, len(mobile_listings))
        else:
            log.warning("[%s] 0 listings", url_label)

        for lst in mobile_listings:
            if lst.id in seen_ids:
                log.info("[%s] Overgeslagen (al in andere URL): %s", url_label, lst.title[:40])
                continue
            if require_pano and not has_pano_in_text(f"{lst.title} {lst.description}"):
                log.info("[%s] Overgeslagen (geen pano in beschrijving): %s", url_label, lst.title[:40])
                continue
            seen_ids.add(lst.id)
            all_listings.append(lst)

        log.info("[%s] %d listings toegevoegd", url_label, len(mobile_listings))

    # ── Score en alert ──
    log.info("Totaal: %d listings, nu scoren ...", len(all_listings))

    new_count = 0
    alert_count = 0

    for listing in all_listings:
        listing = score_listing(listing)
        is_new = not listing_exists(conn, listing.id)
        save_listing(conn, listing)

        if is_new:
            new_count += 1
            send_telegram(listing)
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
