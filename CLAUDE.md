# CLAUDE.md — Botsauto

> Claude Code leest dit bestand automatisch. Lees ook `auto-alert/CLAUDE.md`
> voor de volledige technische briefing van de scraper.

## Wat is dit project?

Een geautomatiseerde scraper die mobile.de monitort op **Audi Q3/A3,
Mercedes C-Klasse/GLC, en BMW 330e** (hybrid, met panoramadak) en
Telegram alerts stuurt bij goede matches. Eigenaar: Djari.

## Repo structuur

```
botsauto/
├── CLAUDE.md                          # ← dit bestand (root briefing)
├── auto-alert/
│   ├── CLAUDE.md                      # gedetailleerde technische briefing
│   ├── scraper.py                     # de scraper (~1385 regels, alles-in-één)
│   ├── requirements.txt               # requests, beautifulsoup4, anthropic
│   ├── test_ai_scoring.py             # AI scoring tests
│   ├── test_url.py                    # losse URL test
│   └── .env                           # LOKAAL ONLY, nooit committen
└── .github/workflows/
    ├── alert.yml                      # ACTIEVE workflow (cron */5 7-18 UTC)
    ├── test-ai.yml
    └── test-url.yml
```

## Hoe het draait

- **GitHub Actions** cron elke 5 min, 07-18 UTC (08-19 CET)
- **Scrape.do** voor mobile.de (DataDome bypass, super mode)
- **Claude Haiku 4.5** voor feature-scoring (24 opties + kleur)
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

Custom plan: **1.250.000 credits/maand**. Huidig verbruik: ~8%.
Elke scraper-run kost ~30 credits (zoekpagina's) + ~25 per nieuwe listing
(detail page). Er is ruimte genoeg voor hogere frequentie, maar GitHub
Actions cron gaat niet sneller dan elke 5 minuten.

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
