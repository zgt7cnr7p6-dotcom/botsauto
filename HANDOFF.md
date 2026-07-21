# HANDOFF.md — Auto-Alert scraper overnemen

> **Voor Claude:** dit bestand is een begeleide setup. De gebruiker heeft deze
> repo geforkt en wil zijn eigen werkende kopie. Loop de stappen hieronder in
> volgorde met hem door, één voor één. Vraag na elke stap of het gelukt is
> voordat je verder gaat. Ga uit van een beginner — leg dingen simpel uit, geen
> jargon. De gebruiker praat Nederlands.

## Wat is dit?

Een bot die elke paar minuten **mobile.de** afzoekt naar specifieke premium
plug-in hybrides (Audi Q3/Q5/A3/A4/Q8, Mercedes C/GLC/CLA/E, BMW 3-serie/330e,
Cupra Formentor) met panoramadak en sportpakket, en bij een match een
**Telegram-melding** stuurt met opties, prijs en import-marge.

De bot draait gratis op **GitHub Actions**. Hij leunt op 4 externe diensten
(hieronder). De code hoef je niet te begrijpen om 'm te laten draaien — je hoeft
alleen je eigen accounts te koppelen.

---

## Handige links

| Wat | Link |
|-----|------|
| **De repo (deze code)** | https://github.com/djari-moon/botsauto |
| **Direct forken** | https://github.com/djari-moon/botsauto/fork |
| GitHub account maken | https://github.com/signup |
| Scrape.do (mobile.de ophalen) | https://scrape.do |
| Anthropic / Claude API | https://console.anthropic.com |
| Telegram BotFather (bot maken) | https://t.me/BotFather |
| Telegram userinfobot (chat ID) | https://t.me/userinfobot |
| cron-job.org (optionele trigger) | https://cron-job.org |

---

## Wat je zelf nodig hebt (4 dingen)

Elk hiervan geeft je een **sleutel** (lange reeks tekens). Die 4 sleutels stop
je straks in de "kluis" van je repo (secrets). Gebruik **je eigen** accounts —
niet die van de vorige eigenaar — anders gebruik je zijn credits en komen de
meldingen bij hem binnen.

| Dienst | Waarvoor | Kosten |
|--------|----------|--------|
| **Scrape.do** | haalt mobile.de-pagina's op (mobile.de blokkeert gewone bots) | betaald |
| **Anthropic (Claude)** | beoordeelt elke auto op de opties | betaald, goedkoop |
| **Telegram-bot** | stuurt de meldingen | gratis |
| **Telegram chat ID** | naar welke chat de meldingen gaan | gratis |

---

## Stap 1 — De code overnemen (forken)

Forken = een eigen kopie van dit project op je eigen GitHub-account.

1. Maak een gratis account op https://github.com (als je die nog niet hebt).
2. Ga naar de originele repo en klik rechtsboven op **Fork**.
3. Je hebt nu `jouwnaam/botsauto` — jouw eigen kopie.

> De actieve branch heet `claude/deploy-auto-alert-scraper-ZsKua`. Dat is de
> "hoofd"-branch van dit project; die komt automatisch mee met de fork.

**Voor Claude:** bevestig met de gebruiker dat de fork gelukt is en vraag zijn
GitHub-gebruikersnaam + de repo-naam voordat je verder gaat.

---

## Stap 2 — Telegram-bot maken (gratis)

1. Open Telegram, zoek op **@BotFather** en start een chat.
2. Typ `/newbot` en volg de stappen (kies een naam + gebruikersnaam).
3. BotFather geeft je een **token** — een reeks als `123456:ABC-DEF...`.
   → Dit is je `TELEGRAM_BOT_TOKEN`. Bewaar 'm.

## Stap 3 — Je Telegram chat ID vinden (gratis)

1. Stuur eerst een berichtje ("hoi") naar je **eigen nieuwe bot** in Telegram.
2. Zoek daarna op **@userinfobot** in Telegram, start die, en hij toont je
   **Id** (een getal zoals `12345678`).
   → Dit is je `TELEGRAM_CHAT_ID`.

> Wil je de meldingen in een groep? Voeg je bot toe aan de groep en gebruik de
> group-id (begint vaak met een `-`). Voor jezelf is het bovenstaande genoeg.

## Stap 4 — Scrape.do account (betaald)

1. Maak een account op https://scrape.do
2. Kies een plan (de bot doet ~13 zoekpagina's per run + een detailpagina per
   nieuwe auto — reken op een plan met ruim voldoende credits).
3. In je dashboard vind je je **API token**.
   → Dit is je `SCRAPE_DO_TOKEN`.

## Stap 5 — Anthropic (Claude) API-sleutel (betaald, goedkoop)

1. Maak een account op https://console.anthropic.com
2. Zet wat tegoed op de account (Billing).
3. Ga naar **API Keys** → **Create Key**.
   → Dit is je `ANTHROPIC_API_KEY`.

---

## Stap 6 — De 4 sleutels in de "kluis" zetten (secrets)

