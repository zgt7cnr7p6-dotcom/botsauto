# CLAUDE.md — Auto-Alert Scraper

> Lees dit eerst voordat je iets aan dit project verandert. Dit is de
> volledige technische briefing. Het hoort bij de root `CLAUDE.md`.

## TL;DR

Python-scraper die elke paar minuten **mobile.de** afzoekt naar premium
plug-in hybrides met panoramadak en alerts stuurt via **Telegram**. Per
listing wordt de detail-pagina opgehaald, met **Claude Haiku** gescoord op
27 opties + kleur, en vergeleken met **NL-marktprijzen** om de import-marge
te bepalen.

Draait op **GitHub Actions**, getriggerd door **cron-job.org** (elke 3 min)
via `workflow_dispatch` — GitHub's eigen cron is uitgeschakeld omdat die
niet onder 5 min kan en minder betrouwbaar is.

Alles zit in één bestand: **`scraper.py`** (~1955 regels).

## Welke auto's worden gezocht?

Meerdere merken/modellen, allemaal **HYBRID (PHEV)**, 2021+, met
panoramadak, binnen budget:

| Merk | Modellen |
|------|----------|
| **Audi** | Q3 (Sportback), Q5 (Sportback), A3, A4 Avant, Q8 |
| **Mercedes** | C-Klasse (sedan/Estate), GLC, CLA, E-Klasse |
| **BMW** | 3-serie / 330e (sedan/Touring) |
| **Cupra** | Formentor |

## Repository layout

```
botsauto/
├── auto-alert/
│   ├── scraper.py            # DE scraper. Alles-in-één (~1955 regels).
│   ├── requirements.txt      # requests, beautifulsoup4, anthropic
│   ├── CLAUDE.md             # ← dit bestand
│   ├── test_ai_scoring.py    # AI scoring tests tegen opgeslagen HTML
│   ├── test_url.py           # losse mobile.de URL test
│   ├── .gitignore            # negeert listings.db, __pycache__, .env, debug_*.html
│   └── .github/workflows/alert.yml  # LEGACY (oude cron */5), niet actief
└── .github/workflows/
    ├── alert.yml             # ACTIEVE workflow (workflow_dispatch via cron-job.org)
    ├── research-prices.yml   # NL-marktprijzen research workflow
    ├── test-ai.yml
    └── test-url.yml
```

> Let op: er staan twee `alert.yml` bestanden. De **actieve** is
> `botsauto/.github/workflows/alert.yml`. Die in `auto-alert/.github/` is legacy.

## Wat de scraper precies doet

### Zoekcriteria (`scraper.py:49`)

```python
SEARCH_CRITERIA = {
    "model": "Audi Q3 45 TFSI e",  # legacy label, scraper zoekt veel breder
    "fuel": "hybrid", "year_min": 2021,
    "km_max": 80_000, "price_max": 40_000, "country": "DE",
}
```

### Zoek-URL's (`MOBILE_DE_SEARCH_URLS`, `scraper.py:63`)

13 mobile.de zoek-URL's. Twee soorten pano-filtering:

1. **Freetext** (`ms=...;pano`/`sportback`/etc.) — oudere Q3-URL's.
2. **Feature-filter** (`fe=PANORAMIC_GLASS_ROOF`) — nieuwere "dealer-inkoop"
   URL's; mobile.de's eigen pano-filter, betrouwbaarder dan freetext.

Elke URL-config heeft velden:
- `label` — naam in de logs
- `require_pano_in_desc` — `True` → alleen alerten als AI panoramadak detecteert
- `require_text` — optioneel: alleen doorsturen als deze tekst in titel/desc staat
- `min_listing_date` — optioneel `YYYY-MM-DD`: oudere listings opslaan zonder alert
  (voorkomt alert-spam bij toevoegen van een nieuwe URL voor bestaande voorraad)

URL-overzicht (labels): Q3 pano · Q3 Sportback (pano check) ·
Multi-brand pano (C/GLC/330/Q5) · GLC pano 2023-2025 · Mercedes CLA pano ·
Mercedes E-Klasse pano · Mercedes C pano 2022+ · GLC AMG pano 2023+ ·
Q5 pano 2021+ · A3 pano 2023+ · A4 Avant pano 2022+ · Q8 pano 2021+.

### Pipeline per run (`_run_scrape`, `scraper.py:1742`)

1. **Alle zoekpagina's parallel** (`ThreadPoolExecutor`, 4 workers) via
   **Scrape.do** (`super=true`, `geoCode=de`). Zonder Scrape.do → 502/DataDome.
