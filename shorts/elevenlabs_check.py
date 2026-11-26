"""Check ELEVENLABS_API_KEY without spending credits; report via Telegram."""
import os
import pathlib
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from notify import notify  # noqa: E402

key = os.environ.get("ELEVENLABS_API_KEY")
if not key:
    notify("❌ ElevenLabs-Test: Secret ELEVENLABS_API_KEY fehlt noch auf GitHub.")
    sys.exit(1)
h = {"xi-api-key": key}
sub = requests.get("https://api.elevenlabs.io/v1/user/subscription", headers=h, timeout=30)
if sub.ok:
    s = sub.json()
    left = s.get("character_limit", 0) - s.get("character_count", 0)
    msg = f"✅ ElevenLabs verbunden. Tarif: {s.get('tier')}, verbleibende Credits diesen Monat: {left}"
else:
    v = requests.get(f"https://api.elevenlabs.io/v1/voices/xB7ZTAdAjd7cI20IXiAL", headers=h, timeout=30)
    if v.status_code in (200, 404):
        msg = "✅ ElevenLabs-Schlüssel gültig (Guthaben nicht lesbar – Schlüssel hat keine 'User'-Leserechte, ist ok)."
    else:
        msg = f"❌ ElevenLabs-Schlüssel abgelehnt ({sub.status_code}/{v.status_code}). Bitte neuen Schlüssel mit 'Text to Speech'-Recht anlegen."
print(msg)
notify(msg)
sys.exit(0 if msg.startswith("✅") else 1)
