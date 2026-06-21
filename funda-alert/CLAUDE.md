# CLAUDE.md — Huurhuizen Alert Scraper

> Lees dit eerst voordat je iets aan dit project verandert. Dit bestand is de
> volledige briefing voor een nieuwe Claude-sessie.

## TL;DR

Dit is een Python-scraper die elke paar minuten **meerdere Nederlandse
huizensites** afzoekt naar **huurwoningen** die aan bepaalde criteria voldoen.
Gevonden woningen worden gescored op kenmerken via **Claude Haiku** en bij een
match wordt een **Telegram-alert** gestuurd.

**Waarom multi-source?** Woningen verschijnen vaak EERST op lokale
makelaarswebsites en pas later op Funda. Door lokale makelaars én grote
platforms tegelijk te scrapen, vind je woningen sneller dan iemand die alleen
op Funda kijkt.

De scraper draait op **GitHub Actions** (getriggerd via cron-job.org) en
volgt dezelfde architectuur als de auto-alert scraper in `../auto-alert/`.

## Bronnen / websites om te scrapen

### Tier 1 — Grote platforms (breed bereik)

| Bron | URL-patroon | Opmerkingen |
|------|-------------|-------------|
| **Funda.nl** | `funda.nl/huur/` | Grootste platform NL, maar listings verschijnen hier vaak als LAATSTE |
| **Pararius.nl** | `pararius.nl/huurwoningen/` | Tweede grootste, gratis voor huurders, goede API-achtige structuur |
| **Kamernet.nl** | `kamernet.nl/` | Vooral kamers maar ook appartementen, betaalmuur voor contact |
| **HousingAnywhere** | `housinganywhere.com/` | Internationaal platform, ook NL huurwoningen |
| **Huurwoningen.nl** | `huurwoningen.nl/` | Aggregator die meerdere bronnen combineert |

### Tier 2 — Lokale makelaars (snelheidsvoordeel)

Lokale makelaars publiceren woningen op hun eigen site VOORDAT ze op Funda
komen. Dit is waar het snelheidsvoordeel zit.

**Aanpak:** Maak een configureerbare lijst van lokale makelaarswebsites per
regio. Elke makelaar heeft een eigen scrape-config:

```python
LOCAL_AGENTS = [
    {
        "name": "Makelaar X",
        "base_url": "https://www.makelaarx.nl/huurwoningen",
        "listing_selector": "div.property-listing",  # CSS selector
        "title_selector": "h2.title",
        "price_selector": "span.price",
        "link_selector": "a.property-link",
        "region": "Amsterdam",
    },
    # ... meer makelaars
]
```

> **Strategie:** Begin met de grote platforms (Tier 1). Voeg lokale makelaars
> toe per regio zodra de basis werkt. Elke makelaar is een config-entry, geen
> aparte code.

### Tier 3 — Social / overig

| Bron | Opmerkingen |
|------|-------------|
| Facebook Marketplace | Moeilijk te scrapen (login vereist), lage prioriteit |
| Marktplaats.nl | Huurwoningen sectie, relatief simpel |

## Architectuur

Dezelfde pipeline als `auto-alert/scraper.py`:

```
┌─────────────────────────────────────────────────────┐
│  1. SEARCH — loop over alle bronnen                 │
│     ├── Funda, Pararius, etc. via Scrape.do         │
│     ├── Lokale makelaars via Scrape.do              │
│     └── Parse listings (BS4): titel, prijs, m²,     │
│         kamers, adres, URL                          │
├─────────────────────────────────────────────────────┤
│  2. DEDUP — filter bekende listings (SQLite)        │
│     └── Unieke key: URL of combinatie adres+prijs   │
├─────────────────────────────────────────────────────┤
│  3. DETAIL — haal detail-pagina's op (parallel)     │
│     ├── ThreadPoolExecutor, 8 workers               │
│     ├── Via Scrape.do (render=true voor JS-sites)    │
│     └── GDPR/cookie bypass via Scrape.do cookies    │
├─────────────────────────────────────────────────────┤
│  4. AI SCORE — Claude Haiku feature-scoring         │
│     ├── ThreadPoolExecutor, 5 workers               │
│     ├── Geeft JSON terug met woningkenmerken        │
│     └── Score + verdict (match / misschien / skip)  │
├─────────────────────────────────────────────────────┤
│  5. FILTER — pas criteria toe                       │
│     └── Prijs, m², kamers, regio, must-haves        │
├─────────────────────────────────────────────────────┤
│  6. ALERT — Telegram bij nieuwe matches             │
│     ├── HTML format met kenmerken en score           │
│     └── Pas NA succesvolle send → opslaan in DB     │
└─────────────────────────────────────────────────────┘
```

