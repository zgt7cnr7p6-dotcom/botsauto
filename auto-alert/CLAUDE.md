# CLAUDE.md — Auto-Alert Scraper

> Lees dit eerst voordat je iets aan dit project verandert. Dit is de volledige
> technische briefing. Het hoort bij de root `CLAUDE.md`.
> Laatst bijgewerkt: **2026-08-20**.

## TL;DR

Python-scraper die **mobile.de** afzoekt en **Telegram-alerts** stuurt bij nieuwe
auto's. Per auto: detailpagina ophalen → met **Claude Haiku** scoren op **35 opties
+ kleur** → vergelijken met de **NL-marktprijs** (import-marge) → alert.

In de alert staat **hoe lang de auto al online is** ("Online sinds … (4 min geleden)"),
uit mobile.de's eigen tijdstempel. Dat getal is de meetlat voor reactiesnelheid.

> Er was tijdelijk een aparte **flits-alert** vóór de dure stappen; die is op
> 2026-08-20 verwijderd. Zinvol toen een ronde op GitHub Actions 30-60s duurde,
> maar op de eigen server duurt een hele ronde ~1s — beide berichten kwamen dus
> tegelijk aan.

Alles zit in één bestand: **`scraper.py`** (~2290 regels).

## Zoek-URL's — volledig flexibel

`MOBILE_DE_SEARCH_URLS` (`scraper.py:69`) is een **lijst die vrij groeit of krimpt**.
Nu staan er **2** in (Audi + BMW, beide hybride + panoramadak), maar dat is een
momentopname: de eigenaar past de set regelmatig aan en wil er mogelijk **10+**
draaien. Een link toevoegen = een `{"label": ..., "url": ...}` erbij, verder niets.

Optionele velden per URL-config:
- `label` — naam in de logs
- `require_pano_in_desc` — `True` → alleen alerten als de AI panoramadak detecteert
- `require_text` — alleen doorsturen als deze tekst in titel/omschrijving staat
- `min_listing_date` — `YYYY-MM-DD`: oudere listings opslaan zonder alert

> ⚠️ **Elke extra link vermenigvuldigt de kosten.** Zie "Credits" hieronder —
> dat is de belangrijkste ontwerpbeperking van dit project.

`SEARCH_CRITERIA` (`scraper.py:53`) is nog slechts een **legacy label** voor de
logregel; de echte filtering zit volledig in de mobile.de-URL's.

## Draaien & triggers

**Sinds 2026-08-20 draait de bot op een eigen Hetzner-server** (`62.238.17.43`),
niet meer op GitHub Actions. Zie **`server/README.md`** voor de volledige opstelling
en bediening.

- **systemd-timer** `botsauto.timer` → elke 3 min (`OnCalendar=*:0/3`).
  Voor 60 sec: `*:*:0` — reken eerst de credits na.
- Draait als gebruiker `botsauto`, code in `/home/botsauto/app`, secrets in
  `.env` (0600), database gewoon als bestand ernaast.
- **Bewaking:** dagelijks Telegram-rapport (08:05) + melding bij een harde crash.
  Blijft het dagrapport uit, dán is er iets mis — dat signaal ontbrak vroeger.

> **Historisch (uitgeschakeld):** GitHub Actions `alert.yml` + cron-job.org +
> GitHub-cron + de `db-data`-branch. Alle uitval van juli/aug 2026 kwam door die
> afhankelijkheden (cron-job.org zette zichzelf uit toen de repo privé werd,
> Actions-minuten). De workflow staat op `disabled` en dient nog als noodoplossing.

### Draaivenster (`run_mode`, `scraper.py:2291`)

| Tijd (CET) | Gedrag |
|---|---|
| **08:00–19:59** | vol gas — elke ronde draait |
| 20:00, 00:00, 04:00 (± 5 min) | één bijhoud-ronde |
| overige nachturen | overslaan |

