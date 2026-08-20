# Server-setup (botsauto)

De bot draait sinds **2026-08-20** op een eigen Hetzner-server in plaats van
GitHub Actions. Dit is de referentie van die opstelling — de bestanden hier zijn
kopieën van wat op de server staat, zodat de configuratie versiebeheerd is.

## Waarom van GitHub Actions af

| | GitHub Actions | Eigen server |
|---|---|---|
| Opstarttijd per ronde | 1–1,5 min | **~0 sec** |
| Snelste cadans | ~3 min | **60 sec** haalbaar |
| Externe afhankelijkheden | cron-job.org, Actions-minuten, repo-zichtbaarheid | geen |
| Database | force-push naar `db-data`-branch | gewoon een bestand |

Alle storingen van juli/augustus 2026 (cron-job.org uitgezet, repo op privé,
Actions-minuten) kwamen door die afhankelijkheden. Die zijn nu weg.

## Opstelling

```
/home/botsauto/app/          git-clone van deze repo (branch claude/deploy-...)
/home/botsauto/app/auto-alert/.env         secrets (0600, gebruiker botsauto)
/home/botsauto/app/auto-alert/listings.db  database (verhuisd van db-data-branch)
/home/botsauto/venv/         Python-omgeving
/home/botsauto/healthcheck.sh              dagelijks gezondheidsrapport
   (zoekopdrachten staan in de DATABASE, tabel `searches` — te beheren via Telegram)
```

Draait als gebruiker **`botsauto`** (niet root). Firewall: alleen SSH.
Tijdzone: Europe/Amsterdam.

## Systemd-units

| Unit | Doel |
|---|---|
| `botsauto.service` | één scrape-ronde (oneshot) |
| `botsauto.timer` | **elke 3 min** (`OnCalendar=*:0/3`) — voor 60 sec: `*:*:0` |
| `botsauto-bot.service` | **Telegram-bediening** (long polling, `Restart=always`) |
| `botsauto-health.service/.timer` | dagelijks rapport naar Telegram, 08:05 |
| `botsauto-failure.service` | Telegram-melding zodra een ronde hard faalt |

Het **draaivenster** (vol gas 08:00–20:00, 's nachts elke 4 uur) zit in de
scraper zelf (`run_mode`), niet in de timer. De timer mag dus rustig elke
3 minuten vuren — buiten het venster stopt de scraper zelf meteen (0 credits).

## Bediening

```bash
ssh root@62.238.17.43

systemctl status botsauto.timer          # draait de planner?
systemctl list-timers botsauto           # wanneer de volgende ronde?
systemctl start botsauto.service         # nu een ronde draaien
journalctl -u botsauto.service -n 50     # laatste logs
journalctl -u botsauto.service -f        # live meekijken

systemctl stop botsauto.timer            # scrapen pauzeren
systemctl start botsauto.timer           # hervatten

systemctl status botsauto-bot            # draait de Telegram-bediening?
journalctl -u botsauto-bot -f            # live meekijken met commando's
```

**Code bijwerken** (na een push naar GitHub):
```bash
sudo -u botsauto git -C /home/botsauto/app pull
```

**Cadans wijzigen** (bv. naar 60 sec):
```bash
sed -i 's|OnCalendar=.*|OnCalendar=*:*:0|' /etc/systemd/system/botsauto.timer
systemctl daemon-reload && systemctl restart botsauto.timer
```
⚠️ Reken eerst de credits na — zie `auto-alert/CLAUDE.md` → "Credits".

## Bewaking

- **Dagelijks rapport** om 08:05 in Telegram: aantal rondes, nieuwe auto's,
  flitsen, alerts + waarschuwingen bij bekende storingen (Anthropic-tegoed op,
  Scrape.do 401, Telegram-fouten). **Blijft het rapport uit, dan is er iets mis** —
  dat is precies het signaal dat vroeger ontbrak.
- **Storingsmelding** bij een harde crash van een ronde.

## Herstel na een verse server

1. Server aanmaken (klein type volstaat), SSH-key toevoegen
2. `apt install python3 python3-venv git sqlite3 ufw fail2ban bc`
3. Gebruiker `botsauto` aanmaken, repo klonen naar `/home/botsauto/app`
4. venv + `pip install -r auto-alert/requirements.txt`
5. `.env` vullen met de 4 secrets (zie `auto-alert/CLAUDE.md`)
6. `listings.db` terugzetten (of leeg starten — dan baselinet hij vanzelf)
7. Units uit `server/systemd/` kopiëren, `systemctl enable --now botsauto.timer botsauto-health.timer`
