#!/usr/bin/env bash
# Dagelijks gezondheidsrapport naar Telegram.
#
# Bestaansreden: de bot lag ooit 4 weken stil zonder dat iemand het merkte —
# uitval is onzichtbaar als je alleen op binnenkomende alerts let. Dit bericht
# komt elke dag, dus het uitblijven ervan is zélf al een signaal.
set -uo pipefail

ENV_FILE=/home/botsauto/app/auto-alert/.env
DB=/home/botsauto/app/auto-alert/listings.db
set -a; . "$ENV_FILE"; set +a

SINCE="24 hours ago"
LOG=$(journalctl -u botsauto.service --since "$SINCE" --output=cat 2>/dev/null)

# "=== Klaar: 3 totaal, 2 nieuw, 2 alerts verstuurd ===" uitlezen en optellen
runs=$(grep -c "=== Klaar:" <<<"$LOG")
nieuw=$(grep -oE "Klaar: [0-9]+ totaal, ([0-9]+) nieuw" <<<"$LOG" | grep -oE "[0-9]+ nieuw" | grep -oE "^[0-9]+" | paste -sd+ - | bc 2>/dev/null || echo 0)
alerts=$(grep -oE "([0-9]+) alerts verstuurd" <<<"$LOG" | grep -oE "^[0-9]+" | paste -sd+ - | bc 2>/dev/null || echo 0)
totaal_db=$(sqlite3 "$DB" "SELECT count(*) FROM listings;" 2>/dev/null || echo "?")
zoekopdrachten=$(sqlite3 "$DB" "SELECT count(*) FROM searches WHERE active = 1;" 2>/dev/null || echo "?")

# Bekende storingen opsporen
waarschuwingen=""
grep -qi "credit balance is too low" <<<"$LOG" && waarschuwingen+="\n⚠️ Anthropic-tegoed op (geen AI-score)"
grep -qiE "Monthly request limit|status=401" <<<"$LOG" && waarschuwingen+="\n⚠️ Scrape.do: credits op of token ongeldig"
grep -qi "Telegram fout" <<<"$LOG" && waarschuwingen+="\n⚠️ Telegram-bezorging faalde"
grep -qi "SCRAPER GEFAALD" <<<"$LOG" && waarschuwingen+="\n⚠️ Scraper volledig gefaald"
[ "$runs" -eq 0 ] && waarschuwingen+="\n🚨 GEEN ENKELE RONDE in 24 uur!"
systemctl is-active --quiet botsauto-bot.service || waarschuwingen+="\n⚠️ Telegram-bediening ligt eruit (/links werkt niet)"

kop="🩺 <b>Botsauto dagrapport</b>"
[ -n "$waarschuwingen" ] && kop="⚠️ <b>Botsauto dagrapport</b>"

tekst="${kop}
🔄 ${runs} rondes (24u)
🚗 ${nieuw} nieuwe auto's · ${alerts} alerts
🔍 ${zoekopdrachten} zoekopdrachten
💾 ${totaal_db} auto's in database${waarschuwingen}"

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=${tekst}" \
  -d "parse_mode=HTML" -d "disable_notification=true" >/dev/null
