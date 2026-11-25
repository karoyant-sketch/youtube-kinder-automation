"""Send a phone notification to the owner — only when action is needed.

WhatsApp Cloud API (Meta, free): secrets WA_TOKEN, WA_PHONE_ID, WHATSAPP_PHONE.
SMS via seven.io: secrets SEVEN_API_KEY, SMS_PHONE (+49...).
ntfy (simplest): secret NTFY_TOPIC — install the ntfy app and subscribe to that topic.

Telegram (preferred): secret TELEGRAM_BOT_TOKEN, optional TELEGRAM_CHAT_ID.
  Without a chat id, the chat of whoever last wrote to the bot is used (send /start once).
WhatsApp via CallMeBot (fallback): secrets WHATSAPP_PHONE, CALLMEBOT_APIKEY.
Usage: python notify.py "message text"   or   from notify import notify
"""
import os
import sys
import urllib.parse

import requests


def _telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False
    api = f"https://api.telegram.org/bot{token}"
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat:
        updates = requests.get(f"{api}/getUpdates", timeout=30).json().get("result", [])
        chats = [u["message"]["chat"]["id"] for u in updates if "message" in u]
        if not chats:
            print("Telegram: no chat found – open your bot in Telegram and send /start")
            return False
        chat = chats[-1]
    r = requests.post(f"{api}/sendMessage", json={"chat_id": chat, "text": text[:4000],
                                                  "disable_web_page_preview": True}, timeout=30)
    print("Telegram sent" if r.ok else f"Telegram failed: {r.status_code}")
    return r.ok


def _whatsapp(text: str) -> bool:
    phone, key = os.environ.get("WHATSAPP_PHONE"), os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not key:
        return False
    url = ("https://api.callmebot.com/whatsapp.php?phone=" + urllib.parse.quote(phone)
           + "&text=" + urllib.parse.quote(text[:1500]) + "&apikey=" + urllib.parse.quote(key))
    r = requests.get(url, timeout=30)
    return r.ok and "error" not in r.text.lower()


def _ntfy(text: str) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    r = requests.post(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"),
                      headers={"Title": "Open World Chaos", "Priority": "high"}, timeout=30)
    print("ntfy sent" if r.ok else f"ntfy failed: {r.status_code}")
    return r.ok


def _sms(text: str) -> bool:
    key, phone = os.environ.get("SEVEN_API_KEY"), os.environ.get("SMS_PHONE")
    if not key or not phone:
        return False
    r = requests.post("https://gateway.seven.io/api/sms", headers={"X-Api-Key": key},
                      data={"to": phone, "text": text[:450], "from": "OWC"}, timeout=30)
    ok = r.ok and r.text.strip().startswith(("100", "{"))
    print("SMS sent" if ok else f"SMS failed: {r.status_code} {r.text[:80]}")
    return ok


def _whatsapp_meta(text: str) -> bool:
    """Official WhatsApp Cloud API. Business-initiated messages need an approved template:
    WA_TEMPLATE (default 'action_needed', language WA_TEMPLATE_LANG default 'de') with one body
    variable {{1}}. Falls back to plain text (works within 24h after you wrote to the number)."""
    token, phone_id, to = (os.environ.get(k) for k in ("WA_TOKEN", "WA_PHONE_ID", "WHATSAPP_PHONE"))
    if not token or not phone_id or not to:
        return False
    url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
    h = {"Authorization": f"Bearer {token}"}
    to = to.lstrip("+")
    tpl = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": {
        "name": os.environ.get("WA_TEMPLATE", "action_needed"),
        "language": {"code": os.environ.get("WA_TEMPLATE_LANG", "de")},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": text[:1000].replace("\n", " | ")}]}]}}
    r = requests.post(url, headers=h, json=tpl, timeout=30)
    if not r.ok:
        r = requests.post(url, headers=h, json={"messaging_product": "whatsapp", "to": to, "type": "text",
                                                "text": {"body": text[:4000]}}, timeout=30)
    print("WhatsApp sent" if r.ok else f"WhatsApp failed: {r.status_code} {r.text[:120]}")
    return r.ok


def notify(text: str) -> bool:
    if _whatsapp_meta(text) or _sms(text) or _ntfy(text) or _telegram(text) or _whatsapp(text):
        return True
    print("No notification channel configured, message was:\n" + text)
    return False


if __name__ == "__main__":
    ok = notify(" ".join(sys.argv[1:]) or "Test: Open World Chaos Bot ist verbunden ✅")
    sys.exit(0 if ok else 1)
