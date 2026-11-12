"""Paid plans: who has one, as the shop tells it.

A plan is a monthly subscription bought on the Starfront shop (Shopify,
billed by the Appstle app). This module is how the server learns of it and
what `Personal.entitlement()` reads. The whole design, and what has to be
set up by hand in the shop, is in server/deploy/BILLING-SUPPORT.md.

**Whose subscription it is.** The My imaging page links to the shop with a
*link code* for the signed-in person: their Discord id and a signature only
this server can make (`link_code`). The shop keeps it on the order line, and
every message about that subscription carries it back. A purchase with no
code, or one somebody typed by hand, is kept *unlinked*: paid, but nobody's,
until its buyer claims it with the order number and email, or an owner
links it.

**How the server hears.** Three doors, each checked before anything is
believed, all leading to the same `apply()`:

* `POST /api/v1/billing/appstle` - Appstle's own webhooks (HMAC, the
  Standard Webhooks scheme). The full story: created, paid, failed, paused,
  cancelled. Needs an Appstle plan that includes webhooks.
* `POST /api/v1/billing/shopify` - Shopify's `orders/paid` and
  `refunds/create` webhooks (HMAC). Every plan has them; they say paid and
  refunded, and silence means not paid.
* `POST /api/v1/billing/flow` - a Shopify Flow "Send HTTP request" step
  (Flow cannot sign, so a shared secret in a header).

And once a night `reconcile()` asks Appstle's API for every subscription and
corrects whatever a missed message left wrong.

**What a person gets** is worked out when asked, from dates, never from a
job that must run: paid up -> the plan; payment late -> the plan for
`graceDays` more; cancelled -> the plan to the end of what was paid; after
that uploads stop (402) and what they stored stays, read-only. Nothing is
ever deleted here: the owner's list says what is past `keepDays`.

Off unless `STARFRONT_BILLING=1`. Plans, prices and sizes are not in the
code: they are in `plans.json` beside the database (see
server/deploy/plans.example.json). Secrets come from the environment or
`billing.env` beside the database, never the repository:

| | |
|---|---|
| `STARFRONT_BILLING` | `1` to read plans at all (default off) |
| `STARFRONT_APPSTLE_WEBHOOK_SECRET` | the `whsec_...` signing secret of the Appstle webhook |
| `STARFRONT_SHOPIFY_WEBHOOK_SECRET` | the signing secret Shopify shows under Notifications > Webhooks |
| `STARFRONT_FLOW_SECRET` | a long random string, sent by the Flow step as `X-Starfront-Secret` |
| `STARFRONT_APPSTLE_API_KEY` | a read-only Appstle API key, for the nightly reconcile |
| `STARFRONT_BILLING_LINK_SECRET` | signs link codes (made and kept in `billing-link-secret.txt` if unset) |
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from server import auth
from server.support import Send, http_send, load_env_file

ENV_FILE = "billing.env"
PLANS_FILE = "plans.json"
LINK_SECRET_FILE = "billing-link-secret.txt"
APPSTLE_API = "https://subscription-admin.appstle.com/api/external/v2"
#: A signed message older or newer than this is refused (replays).
TOLERANCE_SECONDS = 5 * 60
DAY = 86400.0
_LINK = re.compile(r"sf1\.([A-Za-z0-9_-]{1,40})\.([0-9a-f]{16})")
#: Keys a line item names its product by, in any of the three doors' shapes.
_PRODUCT_KEYS = {"variantid", "variant_id", "sku", "productid", "product_id"}
_log = logging.getLogger("starfront.billing")

#: What a subscription's status at the shop means here.
_STATUS = {"active": "active", "paused": "paused", "cancelled": "cancelled",
           "canceled": "cancelled", "expired": "cancelled", "failed": "past_due"}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _tail(value: Any) -> str:
    """An id however it is written: 'gid://shopify/ProductVariant/42' and 42 are '42'."""
    return str(value if value is not None else "").strip().rsplit("/", 1)[-1]


def _when(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if 0 < value < 1e11 else None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _walk(node: Any) -> Iterator[tuple[str, Any]]:
    """Every key and value in a message, however deep. Text that is itself
    JSON (Appstle sends a contract's lines that way) is opened too."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key), value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, str) and node[:1] in "[{" and len(node) < 200_000:
        try:
            yield from _walk(json.loads(node))
        except ValueError:
            return


def verify_shopify(secret: str, body: bytes, header: str) -> bool:
    """Shopify's signature: base64 HMAC-SHA256 of the raw body."""
    if not secret or not header:
        return False
    want = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(want, header.strip())


def verify_standard(secret: str, body: bytes, msg_id: str, timestamp: str, header: str,
                    now: float) -> bool:
    """The Standard Webhooks signature Appstle uses: HMAC-SHA256 of
    `<id>.<timestamp>.<body>` with the base64 key inside `whsec_...`; the
    header lists one or more `v1,<signature>`."""
    if not (secret and msg_id and timestamp and header):
        return False
    try:
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
        sent = float(timestamp)
    except (ValueError, IndexError):
        return False
    if abs(now - sent) > TOLERANCE_SECONDS:
        return False
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    want = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(want, part.split(",", 1)[1])
               for part in header.split() if part.startswith("v1,"))


