# CLAUDE.md — Auto-Alert Scraper

> Lees dit eerst voordat je iets aan dit project verandert. Dit bestand is de
> volledige briefing voor een nieuwe Claude-sessie.

## TL;DR

Dit is een Python-scraper die elke paar minuten **mobile.de** afzoekt naar
**Audi Q3 (Sportback) 45 TFSI e** plug-in hybrides. Gevonden auto's worden
gescored op opties via **Claude Haiku** en bij een match wordt een
**Telegram-alert** gestuurd.

De scraper draait op **GitHub Actions** (cron `*/5 7-18 * * *`) en blijft
daar draaien — Actions is betrouwbaar genoeg voor deze workload. **Geen
Hetzner-verhuizing**.

Wat we wél willen verbeteren: de **dev-loop in VS Code** zodat Claude
snel logs kan lezen, bugs kan reproduceren en fixes kan pushen. Dat
gebeurt via `gh` CLI (logs + artifacts) en lokale dry-runs.

## Repository layout

```
botsauto/
├── auto-alert/
│   ├── scraper.py            # ENIGE echte scraper. Alles staat hierin (~1385 regels).
│   ├── requirements.txt      # requests, beautifulsoup4, anthropic
│   ├── README.md             # Korte beschrijving
│   ├── CLAUDE.md             # ← dit bestand
│   ├── test_ai_scoring.py    # Unit tests voor AI scoring met opgeslagen HTML
│   ├── test_url.py           # Test losse mobile.de URL handmatig
│   ├── setup-repo.sh         # OUDE setup voor GitHub repo (legacy)
│   ├── .gitignore            # negeert listings.db, __pycache__, .env
│   └── .github/workflows/alert.yml  # (legacy locatie)
└── .github/workflows/
    ├── alert.yml             # ACTIEVE GitHub Actions workflow (cron */5)
    ├── test-ai.yml
    └── test-url.yml
```

> Let op: er staan twee `alert.yml` bestanden. De **actieve** is
> `botsauto/.github/workflows/alert.yml`. De andere is legacy.

## Wat de scraper precies doet

### Zoekcriteria (in `scraper.py`)

```python
SEARCH_CRITERIA = {
    "model": "Audi Q3 45 TFSI e",
    "fuel": "hybrid",
    "year_min": 2021,
    "km_max": 80_000,
    "price_max": 40_000,
    "country": "DE",
}
```

### Drie zoek-URL's op mobile.de

`MOBILE_DE_SEARCH_URLS` (in `scraper.py:63`) bevat drie varianten:

| # | Label | Doel | Pano-check? |
|---|-------|------|-------------|
| 1 | Q3 pano | freetext "pano" — alles doorsturen | nee |
| 2 | Q3 Sportback (pano check) | freetext "sportback" | ja, via AI |
| 3 | Q3 Sportback catch-all | alle Q3 hybrids, filter op "sportback" | ja, via AI |

URL 2 en 3 hebben een extra check: alleen doorsturen als het AI-model
"panoramadak" detecteert in de detail-pagina.

### Pipeline per run

1. **Loop over de 3 zoek-URL's**
   - Haal zoekpagina op via **Scrape.do** (`super=true`, `geoCode=de`).
     Zonder Scrape.do krijg je 502 / DataDome block.
   - Parse listings met BeautifulSoup (titel, prijs, km, jaar, listing date,
     locatie). Gesponsorde advertenties worden overgeslagen.
   - Filter listings die we al kennen (`listings.db`) of niet aan
     `require_text` voldoen.
2. **Detail pages parallel ophalen** (`ThreadPoolExecutor`, 8 workers)
   - Via Scrape.do met `render=true` (headless Chromium).
   - GDPR-bypass via `setCookies=usercentrics-cmp-consent=true`.
   - Fallback: `playWithBrowser` clickt de Usercentrics shadow-DOM accept-knop.
   - HTML wordt opgeschoond (`clean_detail_html`) — cookie banners, nav,
     footer en bekende GDPR-tekstblokken worden gestript.