2. **Parse + dedup** (`scrape_mobile_de`, `scraper.py:1431`): titel, prijs, km,
   jaar, locatie, listing-datum. Gesponsorde ads worden overgeslagen. Listings
   die al in `listings.db` staan of niet aan `require_text`/`min_listing_date`
   voldoen worden gefilterd (en zo nodig opgeslagen zonder alert).
3. **Detail pages parallel** (`_fetch_detail_pages`, `scraper.py:1662`, 8 workers):
   Scrape.do `super=true` + `setCookies=usercentrics-cmp-consent=true` voor
   GDPR. HTML wordt opgeschoond (`clean_detail_html`, `scraper.py:412`):
   cookie banners, nav, footer, GDPR-tekst eruit. Mislukt de detail-fetch →
   `detail_incomplete=True` (score onbetrouwbaar, NIET opslaan zodat retry volgt).
4. **AI scoring parallel** (`score_listing`, 5 workers): Claude Haiku 4.5
   (`claude-haiku-4-5-20251001`) met `AI_SCORING_PROMPT` (`scraper.py:479`).
   Geeft JSON met **27 features + kleur**. 3x retry; daarna `score=-1` en
   **niet** opslaan zodat de volgende run het opnieuw probeert.
5. **Pano-filter**: bij `require_pano_in_desc` URL's zonder `panoramadak` in
   features → opslaan, géén alert.
   **Sportpakket-filter** (`SPORT_PACKAGE_FEATURES`, `scraper.py`): listing
   zonder `s_line`/`s_line_exterieur` (= S line / AMG Line / M Sportpaket / VZ)
   → opslaan, géén alert. Werkt op de al-gescoorde features, dus geen extra
   AI-calls. Bij `detail_incomplete` → niet opslaan zodat de volgende run het
   opnieuw probeert.
6. **Telegram alert** (`send_telegram`, `scraper.py:1058`): merk-detectie uit
   titel, NL-marktprijs vergelijking, koopadvies (`_buy_advice`), verdict,
   feature-checklist (must-haves met ⭐). Pas **ná** succesvolle send wordt de
   listing in de DB gezet.
7. **Seed mode**: als de DB leeg is (eerste run / cache verloren) en er >5
   listings zijn → alles opslaan zónder alerts. Voorkomt 70+ alerts tegelijk.
8. **Failure path**: `_run_scrape` zit in retry-loop (`MAX_RETRIES=3`, backoff
   `[10,30,60]s`). Volledige fail → Telegram `🚨 SCRAPER GEFAALD`.

### AI scoring: features (`scraper.py:207`, `FULL_OPTION_FEATURES`)

24 features in `FULL_OPTION_FEATURES` (panoramadak, keyless, camera's, s_line
(int/ext), matrix_led, velgen, audio_premium, stoelen, ACC, lane/travel assist,
drive_select, onderstel, assists, ambient, achterklep, optik zwart, dyn.
knipperlicht, head_up, luchtvering). Plus `stoelen_memory` en `sportback` voor
display. Kleur wordt apart teruggegeven.

**Merk-bewuste prompt** — de prompt mapt pakketten per merk naar features:
- **Audi** (Q3/Q5/A3): S line, Assistenzpaket Tour/Stadt, Komfortschlüssel,
  B&O/Sonos, Matrix LED, Optikpaket Schwarz, drive select, luchtvering (Q5)
- **Mercedes** (C/GLC): AMG Line, Night-Paket, DISTRONIC, Fahrassistenz-Paket,
  KEYLESS-GO, Burmester, DIGITAL LIGHT, AIRMATIC, pakket-tiers (Advanced/Premium)
- **BMW** (3er/330e): M Sportpaket, Shadow Line, Harman Kardon, Comfort Access,
  Driving Assistant Professional, Adaptive LED
- **Cupra** (Formentor): VZ-pakket, Beats, KESSY, Travel Assist, Drive Profile

**Implicaties afgedwongen in code** (`scraper.py:757`):
- `camera_360 → camera_achteruit`
- `luchtvering → adaptief_onderstel`
- `acc + lane_assist → travel_assist` (en omgekeerd impliceert travel_assist beide)

Display-namen per merk: `FEATURE_DISPLAY_NAMES` + `_MERCEDES`/`_BMW`/`_CUPRA`
overrides (`scraper.py:235`).

### NL-marktprijs vergelijking (`scraper.py:939`)

`NL_MARKET_PRICES` is een lookup-tabel met goedkoopste NL-prijzen per
`(jaar, km-bracket, tier)` per model (bron: Gaspedaal.nl research). `tier` =
`sport` (S-line/AMG/M-sport) of `std`. `_nl_market_price` (`scraper.py:994`)
zoekt exact → andere tier → dichtstbijzijnde km → dichtstbijzijnde jaar.
In de alert: `🇳🇱 NL vanaf: €X → +marge`. Geeft Djari direct de import-marge.

