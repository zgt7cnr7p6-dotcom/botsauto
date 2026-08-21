#!/usr/bin/env python3
"""Telegram-bediening voor botsauto — zoekopdrachten beheren vanuit de chat.

Draait als losse, doorlopende service naast de scraper (die per timer één ronde
doet). Luistert via long polling, dus reageert binnen een seconde.

Beheer gaat via de tabel `searches` in dezelfde database; de scraper leest die
elke ronde opnieuw, dus een wijziging is meteen actief — geen herstart nodig.

Bediening:
  • een mobile.de-zoeklink plakken  → testen + bevestigen met een knop
  • /links    zoekopdrachten bekijken en verwijderen
  • /status   draait alles goed?
  • /help     uitleg

Alleen TELEGRAM_OWNER_ID mag wijzigen; anderen krijgen een vriendelijke weigering.
"""
from __future__ import annotations

import html
import logging
import os
import re
import sys
import time
import traceback

import requests

import scraper as core

log = logging.getLogger("telegram_bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

# Openstaande "wil je deze toevoegen?"-vragen: knop-id -> URL.
# Bewust in het geheugen: bij een herstart is de vraag simpelweg verlopen en
# plak je de link opnieuw. Telegram staat maar 64 bytes callback-data toe, dus
# de URL zelf kan er niet in.
_pending: dict[str, str] = {}
_counter = 0

MOBILE_LINK = re.compile(r"https?://\S*mobile\.de/\S+", re.I)


# ── Telegram-hulpjes ────────────────────────────────────────────────────────

def api(method: str, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=45)
        return r.json()
    except Exception as exc:
        log.warning("Telegram-aanroep %s faalde: %s", method, exc)
        return {"ok": False}


def stuur(chat_id, tekst, knoppen=None, reply_to=None):
    p = {"chat_id": chat_id, "text": tekst, "parse_mode": "HTML",
         "link_preview_options": {"is_disabled": True}}
    if knoppen:
        p["reply_markup"] = {"inline_keyboard": knoppen}
    if reply_to:
        p["reply_to_message_id"] = reply_to
    return api("sendMessage", **p)


def bewerk(chat_id, message_id, tekst, knoppen=None):
    p = {"chat_id": chat_id, "message_id": message_id, "text": tekst,
         "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    p["reply_markup"] = {"inline_keyboard": knoppen or []}
    return api("editMessageText", **p)


def _token() -> str:
    global _counter
    _counter += 1
    return f"{int(time.time())}{_counter}"


# ── Berichten ───────────────────────────────────────────────────────────────

HELP = (
    "👋 <b>Zo beheer je je zoekopdrachten</b>\n\n"
    "➕ <b>Toevoegen:</b> typ <code>/add</code> met daarachter je mobile.de-zoeklink.\n"
    "   Ik test hem en vraag je om te bevestigen.\n"
    "🎯 <b>Met voorwaarde:</b> zet een optie achter de link, bv.\n"
    "   <code>/add &lt;link&gt; s-line</code> of <code>/add &lt;link&gt; trekhaak</code>\n"
    "   → alleen auto's mét die optie worden gepusht.\n"
    "🚫 <b>Uitsluiten:</b> met 'zonder' of 'geen', bv.\n"
    "   <code>/add &lt;link&gt; geen sportback</code>\n"
    "   <code>/add &lt;link&gt; s-line zonder sportback</code>\n"
    "   <i>(Een link los plakken werkt ook, zodra de groeps-privacy van de bot uitstaat.)</i>\n\n"
    "📋 /links — bekijken en verwijderen\n"
    "📊 /status — draait alles goed?\n"
    "❓ /help — dit bericht"
)


def _credit_regel(aantal: int) -> str:
    return f"{core.estimate_monthly_credits(aantal):,}".replace(",", ".")


def toon_links(chat_id):
    conn = core.init_db()
    try:
        rijen = core.get_searches(conn, only_active=False)
    finally:
        conn.close()

    if not rijen:
        stuur(chat_id, "📭 Je hebt nog geen zoekopdrachten.\n\n"
                       "Plak een mobile.de-zoeklink om er een toe te voegen.")
        return

    regels = [f"📋 <b>Je zoekopdrachten ({len(rijen)})</b>\n"]
    knoppen = []
    for i, r in enumerate(rijen, 1):
        naam = html.escape(r["label"] or "zoekopdracht")
        regels.append(f"<b>{i}.</b> <a href=\"{html.escape(r['url'])}\">{naam}</a>")
        if r.get("require_feature"):
            regels.append(f"    🎯 alleen mét {html.escape(core.requirement_label('feature', r['require_feature']))}")
        elif r.get("require_text"):
            regels.append(f"    🎯 alleen met \"{html.escape(r['require_text'])}\" in de advertentie")
        if r.get("exclude_feature"):
            regels.append(f"    🚫 zonder {html.escape(core.requirement_label('feature', r['exclude_feature']))}")
        elif r.get("exclude_text"):
            regels.append(f"    🚫 zonder \"{html.escape(r['exclude_text'])}\" in de advertentie")
        knoppen.append([{"text": f"🗑 {i}. {naam[:28]}", "callback_data": f"vraagdel:{r['id']}"}])

    regels.append(f"\n💳 Samen ± <b>{_credit_regel(len(rijen))}</b> credits/maand")
    stuur(chat_id, "\n".join(regels), knoppen)


def toon_status(chat_id):
    conn = core.init_db()
    try:
        n = len(core.get_searches(conn))
        totaal = conn.execute("SELECT count(*) FROM listings").fetchone()[0]
        vandaag = conn.execute(
            "SELECT count(*) FROM listings WHERE first_seen > date('now')").fetchone()[0]
    finally:
        conn.close()

    stuur(chat_id,
          "📊 <b>Status</b>\n\n"
          f"✅ Bot draait\n"
          f"🔍 {n} zoekopdracht{'en' if n != 1 else ''}\n"
          f"🚗 {totaal} auto's in database ({vandaag} vandaag)\n"
          f"⏱ Elke {core.POLL_INTERVAL_MINUTES} min "
          f"({core.ACTIVE_START_HOUR:02d}:00–{core.ACTIVE_END_HOUR:02d}:00), "
          f"'s nachts elke 4 uur\n"
          f"💳 ± {_credit_regel(n)} credits/maand")


def _parse_voorwaarden(tekst: str):
    """Splits '/add <link> s-line zonder sportback' in (vereist, uitgesloten).

    Uitsluit-woorden: 'zonder', 'geen', 'niet', of een '-' voor de term.
    Voorbeelden: "s-line" -> ("s-line", "") · "geen sportback" -> ("", "sportback")
    · "s-line zonder sportback" -> ("s-line", "sportback")
    """
    t = (tekst or "").strip()
    if not t:
        return "", ""
    m = re.search(r"\b(?:zonder|geen|niet)\b", t, re.I)
    if m:
        return t[:m.start()].strip(" ,;-—"), t[m.end():].strip(" ,;-—")
    if t.startswith("-"):
        return "", t[1:].strip()
    return t, ""


def _resolve(deel: str):
    """Voorwaarde-tekst -> (feature, text) voor opslag."""
    soort, waarde = core.resolve_requirement(deel)
    if soort == "sportpakket":
        return "sportpakket", ""
    if soort == "feature":
        return waarde, ""
    if soort == "text":
        return "", waarde
    return "", ""


def verwerk_link(chat_id, url, msg_id, voorwaarde_tekst=""):
    stuur(chat_id, "🔎 Even checken…", reply_to=msg_id)

    # "s-line zonder sportback" -> vereiste + uitsluiting
    req_deel, exc_deel = _parse_voorwaarden(voorwaarde_tekst)
    req_feature, req_text = _resolve(req_deel)
    exc_feature, exc_text = _resolve(exc_deel)
    try:
        r = core.check_search_url(url)
    except Exception as exc:
        log.error("check_search_url faalde: %s", exc)
        stuur(chat_id, "⚠️ Kon de link niet testen. Probeer het zo nog eens.")
        return

    if not r["ok"]:
        stuur(chat_id, f"❌ <b>Deze link werkt niet</b>\n{html.escape(r['fout'])}")
        return

    conn = core.init_db()
    try:
        bestaand = core.get_searches(conn, only_active=False)
    finally:
        conn.close()
    if any(s["url"] == r["url"] for s in bestaand):
        stuur(chat_id, "ℹ️ Deze zoekopdracht heb je al staan. Bekijk ze met /links")
        return

    nieuw_aantal = len(bestaand) + 1
    regels = [
        f"🔍 <b>{html.escape(r['omschrijving'])}</b>\n",
        f"✅ {r['count']} auto's gevonden op 1e pagina",
    ]
    if r["nieuwste"]:
        regels.append(f"🕐 Nieuwste staat er {r['nieuwste']} op")
    if r["sort_aangepast"]:
        regels.append("🔧 Stond niet op <i>nieuwste eerst</i> — gecorrigeerd")
    if r["sortering_ok"] is False:
        regels.append("⚠️ Sortering klopt nog steeds niet — je mist mogelijk auto's")
    if req_feature:
        regels.append(f"🎯 Alleen tonen mét: <b>{html.escape(core.requirement_label('feature', req_feature))}</b>")
    elif req_text:
        regels.append(f"🎯 Alleen tonen als de advertentie <b>\"{html.escape(req_text)}\"</b> bevat")
    if exc_feature:
        regels.append(f"🚫 Niet tonen mét: <b>{html.escape(core.requirement_label('feature', exc_feature))}</b>")
    elif exc_text:
        regels.append(f"🚫 Niet tonen als de advertentie <b>\"{html.escape(exc_text)}\"</b> bevat")
    regels.append(
        f"\n💳 Wordt zoekopdracht <b>{nieuw_aantal}</b> → "
        f"± <b>{_credit_regel(nieuw_aantal)}</b> credits/maand "
        f"<i>(nu {_credit_regel(len(bestaand))})</i>"
    )

    t = _token()
    _pending[t] = {"url": r["url"], "require_feature": req_feature, "require_text": req_text,
                   "exclude_feature": exc_feature, "exclude_text": exc_text}
    stuur(chat_id, "\n".join(regels), [[
        {"text": "✅ Toevoegen", "callback_data": f"add:{t}"},
        {"text": "❌ Annuleren", "callback_data": f"nee:{t}"},
    ]])


# ── Afhandeling ─────────────────────────────────────────────────────────────

def mag(user_id) -> bool:
    return OWNER_ID and str(user_id) == OWNER_ID


def verwerk_bericht(msg):
    chat_id = msg.get("chat", {}).get("id")
    user_id = (msg.get("from") or {}).get("id")
    tekst = (msg.get("text") or "").strip()
    if not chat_id or not tekst:
        return

    if not mag(user_id):
        if tekst.startswith("/") or MOBILE_LINK.search(tekst):
            stuur(chat_id, "🔒 Sorry, alleen de eigenaar kan zoekopdrachten wijzigen.")
        return

    cmd = tekst.split()[0].lower().split("@")[0]
    if cmd in ("/help", "/start"):
        stuur(chat_id, HELP)
    elif cmd == "/links":
        toon_links(chat_id)
    elif cmd == "/status":
        toon_status(chat_id)
    else:
        m = MOBILE_LINK.search(tekst)
        if m:
            # Alles ná de link is de voorwaarde: "/add <link> s-line"
            na_link = tekst[m.end():].strip().lstrip("-—,;: ")
            verwerk_link(chat_id, m.group(0), msg.get("message_id"), na_link)
        elif cmd == "/add":
            stuur(chat_id, "➕ Zet de link er direct achter, bijvoorbeeld:\n"
                           "<code>/add https://suchen.mobile.de/fahrzeuge/search.html?...</code>")
        elif tekst.startswith("/"):
            stuur(chat_id, "🤔 Dat commando ken ik niet. Typ /help voor de mogelijkheden.")


def verwerk_knop(cb):
    cb_id = cb.get("id")
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    data = cb.get("data") or ""
    user_id = (cb.get("from") or {}).get("id")

    if not mag(user_id):
        api("answerCallbackQuery", callback_query_id=cb_id,
            text="Alleen de eigenaar kan dit wijzigen", show_alert=True)
        return
    api("answerCallbackQuery", callback_query_id=cb_id)

    actie, _, arg = data.partition(":")

    if actie == "add":
        p = _pending.pop(arg, None)
        if not p:
            bewerk(chat_id, msg_id, "⌛ Deze vraag is verlopen. Plak de link opnieuw.")
            return
        conn = core.init_db()
        try:
            r = core.check_search_url(p["url"])     # verse omschrijving
            naam = r["omschrijving"] if r["ok"] else "mobile.de zoekopdracht"
            nid = core.add_search(conn, p["url"], naam, str(user_id),
                                  require_feature=p.get("require_feature", ""),
                                  require_text=p.get("require_text", ""),
                                  exclude_feature=p.get("exclude_feature", ""),
                                  exclude_text=p.get("exclude_text", ""))
            aantal = len(core.get_searches(conn))
        finally:
            conn.close()
        if nid == -1:
            bewerk(chat_id, msg_id, "ℹ️ Deze zoekopdracht stond er al in.")
            return
        voorwaarde = ""
        if p.get("require_feature"):
            voorwaarde += f"\n🎯 Alleen mét {html.escape(core.requirement_label('feature', p['require_feature']))}"
        elif p.get("require_text"):
            voorwaarde += f"\n🎯 Alleen met \"{html.escape(p['require_text'])}\" in de advertentie"
        if p.get("exclude_feature"):
            voorwaarde += f"\n🚫 Zonder {html.escape(core.requirement_label('feature', p['exclude_feature']))}"
        elif p.get("exclude_text"):
            voorwaarde += f"\n🚫 Zonder \"{html.escape(p['exclude_text'])}\" in de advertentie"
        bewerk(chat_id, msg_id,
               f"✅ <b>Toegevoegd</b>\n{html.escape(naam)}{voorwaarde}\n\n"
               f"De eerste ronde slaat de auto's die er nu al staan stil op — "
               f"je krijgt alleen meldingen voor <b>nieuwe</b> auto's.\n"
               f"💳 Nu ± {_credit_regel(aantal)} credits/maand")

    elif actie == "nee":
        _pending.pop(arg, None)
        bewerk(chat_id, msg_id, "❌ Geannuleerd — er is niets gewijzigd.")

    elif actie == "vraagdel":
        conn = core.init_db()
        try:
            rij = next((s for s in core.get_searches(conn, only_active=False)
                        if str(s["id"]) == arg), None)
        finally:
            conn.close()
        if not rij:
            bewerk(chat_id, msg_id, "ℹ️ Die zoekopdracht bestaat niet meer.")
            return
        bewerk(chat_id, msg_id,
               f"🗑 <b>Verwijderen?</b>\n{html.escape(rij['label'])}\n\n"
               f"Je krijgt dan geen meldingen meer voor deze zoekopdracht.",
               [[{"text": "✅ Ja, verwijderen", "callback_data": f"del:{arg}"},
                 {"text": "↩️ Nee, laat staan", "callback_data": "terug:"}]])

    elif actie == "del":
        conn = core.init_db()
        try:
            gelukt = core.remove_search(conn, int(arg))
            aantal = len(core.get_searches(conn))
        finally:
            conn.close()
        bewerk(chat_id, msg_id,
               (f"🗑 <b>Verwijderd</b>\n💳 Nu ± {_credit_regel(aantal)} credits/maand"
                if gelukt else "ℹ️ Die zoekopdracht bestond niet meer."))

    elif actie == "terug":
        bewerk(chat_id, msg_id, "↩️ Oké, laten staan. Bekijk je lijst met /links")


# ── Hoofdlus ────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN ontbreekt"); sys.exit(1)
    if not OWNER_ID:
        log.error("TELEGRAM_OWNER_ID ontbreekt — zonder eigenaar mag niemand wijzigen")
        sys.exit(1)

    core.init_db().close()          # zorgt dat de tabellen bestaan
    log.info("Telegram-bot gestart (eigenaar: %s)", OWNER_ID)

    offset = None
    while True:
        try:
            t0 = time.time()
            r = requests.get(f"{API}/getUpdates", timeout=40, params={
                "timeout": 30,
                "offset": offset,
                "allowed_updates": '["message","callback_query"]',
            }).json()
            if not r.get("ok"):
                log.warning("getUpdates niet ok: %s", str(r)[:200])
                time.sleep(5)
                continue
            ups = r.get("result", [])
            if ups:
                log.info("%d update(s) ontvangen na %.1fs wachten", len(ups), time.time() - t0)
            for up in ups:
                offset = up["update_id"] + 1
                try:
                    if "message" in up:
                        m = up["message"]
                        log.info("bericht van %s: %r", (m.get("from") or {}).get("id"),
                                 (m.get("text") or "")[:40])
                        t1 = time.time()
                        verwerk_bericht(m)
                        log.info("  afgehandeld in %.1fs", time.time() - t1)
                    elif "callback_query" in up:
                        log.info("knop: %s", up["callback_query"].get("data"))
                        verwerk_knop(up["callback_query"])
                except Exception:
                    log.error("Fout bij verwerken update:\n%s", traceback.format_exc())
        except requests.exceptions.Timeout:
            continue                       # normaal bij long polling
        except Exception:
            log.error("Fout in hoofdlus:\n%s", traceback.format_exc())
            time.sleep(10)


if __name__ == "__main__":
    main()
