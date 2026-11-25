"""Send a WhatsApp message to the owner via CallMeBot — only when action is needed.

Secrets (GitHub): WHATSAPP_PHONE (+49...), CALLMEBOT_APIKEY
Usage: python notify.py "message text"   or   from notify import notify
"""
import os
import sys
import urllib.parse

import requests


def notify(text: str) -> bool:
    phone, key = os.environ.get("WHATSAPP_PHONE"), os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not key:
        print("WhatsApp not configured, message was:\n" + text)
        return False
    url = ("https://api.callmebot.com/whatsapp.php?phone=" + urllib.parse.quote(phone)
           + "&text=" + urllib.parse.quote(text[:1500]) + "&apikey=" + urllib.parse.quote(key))
    r = requests.get(url, timeout=30)
    ok = r.ok and "error" not in r.text.lower()
    print("WhatsApp sent" if ok else f"WhatsApp failed: {r.status_code}")
    return ok


if __name__ == "__main__":
    notify(" ".join(sys.argv[1:]) or "Test: Open World Chaos Bot ist verbunden ✅")