3. **AI scoring** (`ThreadPoolExecutor`, 5 workers)
   - Per listing → Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) met
     `AI_SCORING_PROMPT` (`scraper.py:338`). Geeft JSON terug met 24 features
     + kleur.
   - Implicaties worden afgedwongen in code: `camera_360 → camera_achteruit`,
     `acc + lane_assist → travel_assist`, `S line Paket → s_line + s_line_exterieur`.
   - Bij failure → 3x retry; daarna `score = -1` en **niet** opslaan zodat
     volgende run het opnieuw probeert.
4. **Pano-filter** (alleen URL 2 + 3): listing zonder `panoramadak` →
   wel opslaan, géén alert.
5. **Telegram alert** voor nieuwe listings:
   - HTML format met titel, prijs, km, jaar, koopadvies (`_buy_advice`),
     verdict, gevonden + missende features (must-haves krijgen ⭐).
   - Pas **ná** succesvolle Telegram send wordt de listing in de DB gezet.
6. **Failure path**: hele `_run_scrape()` zit in een retry-loop
   (`MAX_RETRIES=3`, backoff `[10, 30, 60]s`). Bij volledige fail → Telegram
   `🚨 SCRAPER GEFAALD` alert.

### Database

SQLite (`listings.db`), één tabel `listings` met `id` (PK),
`source`, `title`, `price`, `year`, `km`, `url`, `score`, `features` (JSON),
`first_seen`, `last_seen`. Functies: `init_db`, `listing_exists`, `save_listing`.

### Quiet hours

Standaard draait de scraper alleen 08:00–20:00 CET, met één extra "nachtscan"
om 00:30. Override via `--force` of GitHub `workflow_dispatch`.

### Externe diensten / secrets

| Env var | Doel | Verplicht |
|---------|------|-----------|
| `SCRAPE_DO_TOKEN` | Scrape.do API — DataDome bypass voor mobile.de | ja |
| `ANTHROPIC_API_KEY` | Claude Haiku voor feature-scoring | ja |
| `TELEGRAM_BOT_TOKEN` | Telegram bot voor alerts | ja (anders DRY_RUN) |
| `TELEGRAM_CHAT_ID` | Doel chat van de alerts | ja |

Zonder `TELEGRAM_BOT_TOKEN` zet de scraper zichzelf in `DRY_RUN` mode
(geen Telegram, alleen logs).

### Belangrijke functies (file:line)

- `scraper.py:1340` `main` — entrypoint, quiet hours + retry loop
- `scraper.py:1221` `_run_scrape` — kern: scrape → score → alert
- `scraper.py:937` `scrape_mobile_de` — zoekpagina parser
- `scraper.py:1154` `_fetch_detail_pages` — parallel detail fetcher
- `scraper.py:1122` `_fetch_single_detail` — single detail + GDPR fallback
- `scraper.py:804` `scrape_do_fetch` — Scrape.do client met retry
- `scraper.py:458` `score_listing_ai` — Claude AI scoring
- `scraper.py:271` `clean_detail_html` — HTML opschonen
- `scraper.py:600` `_buy_advice` — koopadvies-tekst
- `scraper.py:667` `send_telegram` — alert opmaak + verzenden

## Hosting (GitHub Actions)

`.github/workflows/alert.yml` is de actieve workflow:

```yaml
on:
  schedule:
    - cron: '*/5 7-18 * * *'   # elke 5 min, 07-18 UTC = 08-19 CET
```

Stappen: checkout → Python 3.12 → `pip install` → `actions/cache/restore`
voor `listings.db` → `python scraper.py` → `actions/cache/save` → upload
`debug_*.html` als artifact.

Werkt prima en is betrouwbaar genoeg. **Niet verhuizen** tenzij er een
concrete reden komt.

## Dev-loop in VS Code

Het belangrijkste doel: Claude Code in VS Code moet snel logs kunnen lezen,
bugs reproduceren en fixes pushen.

### Logs lezen via `gh` CLI

Vanuit Claude (Bash tool) of in een VS Code terminal:

```bash
# Laatste 10 runs van de scraper workflow
gh run list --workflow=alert.yml --limit 10

# Volledige log van een specifieke run
gh run view <run-id> --log

# Alleen failed steps
gh run view <run-id> --log-failed

# Logs van de meest recente run
gh run view --log $(gh run list --workflow=alert.yml --limit 1 --json databaseId -q '.[0].databaseId')

# Debug HTML artifact downloaden naar ./debug/
gh run download <run-id> --name debug-html --dir debug/
```