## Zoekcriteria (configureerbaar)

```python
SEARCH_CRITERIA = {
    "type": "huur",              # huur, niet koop
    "price_min": 800,            # minimale huurprijs per maand
    "price_max": 1800,           # maximale huurprijs per maand
    "area_min_m2": 50,           # minimale oppervlakte
    "rooms_min": 2,              # minimaal aantal kamers
    "regions": [                 # regio's om te zoeken
        "Amsterdam",
        "Utrecht",
        "Haarlem",
        # ... configureerbaar
    ],
    "radius_km": 15,             # straal rondom regio-centrum
    "furnished": None,           # None = alles, "furnished", "unfurnished"
    "available_from": None,      # None = alles, of "2026-07-01"
}
```

> **Let op:** Deze criteria zijn een startpunt. Pas ze aan naar de
> wensen van de gebruiker voordat je begint met bouwen.

## AI Scoring — woningkenmerken

Claude Haiku scoort elke woning op basis van de detailpagina HTML.
Prompt retourneert JSON met kenmerken:

```python
HOUSING_FEATURES = {
    # Basis
    "oppervlakte_m2": int,       # woonoppervlak
    "kamers": int,               # aantal kamers
    "slaapkamers": int,          # aantal slaapkamers
    "badkamers": int,            # aantal badkamers
    "verdieping": str,           # "begane grond", "1e", "2e", etc.
    "bouwjaar": int,             # bouwjaar

    # Woning kenmerken
    "balkon": bool,              # balkon of terras
    "tuin": bool,                # tuin (privé)
    "parkeren": str,             # "geen", "straat", "garage", "parkeerplaats"
    "berging": bool,             # berging / opslag
    "lift": bool,                # lift in gebouw (relevant voor appartementen)

    # Interieur
    "gemeubileerd": str,         # "kaal", "gestoffeerd", "gemeubileerd"
    "keuken_type": str,          # "open", "gesloten", "nvt"
    "vloerverwarming": bool,
    "airco": bool,

    # Energielabel
    "energielabel": str,         # "A", "B", "C", etc.

    # Omgeving
    "afstand_ov": str,           # "< 5 min", "5-10 min", "10+ min"
    "buurt_sfeer": str,          # korte beschrijving

    # Status
    "beschikbaar_per": str,      # datum of "per direct"
    "huurperiode_min": str,      # "onbepaald", "1 jaar", "2 jaar"
    "reageren_mogelijk": bool,   # of er nog gereageerd kan worden

    # Dealbreakers
    "huisdieren_toegestaan": bool,
    "inschrijving_mogelijk": bool,  # GBA inschrijving
    "inkomen_eis": str,          # "3x huur", "4x huur", specifiek bedrag
}
```

### Must-haves (configureerbaar)

```python
MUST_HAVES = [
    "balkon",                    # balkon of terras verplicht
    "inschrijving_mogelijk",     # GBA inschrijving moet kunnen
]

NICE_TO_HAVES = [
    "energielabel",              # A of B = bonus
    "parkeren",                  # garage of parkeerplaats = bonus
    "lift",                      # bonus bij hogere verdieping
]
```

## Telegram alert format

```
🏠 Nieuwe huurwoning gevonden!

📍 Keizersgracht 123, Amsterdam
💰 €1.450/mnd | 75m² | 3 kamers
🏗 Bouwjaar 2015 | Energielabel A
📅 Beschikbaar per: 1 augustus 2026

✅ Gevonden features:
⭐ Balkon (must-have)
⭐ Inschrijving mogelijk (must-have)
• Gestoffeerd
• Lift
• Parkeerplaats

❌ Ontbrekend:
• Geen airco
• Geen vloerverwarming

📊 Score: 8/10
💡 Verdict: Aanrader — snel reageren!
🔗 Bron: Pararius
🔗 [Bekijk op Pararius](https://...)
```

## Database

SQLite (`listings.db`), één tabel `listings`:

