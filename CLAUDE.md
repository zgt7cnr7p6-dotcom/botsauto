# CLAUDE.md — Botsauto

> Claude Code leest dit bestand automatisch. Lees ook `auto-alert/CLAUDE.md`
> voor de volledige technische briefing van de scraper.

## Wat is dit project?

Een geautomatiseerde scraper die mobile.de monitort op premium **plug-in
hybrides met panoramadak** (2021+, binnen budget) en Telegram alerts stuurt
bij goede matches. Per listing wordt de import-marge t.o.v. de NL-markt
getoond. Eigenaar: Djari.

Gezochte modellen:
- **Audi**: Q3 (Sportback), Q5 (Sportback), A3, A4 Avant, Q8
- **Mercedes**: C-Klasse (sedan/Estate), GLC, CLA, E-Klasse
- **BMW**: 3-serie / 330e (sedan/Touring)
- **Cupra**: Formentor

## Repo structuur

```
botsauto/
├── CLAUDE.md                          # ← dit bestand (root briefing)
├── auto-alert/
│   ├── CLAUDE.md                      # gedetailleerde technische briefing
│   ├── scraper.py                     # de scraper (~1955 regels, alles-in-één)
│   ├── requirements.txt               # requests, beautifulsoup4, anthropic
│   ├── test_ai_scoring.py             # AI scoring tests
│   ├── test_url.py                    # losse URL test
│   └── .env                           # LOKAAL ONLY, nooit committen
└── .github/workflows/
    ├── alert.yml                      # ACTIEVE workflow (workflow_dispatch)
    ├── research-prices.yml            # NL-marktprijzen research
    ├── test-ai.yml
    └── test-url.yml
```

## Hoe het draait

- **GitHub Actions**, getriggerd door **cron-job.org** (elke 3 min) via
  `workflow_dispatch` — GitHub-cron is uit (kan niet onder 5 min)
- 13 mobile.de zoek-URL's, parallel opgehaald
- **Scrape.do** voor mobile.de (DataDome bypass, super mode + GDPR cookie)
- **Claude Haiku 4.5** voor feature-scoring (27 opties + kleur, merk-bewust)
- **NL-marktprijs vergelijking** voor de import-marge in de alert
- **Telegram Bot** voor alerts
- **SQLite** (`listings.db`) als database, gecached via Actions cache

## Secrets (GitHub Actions)

| Secret | Doel |
|--------|------|
| `SCRAPE_DO_TOKEN` | Scrape.do API |
| `ANTHROPIC_API_KEY` | Claude Haiku scoring |
| `TELEGRAM_BOT_TOKEN` | Telegram alerts |
| `TELEGRAM_CHAT_ID` | Telegram chat |

## Scrape.do budget

Custom plan: **1.250.000 credits/maand**. Elke run kost credits voor de 13
zoekpagina's (super mode) + per nieuwe listing een detail-page fetch. Ruimte
genoeg; cron-job.org pingt de workflow elke 3 min.

## Wat Claude moet doen

### Bij bugs / log-analyse

1. Lees logs via `gh run list --workflow=alert.yml` en `gh run view <id> --log`
2. Zoek het probleem in `auto-alert/scraper.py` (zie `auto-alert/CLAUDE.md` voor file:line referenties)
3. Fix → commit → push naar de actieve branch

### Bij verbeteringen

Lees `auto-alert/CLAUDE.md` sectie "Verbeterpunten" voor de backlog.
Geen refactors of feature creep bij bug-fixes — alleen wat gevraagd wordt.

## Git regels

- **Nooit pushen naar `master`** zonder expliciete toestemming
- Branch moet beginnen met `claude/` en eindigen op het session-id
- Geen `--force` push
- Geen secrets committen

## Communicatie

- Djari praat Nederlands
- Houd antwoorden kort en direct
- Geen onnodige uitleg of stappen — doe het gewoon
- Als je iets niet kunt (bv. geen `gh` CLI), zeg dat meteen