> Tip voor Claude: gebruik `--json` flags zodat je structured output krijgt
> in plaats van te moeten parsen. Bv. `gh run list --json status,conclusion,createdAt,databaseId`.

### Lokaal reproduceren

```bash
cd auto-alert
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# secrets uit .env (lokaal, NIET committen)
export $(grep -v '^#' .env | xargs)

python scraper.py --dry-run --force   # geen Telegram, geen quiet hours
python test_url.py                    # losse URL test
python test_ai_scoring.py             # AI scoring tegen opgeslagen HTML
```

`.env` lokaal moet bevatten:
```
SCRAPE_DO_TOKEN=...
ANTHROPIC_API_KEY=...
TELEGRAM_BOT_TOKEN=...   # optioneel — zonder = DRY_RUN
TELEGRAM_CHAT_ID=...
```

### Bug-fix workflow

1. User stuurt error / log snippet of een `gh run view` output
2. Claude leest `auto-alert/CLAUDE.md` (dit bestand) voor context
3. Claude zoekt het probleem in `scraper.py` (zie file:line refs hieronder)
4. Reproductie lokaal als mogelijk (`--dry-run --force`)
5. Fix → commit → push naar `claude/deploy-auto-alert-scraper-ZsKua`
6. Volgende scheduled run op Actions valideert de fix

## Wat er NIET moet gebeuren

- **Geen secrets committen** — `.env`, `*.db`, `debug_*.html` blijven gitignored
- **Geen refactor van de scoring logic** of de scraping URL's tenzij dat
  expliciet gevraagd wordt — die zijn empirisch afgesteld
- **Geen `git push --force`**, en **geen** push naar `master` — alles op
  branch `claude/deploy-auto-alert-scraper-ZsKua`
- **Geen Playwright of headless Chrome lokaal** — alles loopt via Scrape.do
- **Geen feature creep** bij bug-fixes — alleen het gerapporteerde fixen,
  niets eromheen "verbeteren"

## Verbeterpunten (lijst, los van bug-fixes)

Geen vaste prioriteit — pak op wanneer relevant:

1. **Lockfile** in scraper voor het geval twee runs overlappen
2. **Heartbeat** naar Telegram of healthchecks.io zodat crashes opvallen
3. **Kosten-logging** voor Scrape.do en Anthropic credits per run
4. **Telegram bot commands** (`/status`, `/lastrun`) via long-polling

Als de Actions setup ooit wel pijn gaat doen (cron drift, cache loss, te
trage cold starts) staat het Hetzner-plan in de git history van deze branch
— commit `535afe5` had de volledige systemd-deploy uitgewerkt.

## Workflows / branch policy

- Hoofdbranch: `master` (legacy GitHub Actions)
- Deze taak werkt op: **`claude/deploy-auto-alert-scraper-ZsKua`**
- Push: `git push -u origin claude/deploy-auto-alert-scraper-ZsKua`
- Branch naam moet beginnen met `claude/` en eindigen op de session-id,
  anders weigert de remote met 403

## Hoe je deze sessie opstart in VS Code

1. Open de `botsauto` repo in VS Code
2. Checkout de branch:
   `git checkout claude/deploy-auto-alert-scraper-ZsKua`
   (of `git fetch origin claude/deploy-auto-alert-scraper-ZsKua && git checkout ...`)
3. Start Claude Code in de workspace
4. Verwijs Claude naar dit bestand: "Lees `auto-alert/CLAUDE.md` eerst"
5. Geef de specifieke taak (bijv. "implementeer de Hetzner deploy uit het plan")

## Tests handmatig draaien

```bash
cd auto-alert
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export SCRAPE_DO_TOKEN=... ANTHROPIC_API_KEY=...
python test_url.py            # test losse mobile.de URL
python test_ai_scoring.py     # test AI scoring tegen opgeslagen HTML
python scraper.py --dry-run --force  # full run zonder Telegram, zonder quiet hours
```
