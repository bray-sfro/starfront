"""Telling somebody who is not in the room.

A rig at a remote site fails silently by default.  The sequence stops at one in
the morning, the log records exactly why, and nobody reads it until breakfast —
by which time the night is gone and so is the weather.  This is the difference
between losing an hour and losing a night.

Two ways out, both on the standard library:

  * **A webhook.**  One URL that JSON is POSTed to.  Discord, Slack, Telegram,
    Pushover, ntfy and anything self-hosted all accept one, so this is a webhook
    rather than a list of services that would need keeping up with.  The only
    thing that varies is which key the message goes under, and that is a
    setting.
  * **Mail**, for those who would rather have it in an inbox.

Everything here is best effort and off the calling thread.  A notification that
could delay a slew, or fail a night because a webhook was down, would be worse
than no notification at all.
"""

from __future__ import annotations

import json
import smtplib
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any

#: How long to give a webhook or an SMTP server before giving up on it. Short:
#: nothing waits on this, but a thread per message that hangs for a minute is
#: still a thread per message.
TIMEOUT = 10.0

#: Events, and the setting that decides whether each one is worth sending.
EVENTS = {
    "sequenceEnd": "onSequenceEnd",
    "failure": "onFailure",
    "recovery": "onRecovery",
    "giveUp": "onGiveUp",
    "safety": "onSafety",
    # What the telescope is doing: a night opening, a target starting, a
    # meridian flip, the observatory going to bed, the next night's time.
    "activity": "onActivity",
    # A calibration run finishing, and what it built.
    "calibration": "onCalibration",
    # Something critical on the warnings board: a cover shut over a running
    # sequence, frames that are black, a disk that is full.
    "warning": "onWarning",
    # Always sent: somebody pressed a button to ask for it.
    "test": None,
}

#: Events the rate limit applies to: the ones a rig can raise in a loop at
#: three in the morning. A night's activity is a handful of messages spaced
#: by the sky, and a target starting a minute after the night opened is not
#: a message to drop.
THROTTLED = {"failure", "recovery", "giveUp", "safety", "warning"}

#: A colour per event for services that show one - Discord's embed bar. Red
#: for what needs somebody, amber for what is being handled, green for a
#: night that ended well, blue for a telescope going about its business.
COLOURS = {
    "failure": 0xD9503D, "giveUp": 0xD9503D, "warning": 0xD9503D,
    "safety": 0xD9A03D, "recovery": 0xD9A03D,
    "sequenceEnd": 0x4BB87A, "calibration": 0x4BB87A,
    "activity": 0x5B8DD9, "test": 0x9AA8BD,
}


def is_discord(url: str) -> bool:
    """A Discord webhook, which takes a richer message than a bare `content`."""
    lowered = url.lower()
    return "discord.com/api/webhooks/" in lowered or "discordapp.com/api/webhooks/" in lowered


