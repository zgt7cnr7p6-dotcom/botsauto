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
binnenkomt **automatisch slimmer worden**. Zie ook "Productvisie & roadmap".

## Huidige zoek-set (momentopname — wijzigt regelmatig)

`MOBILE_DE_SEARCH_URLS` is een **vrij aanpasbare lijst**. Op dit moment staan er **2**
links in, beide hybride + panoramadak:
- **Audi** (2021–2024, ≤100.000 km)
- **BMW** (2022–2024, ≤80.000 km)

> Dit is **geen limiet van het systeem**. De eigenaar past de set regelmatig aan en
> wil er mogelijk **10+** draaien. Een merk/model toevoegen = alleen een zoek-URL
> erbij; de scoring en vergelijking zijn merk-generiek.
>
> ⚠️ Wél eerst de **credits narekenen** — elke extra link vermenigvuldigt de kosten
> (zie "Scrape.do budget").

## Repo structuur

```
botsauto/
├── CLAUDE.md                          # ← dit bestand (root briefing)
├── HANDOFF.md                         # begeleide setup voor nieuwe eigenaar
├── auto-alert/
│   ├── CLAUDE.md                      # gedetailleerde technische briefing
│   ├── scraper.py                     # de scraper (~2300 regels, alles-in-één)
│   ├── nl_prices.py                   # NL-prijs live uit de Gaspedaal-zoeklink per alert
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
  `workflow_dispatch`, met een **GitHub-cron van 5 min als vangnet**
- **Draaivenster**: vol gas 08:00–20:00 CET, daarbuiten alleen bijhoud-rondes om
  20:00/00:00/04:00 — 90,4% van de auto's komt binnen dat venster online, dus dit
  halveert de credits zonder snelheidsverlies overdag
- **Flits-alert**: direct na detectie een korte "🆕 NIEUW ONLINE (X min geleden)"-melding,
  vóór detailpagina + AI-scoring; de volledige alert volgt ~30-60s later
- mobile.de zoek-URL's (nu 2, flexibel), parallel opgehaald
- **Scrape.do** voor mobile.de (DataDome bypass, super mode + GDPR cookie)
- **Claude Haiku 4.5** voor feature-scoring (**35 opties + kleur, merk-generiek** —
  detecteert zelf het merk en mapt elk merk's termen op de vaste optie-lijst)
- **NL-marktprijs vergelijking** — **live** opgehaald uit exact de Gaspedaal-zoeklink
  die in de alert wordt meegestuurd (`nl_prices.cheapest_nl`): goedkoopste NL-prijs
  → import-marge + klikbare "Vergelijk zelf op gaspedaal"-link. Slug wordt merk-
  generiek uit de titel afgeleid.
- **Telegram Bot** voor alerts
- **SQLite** (`listings.db`) als database, **durabel opgeslagen op de `db-data`
  git-branch** (elke run hersteld + force-push snapshot)

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

Al gedaan (huidige engine):
- ✅ **Data-fundament** (alle auto's + opties structureel opslaan)
- ✅ **Markt-relatieve score** (zelfverbeterend, geen vaste noemer)
- ✅ **Merk-generieke extractie** (35 vakjes, elk merk)
- ✅ **Zelflerend leverbaar-per-model**

Volgende:
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

## Hosting

- ⚠️ **De repo moet PUBLIEK blijven.** Twee redenen: (1) op privé kon de token van
  cron-job.org er niet meer bij → de job werd automatisch uitgezet en de bot lag
  **4 weken stil**; (2) privé-repo's hebben een limiet op GitHub Actions-minuten
  (≈2.000/mnd) die deze cadans ver overschrijdt. Publiek = onbeperkt.
- Er staan geen secrets in de code of de historie (gecontroleerd); alle sleutels
  zitten in GitHub Actions-secrets.
- **Backlog:** verhuizen naar een eigen server (always-on proces) — schrapt de
  1–1,5 min opstarttijd per run en maakt 60 sec pollen mogelijk. Dan vervallen
  cron-job.org, de GitHub-cron én de `db-data`-branch.

## Scrape.do budget

Plan: **1.250.000 credits/maand**, **gedeeld met een ander project**.

**mobile.de vereist super-mode** (live getest): goedkopere modes geven HTTP 400.
Dus **10 credits per zoekpagina** is de bodem. Detailpagina 10 cr (per nieuwe auto),
Gaspedaal 5 cr (per alert).

Het **aantal auto's maakt vrijwel niets uit** (~1,5%). De rekening is:

```
credits/maand ≈ runs_per_dag × aantal_links × 10 × 30
```

Met het huidige venster: 246 runs/dag (3 min) of 723 runs/dag (60 sec).

| Links | 3 min | 60 sec |
|---|---|---|
| **2 (nu)** | **148k** | 434k |
| 5 | 369k | 1,08M |
| 10 | 738k | 2,17M |

⚠️ **Reken dit altijd na voordat je links toevoegt of het tempo verhoogt** — bij veel
links is 60 sec niet haalbaar binnen het gedeelde budget.

Pauzeren: `gh workflow disable alert.yml` (weer aan: `enable`).

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