Instelbaar via `ACTIVE_START_HOUR` / `ACTIVE_END_HOUR` / `NIGHT_SLOT_HOURS`
(`scraper.py:2286`). `run_mode()` is bewust een **losse, pure functie** zodat het
schema testbaar is zonder de klok te manipuleren.

**Onderbouwing (eigen dataset, 470 auto's):** 90,4% komt online tussen 08:00–20:00;
piek 11:00–14:00 (45%). 's Nachts 1-2 per link — ruim onder de 24-per-pagina grens,
dus 4-uursgaten missen niets. Dit halveerde het verbruik (288k → ~148k/mnd) zónder
snelheidsverlies overdag.

Handmatig buiten het venster draaien: `--force` meegeven
(`sudo -u botsauto /home/botsauto/venv/bin/python scraper.py --force`).

## Pipeline per run (`_run_scrape`, `scraper.py:2025`)

1. **Zoekpagina's parallel** (4 workers) via Scrape.do (`super=true`, `geoCode=de`).
2. **Parse + filter** (`scrape_mobile_de`, `:1781`) — gesponsorde ads, al bekende
   listings, `require_text` en `min_listing_date` vallen af.
3. **Leeftijdsfilter** — auto's ouder dan `MAX_LISTING_AGE_HOURS` (24u) worden
   stil opgeslagen, géén alert. Vóór de detailpagina, dus scheelt ook credits.
4. **Detailpagina's parallel** (8 workers, `:2018`) + GDPR-cookie; HTML opgeschoond
   (`clean_detail_html`, `:434`). Mislukt de fetch → `detail_incomplete=True` →
   **niet opslaan**, zodat de volgende run het opnieuw probeert.
5. **AI-scoring parallel** (5 workers, `score_listing`, `:850`) — Claude Haiku 4.5.
   Mislukt het → `score=-1`, maar de auto wordt **wél gealerteerd** (kale alert).
6. **Volledige alert** (`send_telegram`, `:1422`). Pas **ná** succesvol versturen
   wordt de listing opgeslagen (mislukt Telegram → retry volgende run).
7. **Baseline per zoekopdracht** — de eerste run van een nieuwe/gewijzigde URL slaat
   de dan-online auto's **stil** op (geen alert-golf). Daarna wél alerten.
   Tabel `search_state`; vervangt de oude "seed mode".

## AI-scoring (`AI_SCORING_PROMPT`, `scraper.py:501`)

**35 opties + kleur**, `FULL_OPTION_FEATURES` (`:82`). De prompt is **merk-generiek**:
Haiku herkent zelf merk/model en mapt de pakketten van dat merk (S line, AMG Line,
M Sport, VZ, R-Line, R-Design…) op de vaste taxonomie. Nieuw merk = alleen een
zoek-URL, geen code.

**Sportpakket-regel:** interieur/exterieur alleen aanvinken bij **expliciet** bewijs.
"S line" of "Sportpaket" zonder aanwijzing → géén van beide gokken, maar tonen zoals
het er staat (`sportpakket_detail`, de letterlijke term uit de advertentie).

**Implicaties afgedwongen in code:** `camera_360 → camera_achteruit` ·
`luchtvering → adaptief_onderstel` · `acc + lane_assist ↔ travel_assist`.

## Zelflerende delen (het commerciële hart)

- **`market_spec_verdict`** (`:1115`) — markt-relatieve score: rariteit-gewogen
  percentiel t.o.v. opgeslagen soortgenoten per basismodel. Geen hardcoded "max
  opties". Terugval op vaste drempels onder `MARKET_MIN_PEERS = 10` (`:1112`).
- **`available_features`** (`:1184`) — leert per model welke opties écht leverbaar
  zijn: telt pas mee vanaf `AVAIL_MIN_PEERS = 30` soortgenoten en `AVAIL_MIN_COUNT = 2`
  bevestigingen (één foutieve vink telt dus niet). Voorkomt misleidende ❌.
- **Dataset-fundament** — élke gescrapete auto wordt opgeslagen, ook zonder alert.
  Dat is de brandstof voor bovenstaande.

## NL-marktprijs (`nl_prices.py`)

**Live** opgehaald uit exact de Gaspedaal-zoeklink die in de alert wordt meegestuurd
(`cheapest_nl`): goedkoopste NL-prijs → import-marge + klikbare "Vergelijk zelf op
gaspedaal"-link. De slug wordt **merk-generiek** uit de titel afgeleid
(`gaspedaal_slug`), met een breadcrumb-check die een verkeerde slug detecteert
(voorkomt bv. A-klasse-prijzen tonen als C-klasse).

> De oude `NL_MARKET_PRICES`-lookuptabel is vervangen: prijs en link komen nu uit
> **dezelfde bron**, dus ze kunnen niet meer uit elkaar lopen.

## Filters (standaard UIT)

- `SPORT_PACKAGE_FILTER_ENABLED = False` (`:905`) — code behouden, niet actief
- `MODEL_WHITELIST_ENABLED = False` (`:906`) — de eigenaar filtert via de mobile.de-link

## Database

SQLite `listings.db` — gewoon een bestand naast de code op de server.
*(De oude `db-data`-git-branch is niet meer nodig.)* Tabellen:
- `listings` — id, titel, prijs, jaar, km, url, score, features (JSON), model_key,
  brand, color, listing_date, location, first_seen, last_seen
- `search_state` — welke zoek-URL's al gebaselined zijn
*(`flash_state` kan in oude databases nog bestaan — ongebruikt sinds 2026-08-20.)*

## Telegram

Alerts gaan naar een **groep** (secret `TELEGRAM_CHAT_ID`), niet naar een privé-chat,
zodat meerdere mensen meekijken. Wordt de groep ooit een supergroep, dan verandert
het id en stopt de bezorging.

## Credits (Scrape.do) — de belangrijkste beperking

**mobile.de vereist super-mode.** Live getest (2026-08-20): basis (1cr),
basis+geoCode (1cr) en render-zonder-super (5cr) geven **allemaal HTTP 400** met
*"use super gateway … with 'Super=True'"*. **10 credits per zoekpagina is de bodem.**

| Actie | Credits |
|---|---|
| Zoekpagina (per link, per run) | **10** |
| Detailpagina (per nieuwe auto) | 10 |
| Gaspedaal NL-prijs (per alert) | 5 |

**Het aantal auto's maakt vrijwel niets uit (~1,5% van de rekening).** De formule is:

```
credits/maand ≈ runs_per_dag × aantal_links × 10 × 30
```

Met het huidige venster: **246 runs/dag** (3 min) of **723 runs/dag** (60 sec).

| Links | 3 min | 60 sec |
|---|---|---|
| 2 | 148k | 434k |
| 5 | 369k | 1,08M |
| 10 | 738k | 2,17M |

⚠️ Het plan (1,25M/mnd) wordt **gedeeld met een ander project**. Bij veel links is
60 sec dus niet haalbaar — dan is ~3 min de realistische snelheid. **Reken dit altijd
na voordat je links toevoegt of het tempo verhoogt.**

Concurrency-limiet (50 op het Pro-plan) is géén knelpunt: piek is ~8 gelijktijdige
verzoeken (8 detail-workers). Bij overschrijding geeft Scrape.do 429 → de client
wacht en probeert opnieuw.

## Secrets

| Env var | Doel | Verplicht |
|---------|------|-----------|
| `SCRAPE_DO_TOKEN` | Scrape.do — super-mode voor mobile.de | ja |
| `ANTHROPIC_API_KEY` | Claude Haiku scoring | ja |
| `TELEGRAM_BOT_TOKEN` | Telegram alerts | ja (anders DRY_RUN) |
| `TELEGRAM_CHAT_ID` | Doelgroep | ja |

## Belangrijke functies (file:line)

| Regel | Functie |
|---|---|
| `:2240` | `main` — venster-check + retry-loop |
| `:2227` | `run_mode` — vol gas / nacht / overslaan |
| `:2025` | `_run_scrape` — kern: scrape → filter → detail → score → alert |
| `:1708` | `scrape_mobile_de` — zoekpagina-parser |
| `:1945` | `_fetch_detail_pages` · `:1928` `_fetch_single_detail` |
| `:1561` | `scrape_do_fetch` — Scrape.do-client met retry |
| `:731` `:821` | `score_listing_ai` / `score_listing` |
| `:472` | `AI_SCORING_PROMPT` |
| `:1306` `:1328` | `_listing_age_hours` / `_online_ago` — leeftijd uit mobile.de's tijdstempel |
| `:1346` | `send_telegram` |
| `:1086` | `market_spec_verdict` · `:1155` `available_features` |
| `:1197` | `detect_model` |
| `:2224` | `MAX_LISTING_AGE_HOURS` · `:2214` venster-constanten |

## Storingen — check deze eerst bij "geen meldingen"

1. **Timer stil** → `systemctl status botsauto.timer` op de server
2. **Anthropic-tegoed op** → `credit balance is too low` → score=-1 → 0 alerts
3. **Scrape.do-credits op** → 401 (verwarrend: zelfde code als ongeldige token —
   check `/info?token=` → `RemainingMonthlyRequest`)
4. **Groep omgezet naar supergroep** → chat-id veranderd

> Hoofdsignaal: het **dagelijkse 08:05-rapport** in Telegram. Blijft dat uit,
> dan is er iets mis — dat is precies het signaal dat vroeger ontbrak.

## Dev-loop

```bash
ssh root@62.238.17.43

journalctl -u botsauto.service -n 50      # laatste logs
journalctl -u botsauto.service -f         # live meekijken
systemctl start botsauto.service          # nu een ronde draaien
systemctl list-timers botsauto            # volgende ronde?

# Database bekijken
sudo -u botsauto sqlite3 /home/botsauto/app/auto-alert/listings.db

# Code bijwerken na een push
sudo -u botsauto git -C /home/botsauto/app pull
```

Lokaal testen: `python scraper.py --dry-run --force` (geen Telegram, geen venster-check).
Zie ook `server/README.md`.

## Wat er NIET moet gebeuren

- **Geen secrets committen** — `.env`, `*.db`, `debug_*.html` blijven gitignored
- **Geen refactor van de scoring-prompt of zoek-URL's** tenzij gevraagd — empirisch afgesteld
- **Geen `git push --force`**, **geen push naar `master`**
- **Geen Playwright/headless Chrome** — alles via Scrape.do
- **Geen feature creep** bij bug-fixes — alleen het gerapporteerde fixen
- **Links toevoegen zonder de credits na te rekenen**

## Backlog

1. **Zoeklinks via Telegram beheren** — links van code naar database, plus een
   luisteraar die direct op berichten reageert (link plakken → knop "Toevoegen",
   `/links` met verwijderknoppen). Alleen de eigenaar mag wijzigen; bij toevoegen
   een link-test (werkt hij? staat hij op nieuwste-eerst?) en een credit-waarschuwing.
2. **Naar 60 sec pollen** zodra het creditbudget het toelaat (`OnCalendar=*:*:0`)
3. **Merk-eigen optienamen tonen** (Haiku's eigen term bij ✅)
4. **Per-klant config (multi-tenant)**

✅ *Gedaan: verhuizing naar eigen server (2026-08-20) en hartslag/dagrapport.*

## Git / branch policy

- Hoofdbranch: `master` · actieve branch: `claude/deploy-auto-alert-scraper-ZsKua`
- Branch begint met `claude/` en eindigt op het session-id