| Kolom | Type | Doel |
|-------|------|------|
| `id` | TEXT PK | Unieke key (URL hash of adres+prijs) |
| `source` | TEXT | "funda", "pararius", "makelaar_x", etc. |
| `title` | TEXT | Adres of titel |
| `price` | INTEGER | Huurprijs per maand |
| `area_m2` | INTEGER | Oppervlakte |
| `rooms` | INTEGER | Aantal kamers |
| `url` | TEXT | Link naar listing |
| `score` | INTEGER | AI score (0-10) |
| `features` | TEXT | JSON met alle gevonden features |
| `first_seen` | TEXT | ISO timestamp eerste keer gezien |
| `last_seen` | TEXT | ISO timestamp laatste keer gezien |

Functies: `init_db`, `listing_exists`, `save_listing` — zelfde patroon als
`auto-alert/scraper.py`.

## Repository layout

```
botsauto/
├── funda-alert/
│   ├── scraper.py            # ALLES-IN-ÉÉN scraper (net als auto-alert)
│   ├── requirements.txt      # requests, beautifulsoup4, anthropic
│   ├── CLAUDE.md             # ← dit bestand
│   ├── sources.py            # (optioneel) source configs als het te groot wordt
│   ├── test_scoring.py       # AI scoring tests
│   ├── .gitignore            # listings.db, __pycache__, .env, debug_*.html
│   └── .env                  # LOKAAL, NOOIT COMMITTEN
└── .github/workflows/
    └── funda-alert.yml       # GitHub Actions workflow
```

## Hosting (GitHub Actions)

Zelfde setup als auto-alert:

```yaml
name: Huurhuizen Alert Scraper

on:
  workflow_dispatch:
    inputs:
      clear_db:
        description: 'Database wissen (volledige nieuwe scan)'
        required: false
        type: boolean
        default: false

permissions:
  actions: write
  contents: write

defaults:
  run:
    working-directory: funda-alert

jobs:
  scrape:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    concurrency:
      group: funda-scraper-run
      cancel-in-progress: false

    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r requirements.txt

      - name: Restore database from cache
        if: ${{ github.event_name == 'workflow_dispatch' && inputs.clear_db == false }}
        uses: actions/cache/restore@v4
        with:
          path: funda-alert/listings.db
          key: funda-listings-db-dummy
          restore-keys: funda-listings-db-

      - name: Run scraper
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          SCRAPE_DO_TOKEN: ${{ secrets.SCRAPE_DO_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: python scraper.py 2>&1 | tee scraper_output.log

      - name: Save database to cache
        if: always()
        uses: actions/cache/save@v4
        with:
          path: funda-alert/listings.db
          key: funda-listings-db-${{ github.run_id }}-${{ github.run_attempt }}

      - name: Upload debug HTML
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: debug-html
          path: |
            funda-alert/debug_*.html
            funda-alert/scraper_output.log
          retention-days: 3
          if-no-files-found: ignore
```

Trigger via **cron-job.org** (elke 3-5 min, workflow_dispatch).

## Externe diensten / secrets

| Env var | Doel | Verplicht |
|---------|------|-----------|
| `SCRAPE_DO_TOKEN` | Scrape.do API — scraping en anti-bot bypass | ja |
| `ANTHROPIC_API_KEY` | Claude Haiku voor feature-scoring | ja |
| `TELEGRAM_BOT_TOKEN` | Telegram bot voor alerts | ja (anders DRY_RUN) |
| `TELEGRAM_CHAT_ID` | Doel chat van de alerts | ja |

Zonder `TELEGRAM_BOT_TOKEN` → automatisch `DRY_RUN` mode.

## Scraping per bron — technische details

### Funda.nl

- **URL-patroon:** `https://www.funda.nl/huur/{stad}/beschikbaar/0-{max_prijs}/{min_m2}+woonopp/`
- **Anti-bot:** Funda gebruikt Akamai Bot Manager → Scrape.do met `super=true`
- **Parse:** BeautifulSoup, listings staan in `div.search-result`
- **Detail:** Aparte fetch per listing voor volledige beschrijving
- **Let op:** Funda heeft rate limiting, niet te agressief fetchen

### Pararius.nl

- **URL-patroon:** `https://www.pararius.nl/huurwoningen/{stad}/0-{max_prijs}/{min_m2}m2`
- **Anti-bot:** Minimaal, maar Scrape.do voor consistentie
- **Parse:** Goed gestructureerde HTML, `li.search-list__item`
- **Voordeel:** Minder restrictief dan Funda, snellere pagina's

