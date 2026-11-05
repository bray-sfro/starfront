"""Discord notifications for hub-backed collaborations (plan 4.7).

A collaboration's owner may give it a Discord webhook, `discordWebhookUrl` in
its payload. That field is owner-only: `server/app.py` strips it from every
listing and read, and the stacking front door never reads a collaboration's
payload at all. Two events are posted to it, with display names only:

* `member_joined`: "Eddie joined M33 live stacking."
* `gold_published`: "Gold integration 3 published for Ha: 12.4 h from 4 contributors."

Delivery is a POST of `{"content": "..."}` on a background thread, with a
5 s timeout. A slow, failing or hanging webhook never reaches the caller: the
join or the push that caused the message returns as it would have anyway.

De-duplication: before a post is made, its `(project_id, event, key)` is
claimed with INSERT OR IGNORE into `stack_notified`. Only the call that wins
the claim posts, so a retried push or join never posts twice. The claim is
made before the post, so a webhook that fails is not retried either: a missed
message is preferred to a repeated one.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

#: Seconds before a webhook is given up on.
TIMEOUT = 5.0

_WEBHOOK = re.compile(r"https://discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+")
_USER_AGENT = "Starfront-notify (https://starfront.space, 1)"
_threads: list[threading.Thread] = []
_lock = threading.Lock()


def webhook_ok(url: str) -> bool:
    """True for blank (no webhook) or a Discord webhook URL, else False."""
    return not url or _WEBHOOK.fullmatch(url) is not None


def webhook_of(project: dict[str, Any] | None) -> str:
    """The webhook a collaboration record (`store.project`) posts to, or ''."""
    if not project:
        return ""
    value = str((project.get("payload") or {}).get("discordWebhookUrl") or "")
    return value if webhook_ok(value) else ""


def _post(url: str, content: str) -> None:
    """One POST of the message, with the timeout. Raises on any failure."""
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        response.read()


def _deliver(url: str, content: str) -> None:
    try:
        _post(url, content)
    except Exception as exc:  # the webhook URL is a credential: log the kind only
        log.warning("Discord notification not delivered (%s)", type(exc).__name__)


def _send(url: str, content: str) -> None:
    thread = threading.Thread(target=_deliver, args=(url, content),
                              name="starfront-notify", daemon=True)
    with _lock:
        _threads[:] = [t for t in _threads if t.is_alive()]
        _threads.append(thread)
    thread.start()


def drain(timeout: float = 10.0) -> None:
    """Wait for the posts in flight. For the checks, and a clean shutdown."""
    with _lock:
        threads = list(_threads)
    for thread in threads:
        thread.join(timeout)


def _claim(store: Any, project_id: str, event: str, key: str) -> bool:
    """True only for the first caller to claim this event; that caller posts."""
    cursor = store._run(
        "INSERT OR IGNORE INTO stack_notified (project_id, event, key, at) VALUES (?, ?, ?, ?)",
        project_id, event, key, time.time())
    return cursor.rowcount == 1


def notify_member_joined(store: Any, project: dict[str, Any] | None, display_name: str,
                         *, member_id: str = "") -> None:
    """Post that somebody joined a collaboration's live stacking, once per member.

    `project` is the collaboration record (`store.project(...)`), whose payload
    holds the webhook; `display_name` is the name the member joined under.
    `member_id` is the member's id, the de-duplication key; it defaults to the
    display name, folded. Never raises.
    """
    try:
        url = webhook_of(project)
        if not url:
            return
        if not _claim(store, project["id"], "member_joined",
                      member_id or display_name.casefold()):
            return
        _send(url, f"{display_name} joined {project['name']} live stacking.")
    except Exception:
        log.exception("member_joined notification failed")


def notify_gold_published(store: Any, project: dict[str, Any] | None, stream: str,
                          gold_seq: int, hours: float, contributors: int) -> None:
    """Post that a gold integration was published, once per (project, stream, gold_seq).

    `project` is the collaboration record, as for `notify_member_joined`;
    `stream` is the filter (the stream key), `gold_seq` the gold integration's
    sequence number, `hours` the integration time in hours, `contributors` the
    number of people who contributed to it. T407 calls this from the push
    ingestion. Never raises.
    """
    try:
        url = webhook_of(project)
        if not url:
            return
        if not _claim(store, project["id"], "gold_published", f"{stream}:{int(gold_seq)}"):
            return
        people = "contributor" if contributors == 1 else "contributors"
        _send(url, f"Gold integration {int(gold_seq)} published for {stream}: "
                   f"{float(hours):.1f} h from {int(contributors)} {people}.")
    except Exception:
        log.exception("gold_published notification failed")
