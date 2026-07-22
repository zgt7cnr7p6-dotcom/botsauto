# CLAUDE.md — Botsauto

> Claude Code leest dit bestand automatisch. Lees ook `auto-alert/CLAUDE.md`
> voor de technische briefing van de scraper, en **de Carwave-Data-Science-repo (`VISIE.md`)** voor de leidende
> visie (pan-Europees data-platform), de datalijst en het beslissingen-logboek.

## Wat is dit project?

Een geautomatiseerd systeem dat autoadvertenties (nu **mobile.de**) monitort,
elke auto **AI-scoort op uitrusting**, vergelijkt met de **NL-marktprijs** (voor
de import-marge) en **Telegram-alerts** stuurt bij goede koopjes.

**Huidig gebruik:** de eigenaar handelt in **Duitse premium-auto's** en gebruikt
het om zelf goede inkopen te vinden. De huidige zoek-set (zie hieronder) is de
eigen inkoop-focus en **wijzigt regelmatig** (URL's erbij/eraf).

**Productvisie:** de engine is bewust **merk-onafhankelijk en zelflerend**, zodat
het commercieel **verhuurbaar** wordt aan autobedrijven — van goedkope auto's tot
ultraluxe. De volgende, grotere stap in die visie is een **pan-Europees
data-platform**: alle gebruikte-autodata uit heel Europa longitudinaal scrapen en
opslaan als fundament voor **eigen, accurate prijsbepaling** (à la JP.cars). Zie
**de Carwave-Data-Science-repo (`VISIE.md`)** — dat is nu het leidende richtingsdocument. Zie ook "Productvisie
& roadmap".

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

## Hoe het draait (huidige systeem)

- **GitHub Actions**, getriggerd door **cron-job.org** (elke 3 min) via
  `workflow_dispatch` — GitHub-cron is uit (kan niet onder 5 min)
- ~13 mobile.de zoek-URL's, parallel opgehaald
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

> ⚠️ Deze hosting/opslag is de *huidige* situatie. De pan-EU-visie ontgroeit dit
> (SQLite-op-git-branch + GitHub Actions schalen niet naar heel Europa + foto's).
> Zie de Carwave-Data-Science-repo (`VISIE.md`) §9 — infra-keuze (Postgres + object-opslag + eigen worker) staat
> op de roadmap als Fase 0.

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
  de brandstof voor bovenstaande zelflerende delen — en de eerste stap richting het
  longitudinale panel uit de Carwave-Data-Science-repo (`VISIE.md`).

## Productvisie & roadmap

Doel: van "13 hardcoded modellen voor eigen gebruik" naar een **merk-onafhankelijk,
zelfverbeterend, per-klant instelbaar** product — en uiteindelijk een **pan-Europees
data-platform** dat auto's zelf accuraat prijst. Volledige uitwerking + het
beslissingen-logboek staan in **de Carwave-Data-Science-repo (`VISIE.md`)**.

Al gedaan (huidige engine):
- ✅ **Data-fundament** (alle auto's + opties structureel opslaan)
- ✅ **Markt-relatieve score** (zelfverbeterend, geen vaste noemer)
- ✅ **Merk-generieke extractie** (35 vakjes, elk merk)
- ✅ **Zelflerend leverbaar-per-model**

Volgende, grote richting (zie de Carwave-Data-Science-repo (`VISIE.md`)):
- ⏭️ **Fase 0 — infra** (Postgres + object-opslag + eigen worker; pan-EU + foto's)
- ⏭️ **Fase 1 — longitudinaal data-platform** (snapshots door de tijd, `vehicle_id`
  fingerprint, verdwijn/statijd = verkoop-proxy, foto- + kenteken-opslag, RDW)
- ⏭️ **Fase 2 — waarderingsmodel** (hedonisch €/optie per merk/model + courantheid)
- ⏭️ **Fase 3 — importmarge-engine** (BPM + kosten) + accuracy-backtest + multi-tenant

## Secrets (GitHub Actions)

| Secret | Doel |
|--------|------|
| `SCRAPE_DO_TOKEN` | Scrape.do API |
| `ANTHROPIC_API_KEY` | Claude Haiku scoring |
| `TELEGRAM_BOT_TOKEN` | Telegram alerts |
| `TELEGRAM_CHAT_ID` | Telegram chat |

## Hosting & privacy

- De repo staat op **privé** (plannen niet zichtbaar). ⚠️ **Let op:** privé-repo's
  hebben een **maandelijkse limiet op GitHub Actions-minuten** (Free ≈ 2.000/mnd).
  De ~3-min-cadans (≈480 runs/dag) gaat daar ver overheen → op termijn stopt Actions
  óf ga je betalen. Opties: (a) cadans verlagen, (b) Actions-minuten betalen, of
  (c) — aanbevolen, past bij de visie — de scraper naar een **eigen server/worker**
  verplaatsen. Zie de Carwave-Data-Science-repo (`VISIE.md`) §9.

## Scrape.do budget

Doel-plan: **1.250.000 credits/maand**. Elke run kost credits voor de zoekpagina's
(super mode, 10 cr elk) + per nieuwe listing een detail-page fetch (render, 5 cr) +
per alert een live Gaspedaal-fetch (render). Bij ~3-min cadans is dat fors — op een
kleiner plan raakt het snel op (zie de memory over credit-verbruik en pauzeren via
`gh workflow disable alert.yml`). De pan-EU-uitbreiding verhoogt dit budget
substantieel → gefaseerd uitrollen (zie de Carwave-Data-Science-repo (`VISIE.md`) §9).

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
