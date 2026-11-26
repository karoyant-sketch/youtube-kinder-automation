"""Check POSTEY_MCP_KEY: connect to Postey MCP and list accounts; report via Telegram."""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from notify import notify  # noqa: E402
import pipeline as P  # noqa: E402

key = os.environ.get("POSTEY_MCP_KEY")
if not key:
    notify("❌ Postey-Test: Secret POSTEY_MCP_KEY fehlt noch auf GitHub.")
    sys.exit(1)
try:
    data = P.Postey(key).call("get_accounts", {})
    accs = data.get("accounts", [])
    lines = [f"{a['account_id']}: " + ", ".join(f"{p['platform']} {p.get('handle')}" for p in a.get("platforms", []))
             for a in accs]
    print("\n".join(lines))
    notify("✅ Postey-Schlüssel funktioniert.\nVerbundene Konten:\n" + "\n".join(lines))
except Exception as e:  # noqa: BLE001
    print("ERROR", e)
    notify(f"❌ Postey-Test fehlgeschlagen: {str(e)[:300]}")
    sys.exit(1)