### Lokale makelaars

- **Per makelaar configureerbaar** via `LOCAL_AGENTS` dict
- **Scraping:** CSS selectors per makelaar (titel, prijs, link, foto)
- **Anti-bot:** Meestal geen, maar Scrape.do voor uniformiteit
- **Prioriteit:** HOOG — hier verschijnen woningen het eerst
- **Toevoegen:** Zoek de top-makelaars in de gewenste regio en voeg hun
  aanbodpagina + CSS selectors toe

### Alle bronnen

- Alles via **Scrape.do** (`super=true` voor platforms met anti-bot)
- Detail pages met `render=true` als de site JavaScript nodig heeft
- GDPR/cookie bypass via `setCookies` of `playWithBrowser`
- Parallel fetching met `ThreadPoolExecutor`

## Dev-loop

### Lokaal draaien

```bash
cd funda-alert
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# secrets uit .env
export $(grep -v '^#' .env | xargs)

python scraper.py --dry-run --force   # geen Telegram, geen quiet hours
```

### Logs lezen (GitHub Actions)

```bash
gh run list --workflow=funda-alert.yml --limit 10
gh run view <run-id> --log
gh run view <run-id> --log-failed
```

### Bug-fix workflow

1. User stuurt error of log snippet
2. Claude leest `funda-alert/CLAUDE.md` (dit bestand)
3. Zoek probleem in `scraper.py`
4. Reproductie lokaal (`--dry-run --force`)
5. Fix → commit → push
6. Volgende scheduled run valideert de fix

## Stapsgewijs bouwplan

### Fase 1 — MVP (één bron)

1. Kies **Pararius** als eerste bron (minder anti-bot dan Funda)
2. Bouw `scraper.py` met:
   - `scrape_pararius()` — zoekpagina parsen
   - `fetch_detail_page()` — detail ophalen via Scrape.do
   - `score_listing_ai()` — Claude Haiku scoring
   - `send_telegram()` — alert versturen
   - `init_db`, `listing_exists`, `save_listing` — SQLite
3. Test lokaal met `--dry-run`
4. Deploy naar GitHub Actions
5. Valideer dat alerts binnenkomen

### Fase 2 — Funda toevoegen

1. Voeg `scrape_funda()` toe
2. Dedup moet cross-source werken (zelfde woning op Funda én Pararius)
3. Anti-bot tuning voor Funda (Scrape.do `super=true`, eventueel `render=true`)

### Fase 3 — Lokale makelaars

1. Configureerbaar systeem voor lokale makelaars (`LOCAL_AGENTS`)
2. Generieke `scrape_local_agent(config)` functie
3. Voeg 5-10 makelaars toe in de gewenste regio
4. Dit is waar het **snelheidsvoordeel** zit

### Fase 4 — Optimalisatie

1. Cross-source dedup (adres-normalisatie)
2. Prijs-tracking (prijs veranderd → nieuwe alert)
3. "Al verhuurd" detectie (listing verdwenen → markeren)
4. Heartbeat / monitoring

## Wat er NIET moet gebeuren

- **Geen secrets committen** — `.env`, `*.db`, `debug_*.html` blijven gitignored
- **Geen Playwright of headless Chrome lokaal** — alles via Scrape.do
- **Geen feature creep** bij bug-fixes
- **Geen `git push --force`**, en **geen** push naar `master`
- **Geen login-gebaseerde scrapers** (Facebook, etc.) — alleen publiek
  beschikbare listings
- **Geen betaalde API's** van Funda of Pararius — alleen scraping van
  publieke pagina's

## Referentie: auto-alert scraper

De auto-alert scraper (`../auto-alert/scraper.py`) is het bewezen template.
Kopieer het patroon voor:

- Scrape.do integratie (`scrape_do_fetch`)
- Retry logica met backoff
- ThreadPoolExecutor parallel fetching
- Claude Haiku AI scoring
- Telegram alert formatting
- SQLite database functies
- GitHub Actions workflow + cache
- Error handling + failure alerts
- `--dry-run` en `--force` flags
- Quiet hours logica

Lees `../auto-alert/CLAUDE.md` voor de complete technische details van
dat systeem.
