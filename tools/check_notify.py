"""Notifications: what goes out, to whom, in what shape.

    python tools/check_notify.py

  * A Discord webhook gets an embed - title, description, colour, the
    telescope's name in the foot - not a bare `content`.
  * Any other webhook still gets the message under the configured key.
  * The rate limit holds back failures, not a night's activity.
  * Each event answers to its own switch.

Nothing is sent: the HTTP call is captured.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol import notify                                  # noqa: E402
from astrocontrol.config import Config                           # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


posted = []


class _Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(request, timeout=None):
    posted.append((request.full_url, json.loads(request.data.decode("utf-8"))))
    return _Response()


notify.urllib.request.urlopen = fake_urlopen


def fresh(**settings):
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    config.update("notify", {"enabled": True, "minSecondsBetween": 60.0, **settings})
    notifier = notify.Notifier(config)
    notifier.origin = lambda: "Telescope 1"
    return notifier


def drain():
    time.sleep(0.3)                                  # the send is on a thread


DISCORD = "https://discord.com/api/webhooks/123/abc"

# -------------------------------------------------------------- Discord
posted.clear()
n = fresh(webhookUrl=DISCORD)
case("a Discord URL is recognised", notify.is_discord(DISCORD)
     and not notify.is_discord("https://example.org/hook"))
n.send("sequenceEnd", "Sequence finished", "40 frames over 5.2h")
drain()
case("a Discord webhook gets an embed", posted and "embeds" in posted[-1][1],
     str(posted[-1:]))
embed = posted[-1][1]["embeds"][0] if posted else {}
case("...with the title, the detail and a green bar for a good night",
     embed.get("title") == "Sequence finished" and "40 frames" in embed.get("description", "")
     and embed.get("color") == 0x4BB87A, str(embed))
case("...and the telescope's name in the foot",
     (embed.get("footer") or {}).get("text") == "Telescope 1", str(embed.get("footer")))
case("...posted as Starfront", posted[-1][1].get("username") == "Starfront")

posted.clear()
n.send("failure", "Sequence failed: the mount would not slew", "")
drain()
case("a failure is red", posted and posted[-1][1]["embeds"][0]["color"] == 0xD9503D)

# -------------------------------------------------------- other webhooks
posted.clear()
n = fresh(webhookUrl="https://ntfy.sh/starfront", messageField="message")
n.send("sequenceEnd", "Sequence finished", "done")
drain()
case("any other webhook gets the message under its key",
     posted and posted[-1][1].get("message", "").startswith("Sequence finished")
     and "embeds" not in posted[-1][1], str(posted[-1:]))

# ------------------------------------------------------------ throttling
posted.clear()
n = fresh(webhookUrl=DISCORD)
first = n.send("failure", "Failed once")
second = n.send("failure", "Failed again")
case("two failures inside the gap: the second is held back", first and not second)
third = n.send("activity", "Starting M31", "3×L 300s")
fourth = n.send("activity", "Meridian flip on M31")
case("activity is never held back by the gap", third and fourth)
drain()
case("...so three messages went out", len(posted) == 3, str(len(posted)))

# ----------------------------------------------------------- the switches
posted.clear()
n = fresh(webhookUrl=DISCORD, onActivity=False, onCalibration=True)
case("activity can be switched off", n.send("activity", "Starting M31") is False)
case("while calibration stays on", n.send("calibration", "Calibration finished") is True)
n = fresh(webhookUrl=DISCORD, enabled=False)
case("nothing goes when notifications are off",
     n.send("failure", "Failed") is False)
case("except the test message, which somebody asked for",
     n.send("test", "Test") is True)
drain()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
