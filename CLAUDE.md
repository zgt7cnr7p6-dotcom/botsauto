# CLAUDE.md — Botsauto

> Claude Code leest dit bestand automatisch. Lees ook `auto-alert/CLAUDE.md`
> voor de volledige technische briefing van de scraper.

## Wat is dit project?

Een geautomatiseerd systeem dat autoadvertenties (nu **mobile.de**) monitort,
elke auto **AI-scoort op uitrusting**, vergelijkt met de **NL-marktprijs** (voor
de import-marge) en **Telegram-alerts** stuurt bij goede koopjes.

**Huidig gebruik:** de eigenaar handelt in **Duitse premium-auto's** en gebruikt
het om zelf goede inkopen te vinden. De huidige zoek-set (zie hieronder) is de
eigen inkoop-focus en **wijzigt regelmatig** (URL's erbij/eraf).

**Productvisie:** de engine is bewust **merk-onafhankelijk en zelflerend**, zodat
het commercieel **verhuurbaar** wordt aan autobedrijven — van goedkope auto's tot
ultraluxe. Elk bedrijf werkt met andere auto's; het systeem moet zonder
code-aanpassingen met elk merk/segment kunnen werken en met elke auto die
binnenkomt **automatisch slimmer worden**. Zie "Productvisie & roadmap".

## Huidige zoek-set (eigen inkoop-focus, wijzigt)

Premium plug-in hybrides met panoramadak, 2021+, binnen budget:
- **Audi**: Q3 (Sportback), Q5 (Sportback), A3, A4 Avant, Q8
- **Mercedes**: C-Klasse (sedan/Estate), GLC, CLA, E-Klasse
- **BMW**: 3-serie / 330e (sedan/Touring)
- **Cupra**: Formentor

> De scoring/vergelijking werkt echter voor **elk** merk/model dat je toevoegt —
> deze lijst is alleen de huidige zoekopdracht, geen limiet van het systeem.

## Repo structuur

```
botsauto/
├── CLAUDE.md                          # ← dit bestand (root briefing)
├── HANDOFF.md                         # begeleide setup voor nieuwe eigenaar
├── auto-alert/
│   ├── CLAUDE.md                      # gedetailleerde technische briefing
│   ├── scraper.py                     # de scraper (~2300 regels, alles-in-één)
│   ├── nl_prices.py                   # NL-prijzen via Gaspedaal (live DB + link)
│   ├── requirements.txt               # requests, beautifulsoup4, anthropic
│   ├── test_ai_scoring.py             # AI scoring tests
│   ├── test_url.py                    # losse URL test
│   └── .env                           # LOKAAL ONLY, nooit committen
└── .github/workflows/
    ├── alert.yml                      # ACTIEVE workflow (workflow_dispatch)
    ├── research-prices.yml            # NL-marktprijzen research
    ├── test-ai.yml                    # AI-scoring test (mét Anthropic key)
    └── test-url.yml
```

## Hoe het draait

- **GitHub Actions**, getriggerd door **cron-job.org** (elke 3 min) via
  `workflow_dispatch` — GitHub-cron is uit (kan niet onder 5 min)
- ~13 mobile.de zoek-URL's, parallel opgehaald
- **Scrape.do** voor mobile.de (DataDome bypass, super mode + GDPR cookie)
- **Claude Haiku 4.5** voor feature-scoring (**35 opties + kleur, merk-generiek** —
  detecteert zelf het merk en mapt elk merk's termen op de vaste optie-lijst)
- **NL-marktprijs vergelijking** via een **dagelijks ververste Gaspedaal-database**
  (`nl_prices.py`) → import-marge + klikbare "Vergelijk op Gaspedaal"-link
- **Telegram Bot** voor alerts
- **SQLite** (`listings.db`) als database, gecached via Actions cache

## Slimme, zelflerende scoring (het commerciële hart)

Deze delen maken het systeem merk-onafhankelijk en zelfverbeterend:

- **Merk-generieke extractie** — Haiku herkent elk merk/model en mapt de
  pakketten/termen van dat merk (S line, AMG Line, M Sport, VZ, R-Line, R-Design…)
  op de vaste 35-vakjes taxonomie. Een nieuw merk toevoegen = alleen een zoek-URL.
- **Markt-relatieve score** (`market_spec_verdict`) — een auto wordt vergeleken met
  opgeslagen **soortgenoten** (per basismodel gepoold), rariteit-gewogen percentiel.
  Geen hardcoded "max opties" meer; wordt scherper met elke auto. Terugval op vaste
  drempels zolang een model < 10 soortgenoten heeft.
- **Zelflerend "leverbaar per model"** (`available_features`) — leert uit de data
  welke opties een model écht kan hebben (optie telt pas bij ≥2 auto's; actief vanaf
  ≥30 soortgenoten). Geen misleidende ❌ meer voor onmogelijke opties; vangnet: wat
  de auto zelf heeft telt altijd mee. Vervangt de handmatige uitsluitingslijst.
- **Dataset-fundament** — élke gescrapete + gescoorde auto wordt opgeslagen (merk,
  model_key, opties, prijs, jaar, km), ook de auto's die géén alert worden. Dat is
  de brandstof voor bovenstaande zelflerende delen.

## Productvisie & roadmap

Doel: van "13 hardcoded modellen voor eigen gebruik" naar een **merk-onafhankelijk,
zelfverbeterend, per-klant instelbaar** product voor autobedrijven.

- ✅ **Data-fundament** (alle auto's + opties structureel opslaan)
- ✅ **Markt-relatieve score** (zelfverbeterend, geen vaste noemer)
- ✅ **Merk-generieke extractie** (35 vakjes, elk merk)
- ✅ **Zelflerend leverbaar-per-model**
- ⏭️ **Merk-namen uit de advertentie tonen** (Haiku's eigen term bij ✅, i.p.v.
  neutrale namen — geen per-merk maps nodig)
- ⏭️ **Per-klant config (multi-tenant)** — must-haves, budget, doelmodellen en
  weging per autobedrijf instelbaar; dezelfde engine, andere config

## Secrets (GitHub Actions)

| Secret | Doel |
|--------|------|
| `SCRAPE_DO_TOKEN` | Scrape.do API |
| `ANTHROPIC_API_KEY` | Claude Haiku scoring |
| `TELEGRAM_BOT_TOKEN` | Telegram alerts |
| `TELEGRAM_CHAT_ID` | Telegram chat |

## Scrape.do budget

Doel-plan: **1.250.000 credits/maand**. Elke run kost credits voor de ~13
zoekpagina's (super mode, 10 cr elk) + per nieuwe listing een detail-page fetch
(render, 5 cr) + 1×/dag de Gaspedaal-refresh. Bij ~3-min cadans is dat fors —
op een kleiner plan raakt het snel op (zie de memory over credit-verbruik en
pauzeren via `gh workflow disable alert.yml`).

## Wat Claude moet doen

### Bij bugs / log-analyse

1. Lees logs via `gh run list --workflow=alert.yml` en `gh run view <id> --log`
   (`gh` staat op `~/.local/bin/gh`, niet op PATH — gebruik het volledige pad)
2. Zoek het probleem in `auto-alert/scraper.py` (zie `auto-alert/CLAUDE.md` voor
   file:line referenties)
3. Fix → commit → push naar de actieve branch

### Bij verbeteringen

Actieve feature-ontwikkeling richting de productvisie is verwacht wanneer gevraagd.
Bij **bug-fixes** geldt: geen refactors of feature creep — alleen wat gevraagd wordt.
De AI-scoring-prompt en zoek-URL's zijn empirisch afgesteld: niet zomaar herschrijven.

## Git regels

- **Nooit pushen naar `master`** zonder expliciete toestemming
- Branch moet beginnen met `claude/` en eindigen op het session-id
- Geen `--force` push
- Geen secrets committen

## Communicatie

- Nederlands, kort en direct
- Geen onnodige uitleg of stappen — doe het gewoon
- Als je iets niet kunt (bv. geen token lokaal), zeg dat meteen