class Grant(BaseModel):
    user: str = Field(min_length=1, max_length=40)
    plan: str = Field(min_length=1, max_length=40)
    days: float = Field(default=31, gt=0, le=3660)
    note: str = Field(default="", max_length=200)


class Link(BaseModel):
    contract: str = Field(min_length=1, max_length=80)
    user: str = Field(max_length=40)


class Claim(BaseModel):
    order: str = Field(min_length=1, max_length=20)
    email: str = Field(min_length=5, max_length=254)


class Billing:
    def __init__(self, app: FastAPI, store: Any, who: Callable[[Request], dict[str, Any] | None],
                 data: Path, *, enabled: bool | None = None, config: dict[str, Any] | None = None,
                 secrets_: dict[str, str] | None = None, send: Send = http_send,
                 clock: Callable[[], float] = time.time) -> None:
        self.store, self.who, self.data, self.send, self.clock = store, who, Path(data), send, clock
        self.enabled = _flag("STARFRONT_BILLING") if enabled is None else enabled
        if config is None:
            config = self._read_config()
        self.plans: dict[str, dict[str, Any]] = {}
        for plan in config.get("plans") or []:
            if isinstance(plan, dict) and plan.get("key"):
                self.plans[str(plan["key"])] = plan
        self.grace = float(config.get("graceDays", 7)) * DAY
        self.period = float(config.get("periodDays", 31)) * DAY
        self.keep = float(config.get("keepDays", 90)) * DAY
        self.subscribe_url = str(config.get("subscribeUrl") or "")
        self.manage_url = str(config.get("manageUrl") or "")
        given = secrets_ or {}
        self._secret = {name: (given.get(name) if secrets_ is not None else auth.setting(env)) or ""
                        for name, env in (("appstle", "APPSTLE_WEBHOOK_SECRET"),
                                          ("shopify", "SHOPIFY_WEBHOOK_SECRET"), ("flow", "FLOW_SECRET"),
                                          ("api", "APPSTLE_API_KEY"), ("link", "BILLING_LINK_SECRET"))}
        self._lock = threading.Lock()
        self._routes(app)

    def _read_config(self) -> dict[str, Any]:
        path = Path(auth.setting("PLANS_FILE") or self.data / PLANS_FILE)
        try:
            config = json.loads(path.read_text("utf-8"))
        except OSError:
            return {}
        except ValueError:
            _log.error("%s is not valid JSON; no plans are on offer", path)
            return {}
        return config if isinstance(config, dict) else {}

    # -- whose it is ---------------------------------------------------------------
    def _link_secret(self) -> bytes:
        if not self._secret["link"]:
            path = self.data / LINK_SECRET_FILE
            try:
                self._secret["link"] = path.read_text("utf-8").strip()
            except OSError:
                self._secret["link"] = secrets.token_hex(32)
                self.data.mkdir(parents=True, exist_ok=True)
                path.write_text(self._secret["link"] + "\n", "utf-8")
                os.chmod(path, 0o600)
        return self._secret["link"].encode()

    def link_code(self, user_id: str) -> str:
        """What the shop is handed to say whose purchase this is."""
        sig = hmac.new(self._link_secret(), str(user_id).encode(), hashlib.sha256).hexdigest()[:16]
        return f"sf1.{user_id}.{sig}"

    def linked_user(self, message: Any) -> str:
        """The person a message's link code names, '' if it has none that
        this server signed."""
        for user_id, sig in _LINK.findall(json.dumps(message)):
            if hmac.compare_digest(self.link_code(user_id).rsplit(".", 1)[1], sig):
                return user_id
        return ""

    def plan_of(self, message: Any) -> str:
        """Which plan a message's line items are, '' if none of ours."""
        seen = {_tail(value) for key, value in _walk(message)
                if key.lower() in _PRODUCT_KEYS and isinstance(value, (str, int))}
        for key, plan in self.plans.items():
            ours = {str(x) for name in ("variants", "skus", "products") for x in plan.get(name) or []}
            if ours & seen:
                return key
        return ""

    # -- one message, whatever door it came through ------------------------------------
    def apply(self, kind: str, contract: str, *, at: float | None = None, plan: str = "",
              user: str = "", status: str = "", through: float | None = None,
              customer: str = "", email: str = "", order_name: str = "", order_id: str = "",
              source: str = "") -> dict[str, Any]:
        """Change one subscription by one message. `kind` is what happened:
        paid, failed, cancelled, paused, refunded, or sync (the shop's
        present view of it: `status`, and `through` if it is paid up)."""
        at = self.clock() if at is None else at
        with self._lock:
            row = self.store.subscription(contract)
            if row is None and not contract.startswith("order:"):
                # Shopify's order message came first and could not know the
                # subscription's id: this is that purchase, under its real name.
                row = self._provisional(customer, email, plan)
                if row is not None:
                    self.store.rename_subscription(row["contract"], contract)
            row = row or {"status": "", "paid_through": 0.0, "failed_since": 0.0, "user": "",
                          "payload": {}}
            new: dict[str, Any] = {}
            status = _STATUS.get(status.lower(), "")
            if kind == "paid":
                new.update(status="active", failed_since=0.0, ended=0.0,
                           paid_through=max(float(row["paid_through"]), through or at + self.period))
            elif kind == "failed":
                new.update(status="past_due", failed_since=float(row["failed_since"]) or at)
            elif kind in ("cancelled", "paused"):
                new.update(status=kind, ended=at)
            elif kind == "refunded":
                new.update(status="refunded", ended=at, paid_through=min(float(row["paid_through"]), at))
            elif kind == "sync" and status:
                if row["status"] != "refunded":
                    new["status"] = status
                if status == "active" and through and row["status"] not in ("past_due", "refunded"):
                    new.update(paid_through=max(float(row["paid_through"]), through), ended=0.0)
                elif status in ("cancelled", "paused") and row["status"] not in (status, "refunded"):
                    new["ended"] = at
            for key, value in (("plan", plan), ("customer", customer), ("email", email.lower()),
                               ("order_name", order_name), ("source", source)):
                if value and (key != "source" or not row.get("source")):
                    new[key] = value
            if user and not row["user"]:
                new["user"] = user
            if order_id:
                payload = dict(row.get("payload") or {})
                payload["orders"] = ([o for o in payload.get("orders") or [] if o != order_id] + [order_id])[-12:]
                new["payload"] = payload
            return self.store.put_subscription(contract, **new)

    def _provisional(self, customer: str, email: str, plan: str) -> dict[str, Any] | None:
        for row in self.store.subscriptions():
            if row["contract"].startswith("order:") and (not plan or not row["plan"] or row["plan"] == plan) and (
                    (customer and row["customer"] == customer) or (email and row["email"] == email.lower())):
                return row
        return None

    def _for_order(self, customer: str, email: str, plan: str) -> dict[str, Any] | None:
        """The subscription a paid order renews: this customer's, of this plan."""
        rows = [row for row in self.store.subscriptions() if row["plan"] == plan and (
            (customer and row["customer"] == customer) or (email and row["email"] == email.lower()))]
        rows.sort(key=lambda r: (r["status"] in ("cancelled", "refunded"), -float(r["updated"])))
        return rows[0] if rows else None

    def take_appstle(self, message: dict[str, Any]) -> str:
        kind = str(message.get("type") or "")
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        if kind in ("subscription.billing-success", "subscription.billing-failure"):
            contract = _tail(data.get("contractId"))
            # Every subscription in the shop bills through here, the piers
            # too: only the ones already known as a plan are ours.
            if not contract or self.store.subscription(contract) is None:
                return "ignored: not a plan"
            at = _when(data.get("billingDate")) or self.clock()
            self.apply("paid" if kind.endswith("success") else "failed", contract, at=at,
                       order_name=str(data.get("orderName") or ""), order_id=_tail(data.get("orderId")),
                       source="appstle")
            return kind
        if not kind.startswith("subscription.") or kind.endswith(("upcoming-order-notification", "billing-skipped")):
            return "ignored"
        contract, plan = _tail(data.get("id")), self.plan_of(data)
        if not contract or not (plan or self.store.subscription(contract)):
            return "ignored: not a plan"
        customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
        origin = data.get("originOrder") if isinstance(data.get("originOrder"), dict) else {}
        # A new subscription was paid for at checkout, up to its next billing
        # date. Later changes say what state it is in, never that it was paid.
        created = kind == "subscription.created" and str(data.get("status") or "").lower() == "active"
        self.apply("paid" if created else "sync", contract, plan=plan, user=self.linked_user(data),
                   status=str(data.get("status") or ""),
                   through=_when(data.get("nextBillingDate")) if created else None,
                   customer=_tail(customer.get("id")), email=str(customer.get("email") or ""),
                   order_name=str(origin.get("name") or ""), order_id=_tail(origin.get("id")),
                   source="appstle")
        return kind

    def take_shopify(self, topic: str, order: dict[str, Any]) -> str:
        if topic == "refunds/create":
            order_id = _tail(order.get("order_id"))
            if not self.plan_of(order.get("refund_line_items") or []):
                return "ignored: not a plan"
            for row in self.store.subscriptions():
                if order_id and order_id in (row["payload"].get("orders") or []):
                    self.apply("refunded", row["contract"])
                    return "refunded"
            return "ignored: no such order"
        if topic != "orders/paid":
            return "ignored"
        plan = self.plan_of(order.get("line_items") or [])
        if not plan:
            return "ignored: not a plan"
        buyer = order.get("customer") if isinstance(order.get("customer"), dict) else {}
        customer, email = _tail(buyer.get("id")), str(order.get("email") or buyer.get("email") or "")
        found = self._for_order(customer, email, plan)
        contract = found["contract"] if found else f"order:{_tail(order.get('id'))}"
        self.apply("paid", contract, at=_when(order.get("processed_at") or order.get("created_at")),
                   plan=plan, user=self.linked_user(order), customer=customer, email=email,
                   order_name=str(order.get("name") or ""), order_id=_tail(order.get("id")), source="shopify")
        return "paid"

    def take_flow(self, body: dict[str, Any]) -> str:
        """A Flow step's own small message: {event, contract, status,
        nextBillingDate, customerId, email, orderName, lines, link}."""
        event = str(body.get("event") or "").lower().replace(" ", "_")
        contract = _tail(body.get("contract"))
        kind = {"billing_success": "paid", "billing_failure": "failed", "cancelled": "cancelled",
                "paused": "paused", "created": "paid"}.get(event, "sync")
        plan = self.plan_of(body)
        if not contract or not (plan or self.store.subscription(contract)):
            return "ignored: not a plan"
        self.apply(kind, contract, plan=plan, user=self.linked_user(body),
                   status=str(body.get("status") or ("active" if event == "activated" else "")),
                   through=_when(body.get("nextBillingDate")) if event in ("created", "billing_success") else None,
                   customer=_tail(body.get("customerId")), email=str(body.get("email") or ""),
                   order_name=str(body.get("orderName") or ""), source="flow")
        return event or "sync"

    def reconcile(self) -> dict[str, Any]:
        """Ask Appstle for every subscription and put right what differs."""
        if not self._secret["api"]:
            raise HTTPException(status_code=503, detail="no Appstle API key is set on this server")
        seen = changed = page = 0
        while page < 50:
            status, body = self.send("GET", f"{APPSTLE_API}/subscription-contract-details?page={page}&size=200",
                                     {"X-API-Key": self._secret["api"], "Accept": "application/json"}, b"")
            rows = body.get("items") if isinstance(body, dict) else None
            if status != 200 or not isinstance(rows, list):
                raise HTTPException(status_code=502, detail=f"Appstle answered {status}")
            for item in rows:
                contract, plan = _tail(item.get("subscriptionContractId")), self.plan_of(item)
                if not contract or not (plan or self.store.subscription(contract)):
                    continue
                seen += 1
                before = self.store.subscription(contract)
                paid = "fail" not in str(item.get("lastPaymentStatus") or "").lower()
                after = self.apply("sync", contract, plan=plan, user=self.linked_user(item),
                                   status=str(item.get("status") or ""),
                                   through=_when(item.get("nextBillingDate")) if paid else None,
                                   customer=_tail(item.get("customerId")),
                                   email=str(item.get("customerEmail") or ""),
                                   order_name=str(item.get("orderName") or ""), source="reconcile")
                keys = ("status", "paid_through", "plan", "user")
                changed += before is None or any(before[k] != after[k] for k in keys)
            if len(rows) < 200:
                break
            page += 1
        _log.info("billing reconcile: %s subscriptions seen, %s changed", seen, changed)
        return {"seen": seen, "changed": changed}

    def run_nightly(self, every: float = DAY) -> None:
        """Reconcile once a day, for as long as the server runs."""
        def loop() -> None:
            while True:
                time.sleep(every)
                try:
                    self.reconcile()
                except Exception:                                        # noqa: BLE001 - try again tomorrow
                    _log.exception("billing reconcile failed")
        if self.enabled and self._secret["api"]:
            threading.Thread(target=loop, name="billing-reconcile", daemon=True).start()

    # -- what a person gets ---------------------------------------------------------
    def standing(self, row: dict[str, Any], now: float) -> tuple[str, float]:
        """Where one subscription stands, and until when: active (paid up),
        grace (payment late, still allowed), ending (cancelled, paid time
        left) or lapsed (since when)."""
        through = float(row["paid_through"])
        if row["status"] in ("active", "past_due"):
            if row["status"] == "active" and now <= through:
                return "active", through
            return ("grace", through + self.grace) if now <= through + self.grace else ("lapsed", through + self.grace)
        if row["status"] == "refunded":
            return "lapsed", float(row["ended"] or through)
        return ("ending", through) if now <= through else ("lapsed", max(through, float(row["ended"] or 0)))

    def entitlement(self, user_id: str) -> dict[str, Any] | None:
        """This person's plan as `Personal.entitlement()` returns it, or
        None if they have never had one."""
        now, best = self.clock(), None
        for row in self.store.subscriptions_of(user_id):
            plan = self.plans.get(row["plan"])
            if plan is None:
                continue
            state, until = self.standing(row, now)
            room = int(float(plan.get("storageGB") or 0) * 1024 ** 3)
            rank = (state != "lapsed", room, until)
            if best is None or rank > best[0]:
                best = (rank, row, plan, state, until, room)
        if best is None:
            return None
        _rank, row, plan, state, until, room = best
        day = time.strftime("%b %-d", time.gmtime(until))
        notice = {"grace": f"Your last payment did not go through. Uploads stop on {day} unless it does.",
                  "ending": f"Your plan was cancelled and ends on {day}.",
                  "lapsed": "Your plan has ended. Your images are kept until "
                            f"{time.strftime('%b %-d, %Y', time.gmtime(until + self.keep))}; "
                            "subscribe again to keep uploading."}.get(state, "")
        if row["source"] == "manual" and state == "ending":
            state, notice = "active", ""                  # given by hand, to a date
        allowed = state != "lapsed"
        return {"personal": allowed, "plan": str(plan.get("name") or row["plan"]),
                "storageBytes": room if allowed else 0, "state": state, "until": until,
                "planKey": row["plan"], "notice": notice,
                "why": "" if allowed else "your Starfront plan has ended"}

    def view(self, who: dict[str, Any], allowed: dict[str, Any]) -> dict[str, Any]:
        """What the My imaging page needs to offer, show or manage a plan."""
        if not self.enabled:
            return {"enabled": False}
        code = urllib.parse.quote(self.link_code(str(who["id"])), safe="")
        plans = []
        for key, plan in self.plans.items():
            template = str(plan.get("subscribeUrl") or self.subscribe_url)
            url = template.replace("{link}", code).replace("{variant}", str(plan.get("variant") or "")) \
                .replace("{sellingPlan}", str(plan.get("sellingPlan") or ""))
            plans.append({"key": key, "name": str(plan.get("name") or key), "price": str(plan.get("price") or ""),
                          "storageBytes": int(float(plan.get("storageGB") or 0) * 1024 ** 3),
                          "url": url if url.startswith("https://") else ""})
        return {"enabled": True, "state": allowed.get("state") or ("beta" if allowed.get("plan") == "beta" else ""),
                "until": allowed.get("until"), "notice": allowed.get("notice") or "",
                "manageUrl": self.manage_url if self.manage_url.startswith("https://") else "", "plans": plans}

    # -- the routes --------------------------------------------------------------------
    def _owner(self, request: Request) -> dict[str, Any]:
        try:
            who = self.who(request)
        except HTTPException:
            who = None
        if who is None or not who.get("admin") or who.get("kind") == "agent":
            raise HTTPException(status_code=403, detail="only the server's owners see billing")
        return who

    def _take(self, source: str, event_id: str, topic: str, work: Callable[[], str]) -> dict[str, Any]:
        """Do a checked message's work once; a repeat is thanked and dropped."""
        if not event_id:
            raise HTTPException(status_code=400, detail="the message has no id")
        event_id = f"{source}:{event_id}"[:200]
        if not self.store.add_billing_event(event_id, source, topic[:80]):
            return {"ok": True, "repeat": True}
        try:
            result = work()
        except Exception:
            # Not taken in: the shop will send it again, and must be let in.
            self.store.forget_billing_event(event_id)
            raise
        self.store.set_billing_event(event_id, "", "", result)
        return {"ok": True, "result": result}

    async def _json(self, request: Request, secret: str) -> tuple[bytes, dict[str, Any]]:
        if not self.enabled or not self._secret[secret]:
            raise HTTPException(status_code=404, detail="not here")
        body = await request.body()
        if len(body) > 2_000_000:
            raise HTTPException(status_code=413, detail="too large")
        return body, {}

    @staticmethod
    def _parse(body: bytes) -> dict[str, Any]:
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="send JSON") from None
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="send a JSON object")
        return parsed

    def _routes(self, app: FastAPI) -> None:

        @app.post("/api/v1/billing/appstle")
        async def appstle(request: Request) -> dict[str, Any]:
            body, _ = await self._json(request, "appstle")
            h = request.headers
            # Appstle documents webhook-*; the same scheme is also seen as svix-*.
            msg_id = h.get("webhook-id") or h.get("svix-id") or ""
            if not verify_standard(self._secret["appstle"], body, msg_id,
                                   h.get("webhook-timestamp") or h.get("svix-timestamp") or "",
                                   h.get("webhook-signature") or h.get("svix-signature") or "", self.clock()):
                raise HTTPException(status_code=401, detail="bad signature")
            message = self._parse(body)
            return self._take("appstle", msg_id, str(message.get("type") or ""),
                              lambda: self.take_appstle(message))

        @app.post("/api/v1/billing/shopify")
        async def shopify(request: Request) -> dict[str, Any]:
            body, _ = await self._json(request, "shopify")
            h = request.headers
            if not verify_shopify(self._secret["shopify"], body, h.get("x-shopify-hmac-sha256") or ""):
                raise HTTPException(status_code=401, detail="bad signature")
            topic, order = h.get("x-shopify-topic") or "", self._parse(body)
            return self._take("shopify", h.get("x-shopify-webhook-id") or "", topic,
                              lambda: self.take_shopify(topic, order))

        @app.post("/api/v1/billing/flow")
        async def flow(request: Request) -> dict[str, Any]:
            body, _ = await self._json(request, "flow")
            sent = request.headers.get("x-starfront-secret") or ""
            if len(self._secret["flow"]) < 24 or not hmac.compare_digest(sent, self._secret["flow"]):
                raise HTTPException(status_code=401, detail="bad secret")
            message = self._parse(body)
            # Flow has no message id: the same words twice in ten minutes are
            # one message sent twice.
            # A month later the same words are a new payment, so the id
            # also says roughly when.
            event_id = f"{hashlib.sha256(body).hexdigest()[:32]}:{int(self.clock() // 600)}"
            return self._take("flow", event_id, str(message.get("event") or ""),
                              lambda: self.take_flow(message))

        @app.post("/api/v1/me/billing/link")
        def claim(body: Claim, request: Request) -> dict[str, Any]:
            """Somebody who bought without their link code claims the purchase:
            the order number and the email it was bought with, both."""
            try:
                who = self.who(request)
            except HTTPException:
                who = None
            if who is None or not who.get("id") or who.get("kind") == "agent":
                raise HTTPException(status_code=401, detail="sign in with Discord first")
            if not self.enabled:
                raise HTTPException(status_code=404, detail="not here")
            order, email = body.order.strip().lstrip("#"), body.email.strip().lower()
            for row in self.store.subscriptions(unlinked=True):
                if row["order_name"].lstrip("#") == order and row["email"] and \
                        hmac.compare_digest(row["email"], email):
                    self.store.put_subscription(row["contract"], user=str(who["id"]))
                    return {"linked": True}
            raise HTTPException(status_code=404, detail="no unclaimed subscription matches that order number "
                                                        "and email; check both, or use Get help")

        @app.get("/api/v1/admin/billing")
        def listing(request: Request) -> dict[str, Any]:
            """Every subscription, where it stands, and the last messages."""
            self._owner(request)
            now, rows = self.clock(), []
            for row in self.store.subscriptions():
                state, until = self.standing(row, now)
                rows.append({k: row[k] for k in ("contract", "user", "plan", "status", "paid_through",
                                                 "customer", "email", "order_name", "source", "updated")}
                            | {"state": state, "until": until,
                               "keepUntil": until + self.keep if state == "lapsed" else None})
            return {"enabled": self.enabled, "plans": list(self.plans),
                    "doors": {name: bool(self._secret[name]) for name in ("appstle", "shopify", "flow", "api")},
                    "subscriptions": rows, "unlinked": [r["contract"] for r in rows if not r["user"]],
                    "events": self.store.billing_events(50)}

        @app.post("/api/v1/admin/billing/grant")
        def grant(body: Grant, request: Request) -> dict[str, Any]:
            """Give somebody a plan by hand, for a number of days: a comp, a
            founder, a customer the shop cannot bill."""
            self._owner(request)
            if body.plan not in self.plans:
                raise HTTPException(status_code=400, detail=f"no plan called {body.plan!r}")
            now = self.clock()
            return self.store.put_subscription(
                f"manual:{body.user}:{body.plan}", user=body.user, plan=body.plan, status="cancelled",
                paid_through=now + body.days * DAY, ended=now, source="manual", payload={"note": body.note})

        @app.post("/api/v1/admin/billing/link")
        def link(body: Link, request: Request) -> dict[str, Any]:
            """Say whose a subscription is (or, with a blank user, nobody's)."""
            self._owner(request)
            if self.store.subscription(body.contract) is None:
                raise HTTPException(status_code=404, detail="no such subscription")
            return self.store.put_subscription(body.contract, user=body.user.strip())

        @app.post("/api/v1/admin/billing/reconcile")
        def reconcile_now(request: Request) -> dict[str, Any]:
            self._owner(request)
            return self.reconcile()