`FEATURES_NOT_AVAILABLE` (`scraper.py:924`) sluit features uit per model voor
de max-score berekening (bijv. Q3/A3 hebben geen luchtvering, Q3 geen head-up).

### Database

SQLite (`listings.db`), tabel `listings`: `id` (PK), `source`, `title`,
`price`, `year`, `km`, `url`, `score`, `features` (JSON), `first_seen`,
`last_seen`. Functies: `init_db`, `listing_exists`, `save_listing`.

### Quiet hours (`main`, `scraper.py:1909`)

Draait alleen 08:00–20:00 CET, met één nachtscan rond 00:30. Override via
`--force` of GitHub `workflow_dispatch`.

### Externe diensten / secrets

| Env var | Doel | Verplicht |
|---------|------|-----------|
| `SCRAPE_DO_TOKEN` | Scrape.do — DataDome bypass voor mobile.de | ja |
| `ANTHROPIC_API_KEY` | Claude Haiku feature-scoring | ja |
| `TELEGRAM_BOT_TOKEN` | Telegram alerts | ja (anders DRY_RUN) |
| `TELEGRAM_CHAT_ID` | Doel chat | ja |

Zonder `TELEGRAM_BOT_TOKEN` → `DRY_RUN` (geen Telegram, alleen logs).

### Belangrijke functies (file:line)

- `scraper.py:1909` `main` — entrypoint, quiet hours + retry loop
- `scraper.py:1742` `_run_scrape` — kern: scrape → score → alert
- `scraper.py:1431` `scrape_mobile_de` — zoekpagina parser
- `scraper.py:1662` `_fetch_detail_pages` — parallel detail fetcher
- `scraper.py:1645` `_fetch_single_detail` — single detail + GDPR cookie
- `scraper.py:1289` `scrape_do_fetch` — Scrape.do client met retry
- `scraper.py:717` `score_listing_ai` / `:807` `score_listing` — Claude AI scoring
- `scraper.py:479` `AI_SCORING_PROMPT` — de merk-bewuste prompt
- `scraper.py:412` `clean_detail_html` — HTML opschonen
- `scraper.py:994` `_nl_market_price` — NL marktprijs lookup
- `scraper.py:857` `_buy_advice` — koopadvies-tekst
- `scraper.py:1058` `send_telegram` — merk-detectie + alert opmaak

## Hosting (GitHub Actions)

`.github/workflows/alert.yml`:
- Trigger: `workflow_dispatch` (cron-job.org pingt elke 3 min). GitHub-cron uit.
- `clear_db` input: wist de DB-cache → volledige nieuwe scan (let op: alert-spam).
- Stappen: checkout → Python 3.12 → `pip install` → cache restore `listings.db`
  → `python scraper.py | tee scraper_output.log` → log als commit-comment →
  cache save → upload `debug_*.html` + log als artifact.
- `concurrency: scraper-run` (geen overlappende runs), `timeout-minutes: 8`.

## Dev-loop & logs

```bash
# Laatste runs / logs (gebruik --json voor structured output)
gh run list --workflow=alert.yml --limit 10
gh run view <run-id> --log
gh run view <run-id> --log-failed
gh run download <run-id> --name debug-html --dir debug/

# Run-logs staan ook als commit-comment op de SHA (Post log summary step)
```

### Lokaal reproduceren

```bash
cd auto-alert
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)   # .env lokaal, NOOIT committen

python scraper.py --dry-run --force   # geen Telegram, geen quiet hours
python test_url.py                    # losse URL test
python test_ai_scoring.py             # AI scoring tegen opgeslagen HTML
```

## Wat er NIET moet gebeuren

- **Geen secrets committen** — `.env`, `*.db`, `debug_*.html` blijven gitignored
- **Geen refactor van scoring-prompt of zoek-URL's** tenzij expliciet gevraagd
  — die zijn empirisch afgesteld
- **Geen `git push --force`**, **geen push naar `master`**
- **Geen Playwright/headless Chrome lokaal** — alles via Scrape.do
- **Geen feature creep** bij bug-fixes — alleen het gerapporteerde fixen

## Verbeterpunten (backlog)

1. **Lockfile** voor het geval twee runs overlappen (deels gedekt door `concurrency`)
2. **Heartbeat** naar Telegram/healthchecks.io
3. **Kosten-logging** voor Scrape.do en Anthropic credits per run
4. **Telegram bot commands** (`/status`, `/lastrun`) via long-polling

## Git / branch policy

- Hoofdbranch: `master`
- Branch moet beginnen met `claude/` en eindigen op het session-id
- Push: `git push -u origin <branch>`
