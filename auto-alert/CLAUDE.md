# CLAUDE.md — Auto-Alert Scraper

> Lees dit eerst voordat je iets aan dit project verandert. Dit bestand is de
> volledige briefing voor een nieuwe Claude-sessie.

## TL;DR

Dit is een Python-scraper die elke paar minuten **mobile.de** afzoekt naar
**Audi Q3 (Sportback) 45 TFSI e** plug-in hybrides. Gevonden auto's worden
gescored op opties via **Claude Haiku** en bij een match wordt een
**Telegram-alert** gestuurd.

Op dit moment draait de scraper op **GitHub Actions** (cron `*/5 7-18 * * *`).
**Doel: verhuizen naar een eigen Hetzner-server**, met betere reliability
en zonder de beperkingen van GitHub Actions cron. De server draait ook andere
projecten — deze deploy moet daarom **volledig geïsoleerd** zijn.

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

## Huidige hosting (legacy)

`.github/workflows/alert.yml`:

```yaml
on:
  schedule:
    - cron: '*/5 7-18 * * *'   # elke 5 min, 07-18 UTC = 08-19 CET
```

Stappen: checkout → Python 3.12 → `pip install` → `actions/cache/restore`
voor `listings.db` → `python scraper.py` → `actions/cache/save` → upload
`debug_*.html` als artifact.

**Bekende problemen** met deze setup:
- GitHub cron drift (5 min wordt soms 15+ min)
- `actions/cache` is geen echte database — race conditions, kan stilletjes
  leeg zijn na een failed run
- ~30s cold start per run (checkout + pip install)
- Debug HTML alleen via download
- 8 minuten timeout
- Geen state tussen runs behalve via cache hack

## Doel: Hetzner deploy (geïsoleerd)

De Hetzner server draait al andere services. Deze scraper moet **niets**
anders raken. Eisen:

1. **Eigen unix user** (`autoalert`) — geen sudo, eigen home directory
2. **Eigen Python venv** in `/home/autoalert/venv` — geen system-wide pip
3. **Eigen werkdirectory** `/home/autoalert/auto-alert/` met checkout
4. **Eigen systemd unit** + timer (geen cron op systeemniveau)
5. **Eigen log directory** met `journalctl --user` of file rotation
6. **Eigen `.env`** met secrets — `chmod 600`, niet in git
7. **Geen poorten openen** — scraper is uitgaand only
8. **Geen interferentie** met andere services (geen globale Playwright,
   geen globale Chrome, geen system packages installeren behalve via
   `apt` met expliciete confirmation)

### Voorgestelde directory layout op de server

```
/home/autoalert/
├── .env                       # secrets, chmod 600
├── auto-alert/                # git checkout (deze repo, sparse op auto-alert/)
│   ├── scraper.py
│   ├── requirements.txt
│   ├── ...
│   └── data/
│       └── listings.db        # persistente DB (los van git)
├── venv/                      # python venv
└── logs/                      # rotated logs (optioneel naast journald)
```

### Voorgestelde systemd setup

Twee user-units onder `/home/autoalert/.config/systemd/user/`:

- `auto-alert.service` — oneshot, draait `python scraper.py`
- `auto-alert.timer` — `OnCalendar=*:0/5` (elke 5 min), met `Persistent=true`
  zodat een gemiste run wordt ingehaald

User-units i.p.v. system-units omdat:
- Geen sudo nodig na initiële `loginctl enable-linger autoalert`
- Volledig in `/home/autoalert/`, raakt geen system files
- Logs via `journalctl --user -u auto-alert.service`

Alternatief: één **persistente daemon** met een interne `time.sleep(300)`
loop. Voordeel: nog snellere starts. Nadeel: één crash = alles dood (mitigatie:
systemd `Restart=always`). Voor nu kies ik **timer-based** omdat het
identiek gedrag geeft aan de huidige Actions setup en makkelijker te debuggen is.

### Code-aanpassingen die nodig zijn

1. **Configurabele paden** — `DB_PATH`, debug HTML pad, etc. via env vars
   met fallback naar huidige defaults. Geen breaking change voor Actions.
2. **`.env` loader** — `python-dotenv` of een mini-loader, alleen actief
   als `.env` bestaat. Op Actions blijft het env-vars-only werken.
3. **Quiet hours flag** — moet uit kunnen via env (`QUIET_HOURS=0`)
   zonder `--force` argument, voor systemd timers.
4. **Logging** — naast stdout ook naar bestand (`logs/scraper.log`) met
   rotatie, maar alleen als `LOG_DIR` env var gezet is.
5. **Lockfile** — voorkomen dat twee runs tegelijk draaien als een vorige
   nog bezig is (timer kan overlappen). `flock` of een SQLite lock-tabel.
6. **Health heartbeat** (optioneel) — na succesvolle run een ping naar
   healthchecks.io of een Telegram heartbeat 1x per dag.

### Deploy script

Een idempotent shell script `auto-alert/deploy/install.sh` dat lokaal op
de server draait:

1. Maak user `autoalert` als die niet bestaat
2. `loginctl enable-linger autoalert`
3. Clone of pull deze repo in `/home/autoalert/auto-alert-repo/`
4. Symlink/sparse-checkout naar `/home/autoalert/auto-alert/`
5. Maak venv, `pip install -r requirements.txt`
6. Render systemd units uit templates met juiste paden
7. `systemctl --user daemon-reload && systemctl --user enable --now auto-alert.timer`
8. Print status + volgende geplande run

Plus een `auto-alert/deploy/update.sh` voor de happy path:
`git pull && pip install -r requirements.txt && systemctl --user restart auto-alert.timer`.

## Wat er NIET moet gebeuren

- **Geen wijzigingen aan andere services** op de Hetzner box
- **Geen system-wide installs** — alles in user-space
- **Geen secrets committen** — `.env`, `*.db`, `debug_*.html` blijven gitignored
- **Geen breaking changes voor de huidige Actions workflow** — die mag blijven
  draaien als fallback tot we zeker weten dat de Hetzner-versie stabiel is
- **Geen refactor van de scoring logic** of de scraping URL's tenzij dat
  expliciet gevraagd wordt — die zijn empirisch afgesteld
- **Geen Playwright of headless Chrome op de server** — Scrape.do doet dat
  voor ons, dat is bewust zo gekozen
- **Geen `git push --force`**, en **geen** push naar `master` — alles op
  branch `claude/deploy-auto-alert-scraper-ZsKua`

## Verbeterpunten (lijst, in volgorde van prioriteit)

1. **Hetzner deploy** — geïsoleerd via systemd user units, eigen venv, eigen `.env`
2. **Lockfile** zodat overlappende runs onmogelijk zijn
3. **Persistente, betrouwbare DB** (gewoon SQLite op disk) i.p.v. cache hack
4. **Heartbeat** naar Telegram of healthchecks.io zodat we crashes merken
5. **Structured logging** + rotatie
6. **Telegram bot commands** (`/status`, `/pause`, `/lastrun`) via long-polling
   — los proces dat via dezelfde DB praat
7. **Kosten-monitoring** voor Scrape.do en Anthropic — log credits per run
8. **Eventueel** directe scraping als experiment, met fallback op Scrape.do

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