Secrets = een afgeschermde plek in je repo voor je sleutels. Nooit sleutels in
de code zelf zetten.

1. Ga naar jouw repo op GitHub.
2. **Settings** (bovenaan) → links **Secrets and variables** → **Actions**.
3. Klik **New repository secret** en voeg deze 4 toe. **De namen moeten EXACT
   kloppen** (hoofdletters + underscores), want de bot zoekt ze op die naam op:

| Secret-naam | Wat erin komt |
|-------------|---------------|
| `SCRAPE_DO_TOKEN` | je Scrape.do token |
| `ANTHROPIC_API_KEY` | je Anthropic API-sleutel |
| `TELEGRAM_BOT_TOKEN` | je bot-token van BotFather |
| `TELEGRAM_CHAT_ID` | je chat ID |

**Voor Claude:** laat de gebruiker NOOIT de sleutels in de chat plakken of in
de code committen. Ze horen alleen in GitHub Secrets. Als hij ze toch plakt,
waarschuw hem dat 'ie ze moet vervangen (nieuwe genereren).

---

## Stap 7 — De bot laten lopen op een interval

De workflow (`.github/workflows/alert.yml`) draait op `workflow_dispatch`
(handmatige/externe trigger). Kies één manier om 'm automatisch te laten lopen:

**Optie A — GitHub cron (simpelst, aanrader).** Voeg bovenin
`.github/workflows/alert.yml` onder `on:` een schema toe:

```yaml
on:
  schedule:
    - cron: '*/10 * * * *'   # elke 10 minuten
  workflow_dispatch:
```

> GitHub-cron kan niet sneller dan elke 5 min en loopt soms een paar minuten
> achter. Voor deze bot is elke 10 min prima.

**Optie B — cron-job.org (sneller, meer werk).** Een gratis dienst die de
GitHub-API aanroept om de workflow te starten. Vereist een GitHub Personal
Access Token met `workflow`-rechten. Alleen doen als je écht < 5 min wilt.

Zet daarna Actions aan: tab **Actions** in je repo → bevestig dat workflows
mogen draaien.

**Voor Claude:** help hem optie A toe te passen (dat is één klein edit in
alert.yml, committen, pushen). Leg uit dat de eerste run in "seed-mode" gaat:
de database is leeg, dus alle huidige auto's worden opgeslagen ZONDER meldingen
(anders krijgt hij 50+ meldingen in één keer). Vanaf de tweede run krijgt hij
alleen nieuwe auto's.

---

## Stap 8 — Testen

- **In GitHub:** tab **Actions** → workflow **Auto Alert Scraper** → **Run
  workflow** (handmatig) → wacht tot 'ie klaar is → check bij **groen vinkje**
  dat er geen fouten zijn. De log-samenvatting staat ook als commit-comment.
- **Lokaal (optioneel, voor ontwikkelaars):**
  ```bash
  cd auto-alert
  python -m venv venv && source venv/bin/activate
  pip install -r requirements.txt
  export SCRAPE_DO_TOKEN=... ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
  python scraper.py --dry-run --force   # geen Telegram, geen quiet hours
  ```

---

## Zoekopdrachten aanpassen (welke auto's je zoekt)

De 13 mobile.de zoek-URL's staan in `auto-alert/scraper.py` in de lijst
`MOBILE_DE_SEARCH_URLS`. Wil je andere modellen/prijzen/regio's?

1. Ga naar mobile.de, stel je zoekopdracht in (merk, model, opties, prijs).
2. Kopieer de URL uit je adresbalk.
3. Vervang/voeg toe in `MOBILE_DE_SEARCH_URLS` (elk item heeft een `url` +
   `label`).

> Belangrijk: bouw de URL's altijd via mobile.de zelf. Handmatig knutselen met
> merk/model-ID's gaat vaak mis (dan komen er willekeurige merken door).

De gezochte modellen zitten ook in een whitelist (`is_wanted_model` in
`scraper.py`) die willekeurige merken tegenhoudt — pas die aan als je andere
modellen wilt.

---

## Voor Claude — hoe je deze persoon het beste helpt

- Lees ook `CLAUDE.md` (root) en `auto-alert/CLAUDE.md` voor de volledige
  technische briefing van de scraper.
- Werk de stappen hierboven af in volgorde; ga uit van een beginner.
- **Nooit** secrets in code of chat; alleen in GitHub Secrets.
- Bij fouten: lees de Actions-logs (tab Actions → run → job → log, of de
  commit-comment met de samenvatting). Veelvoorkomend:
  - `SCRAPE_DO_TOKEN niet geconfigureerd` → secret ontbreekt/verkeerde naam.
  - Veel `502` van Scrape.do → tijdelijke storing bij Scrape.do of mobile.de;
    meestal vanzelf over.
  - `0 alerts` terwijl er wel auto's zijn → check de whitelist/zoek-URL's.
- Push wijzigingen naar de bestaande branch; maak geen PR tenzij gevraagd.