class Notifier:
    """Sends the handful of things worth waking up for."""

    def __init__(self, config: Any, log: Any = None) -> None:
        self.config = config
        self._log = log
        # Whose telescope this is, for the foot of every message: with three
        # rigs posting into one Discord channel, "Sequence finished" alone
        # says nothing. A callable, because the master can be renamed.
        self.origin: Any = None
        self._lock = threading.Lock()
        self._last_sent = 0.0
        self._sent = 0
        self._failed = 0
        self._last_error: str | None = None
        self._last_message = ""

    def settings(self) -> dict[str, Any]:
        return self.config.section("notify")

    # -- sending -----------------------------------------------------------
    def send(self, event: str, subject: str, body: str = "") -> bool:
        """Queue one notification. False when it was not wanted.

        Returns whether it was *started*, not whether it arrived: the send runs
        on its own thread, because a webhook that has gone away must not hold up
        a mount that is trying to park.
        """
        settings = self.settings()
        if not settings.get("enabled", False) and event != "test":
            return False

        key = EVENTS.get(event)
        if key is not None and not settings.get(key, True):
            return False

        # A rig failing in a loop at three in the morning should not empty a
        # phone battery. The test message ignores this, or pressing the button
        # twice would look broken.
        gap = float(settings.get("minSecondsBetween") or 0)
        with self._lock:
            if event in THROTTLED and gap and time.monotonic() - self._last_sent < gap:
                return False
            if event in THROTTLED:
                self._last_sent = time.monotonic()
            self._last_message = subject

        threading.Thread(target=self._deliver, args=(settings, event, subject, body),
                         daemon=True, name="notify").start()
        return True

    def _origin(self) -> str:
        try:
            return str(self.origin() if callable(self.origin) else self.origin or "")
        except Exception:                          # noqa: BLE001 - a name is not worth a fault
            return ""

    def _deliver(self, settings: dict[str, Any], event: str, subject: str,
                 body: str) -> None:
        text = f"{subject}\n\n{body}".strip() if body else subject
        problems: list[str] = []
        sent = False

        url = str(settings.get("webhookUrl") or "").strip()
        if url:
            try:
                if is_discord(url):
                    self._post_discord(url, event, subject, body, self._origin())
                else:
                    self._post(url, settings, text)
                sent = True
            except Exception as exc:              # noqa: BLE001 - never fatal
                problems.append(f"webhook: {exc}")

        if str(settings.get("smtpHost") or "").strip():
            try:
                self._mail(settings, subject, body or subject)
                sent = True
            except Exception as exc:              # noqa: BLE001 - never fatal
                problems.append(f"mail: {exc}")

        with self._lock:
            if sent:
                self._sent += 1
            if problems:
                self._failed += 1
                self._last_error = "; ".join(problems)
            elif sent:
                self._last_error = None

        if problems and self._log is not None:
            # Worth one line, because a notifier that has silently stopped
            # working is indistinguishable from a night where nothing happened.
            self._log(f"Could not send a notification — {'; '.join(problems)}", "warn")

    @staticmethod
    def _post(url: str, settings: dict[str, Any], text: str) -> None:
        field = str(settings.get("messageField") or "content").strip() or "content"
        payload = {
            field: text,
            # Sent alongside whatever the service wants, so a self-hosted
            # endpoint can use the structured form and a chat service can
            # ignore it.
            "source": "Starfront",
            "text": text,
        }
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "Starfront"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")

    @staticmethod
    def _post_discord(url: str, event: str, subject: str, body: str,
                      origin: str) -> None:
        """A Discord message the way Discord shows one: an embed.

        A title, the detail underneath, a coloured bar saying at a glance
        whether it is good news, and the telescope's name in the foot so a
        channel shared by a club reads. Discord limits a description to four
        thousand characters and a title to two hundred and fifty-six.
        """
        embed: dict[str, Any] = {
            "title": subject[:256],
            "color": COLOURS.get(event, 0x9AA8BD),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        }
        if body:
            embed["description"] = body[:4000]
        if origin:
            embed["footer"] = {"text": origin[:2048]}
        payload = {"username": "Starfront", "embeds": [embed]}
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "Starfront"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")

    @staticmethod
    def _mail(settings: dict[str, Any], subject: str, body: str) -> None:
        sender = str(settings.get("smtpFrom") or settings.get("smtpUser") or "").strip()
        recipients = [part.strip() for part
                      in str(settings.get("smtpTo") or "").replace(";", ",").split(",")
                      if part.strip()]
        if not sender or not recipients:
            raise RuntimeError("no from or to address")

        message = EmailMessage()
        message["Subject"] = f"Starfront: {subject}"
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message.set_content(body)

        host = str(settings["smtpHost"]).strip()
        port = int(settings.get("smtpPort") or 587)
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as server:
            if settings.get("smtpStartTls", True):
                server.starttls()
            user = str(settings.get("smtpUser") or "").strip()
            if user:
                server.login(user, str(settings.get("smtpPassword") or ""))
            server.send_message(message)

    # -- reporting ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        settings = self.settings()
        with self._lock:
            return {
                "enabled": bool(settings.get("enabled", False)),
                # Whether there is anywhere for a message to go at all, which is
                # a different question from whether it is switched on.
                "configured": bool(str(settings.get("webhookUrl") or "").strip()
                                   or str(settings.get("smtpHost") or "").strip()),
                "discord": is_discord(str(settings.get("webhookUrl") or "")),
                "sent": self._sent,
                "failed": self._failed,
                "lastError": self._last_error,
                "lastMessage": self._last_message,
            }
