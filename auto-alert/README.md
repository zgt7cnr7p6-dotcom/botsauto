# Auto Alert — Audi Q3 45 TFSI e Scraper

Geautomatiseerde scraper die elke 30 minuten mobile.de en AutoScout24 doorzoekt naar Audi Q3 45 TFSI e aanbiedingen en Telegram alerts stuurt bij goede matches.

## Zoekcriteria

| Parameter | Waarde |
|-----------|--------|
| Model | Audi Q3 45 TFSI e |
| Brandstof | Hybride |
| Bouwjaar | 2018+ |
| Kilometerstand | Max 80.000 km |
| Prijs | Max €37.500 |

## Must-have features (scoring)

Elke advertentie wordt gescored op deze features (0-6 punten):

1. Panoramadak
2. Achteruitrijcamera
3. Ambient lighting
4. S line
5. Keyless entry
6. Elektrische stoelen

Een Telegram alert wordt verstuurd bij score >= 2.

## Setup

### GitHub Actions Secrets

Stel de volgende secrets in via Settings > Secrets > Actions:

- `TELEGRAM_BOT_TOKEN` — Bot token van @BotFather
- `TELEGRAM_CHAT_ID` — Je Telegram chat ID

### Handmatig triggeren

Ga naar Actions > Auto Alert Scraper > Run workflow

## Architectuur

- **Scraper**: Playwright (headless Chromium) voor JavaScript-heavy sites
- **Database**: SQLite — wordt bewaard tussen runs via GitHub Actions artifacts (90 dagen retentie)
- **Alerts**: Telegram Bot API
- **Schedule**: GitHub Actions cron (elke 30 min)
