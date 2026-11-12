"""Paid plans: the shop's messages, checked, become what a person may do."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import agentuploads  # noqa: E402
from server.agentuploads import AgentUploads  # noqa: E402
from server.billing import DAY, Billing  # noqa: E402
from server.files import Files  # noqa: E402
from server.jobs import Jobs  # noqa: E402
from server.personal import Personal  # noqa: E402
from server.releases import Releases  # noqa: E402
from server.store import Store  # noqa: E402

RIG = {"authorization": "Bearer rig-a"}
BRAY = {"authorization": "Bearer person-bray"}
ZOE = {"authorization": "Bearer person-zoe"}
BOSS = {"authorization": "Bearer person-boss"}
PEOPLE = {"person-bray": {"kind": "user", "id": "u-bray", "name": "astrofalls", "roles": [], "admin": False},
          "person-zoe": {"kind": "user", "id": "u-zoe", "name": "zoe", "roles": [], "admin": False},
          "person-boss": {"kind": "user", "id": "u-boss", "name": "boss", "roles": [], "admin": True}}
# Made-up values, for these tests only.
KEYS = {"appstle": "whsec_" + base64.b64encode(b"test-appstle-key").decode(), "shopify": "test-shopify-key",
        "flow": "test-flow-key-of-some-length-x", "api": "apst_test", "link": "test-link-key"}
CONFIG = {"plans": [{"key": "small", "name": "Hobbyist", "storageGB": 100, "price": "$1 a month",
                     "variants": ["111"], "variant": "111"},
                    {"key": "big", "name": "Observatory", "storageGB": 1000, "skus": ["SF-BIG"], "variant": "222"}],
          "graceDays": 7, "periodDays": 31, "keepDays": 90,
          "subscribeUrl": "https://shop.example/products/auto?variant={variant}&sf={link}",
          "manageUrl": "https://shop.example/apps/subscriptions"}
T0 = 1_800_000_000.0


class World:
    def __init__(self, tmp_path, monkeypatch, *, enabled=True, keys=KEYS, beta=()):
        monkeypatch.setattr(agentuploads, "AGENT_KEEP_FREE", 0)
        self.now = T0
        self.store = Store(tmp_path / "db.sqlite")
        app = FastAPI()
        owns = lambda who, project: who.get("admin") or project.get("owner_id") == who["id"]  # noqa: E731
        files = Files(app, self.store, tmp_path / "files", PEOPLE.get, owns, [], "http://t", lambda: "c")
        up = AgentUploads(app, self.store, files)
        rel = tmp_path / "files" / "_releases"
        jobs = Jobs(app, self.store, rel, files.who, owns, tmp_path / "job-token.txt", latest=lambda pid: None)
        releases = Releases(app, self.store, rel, files.who, owns)
        self.personal = Personal(app, self.store, files, up.agent, files.who, [], enabled=True, beta=list(beta),
                                 storage_gb=10, releases=releases, jobs=jobs)
        up.personal = jobs.personal = self.personal
        self.appstle_api = []
        self.billing = Billing(app, self.store, files.who, tmp_path, enabled=enabled, config=CONFIG,
                               secrets_=keys, send=self._send, clock=lambda: self.now)
        self.personal.billing = self.billing
        for uid, name in (("u-bray", "astrofalls"), ("u-zoe", "zoe"), ("u-boss", "boss")):
            self.store.upsert_user(uid, "web-" + uid, name, "", [])
        self.store.add_agent("aaaaaaaa1111", "rig-a", "fsq106", "astrofalls", "u-bray")
        self.client = TestClient(app)

    def _send(self, method, url, headers, body):
        assert headers["X-API-Key"] == "apst_test"
        return 200, {"items": self.appstle_api}

    def appstle(self, kind, data, msg_id="m1", key=None, stamp=None):
        body = json.dumps({"type": kind, "data": data}).encode()
        stamp = str(int(self.now if stamp is None else stamp))
        raw = base64.b64decode((key or KEYS["appstle"])[6:])
        sig = base64.b64encode(hmac.new(raw, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
        return self.client.post("/api/v1/billing/appstle", content=body, headers={
            "webhook-id": msg_id, "webhook-timestamp": stamp, "webhook-signature": f"v1,bogus v1,{sig}"})

    def shopify(self, topic, payload, hook="h1", key="test-shopify-key"):
        body = json.dumps(payload).encode()
        sig = base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()
        return self.client.post("/api/v1/billing/shopify", content=body, headers={
            "x-shopify-hmac-sha256": sig, "x-shopify-topic": topic, "x-shopify-webhook-id": hook})

    def contract(self, user="u-bray", *, status="ACTIVE", days=30, variant="gid://shopify/ProductVariant/111",
                 cid="gid://shopify/SubscriptionContract/9001", code=None):
        code = self.billing.link_code(user) if code is None and user else (code or "")
        return {"id": cid, "status": status,
                "nextBillingDate": __import__("datetime").datetime.fromtimestamp(
                    self.now + days * DAY, __import__("datetime").timezone.utc).isoformat(),
                "customer": {"id": "gid://shopify/Customer/55", "email": "Bray@Example.com"},
                "originOrder": {"id": "gid://shopify/Order/700", "name": "#1234"},
                "lines": {"nodes": [{"variantId": variant, "sku": "X",
                                     "customAttributes": [{"key": "_starfront", "value": code}]}]}}

    def allowed(self, who=BRAY):
        return self.client.get("/api/v1/me/personal", headers=who).json()


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_off_by_default_and_closed_without_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("STARFRONT_BILLING", raising=False)
    assert Billing(FastAPI(), Store(tmp_path / "x.sqlite"), lambda r: None, tmp_path).enabled is False
    off = World(tmp_path / "off", monkeypatch, enabled=False)
    assert off.appstle("subscription.created", off.contract()).status_code == 404
    assert off.allowed()["available"] is False and off.allowed()["billing"] == {"enabled": False}
    bare = World(tmp_path / "bare", monkeypatch, keys={})
    assert bare.appstle("subscription.created", bare.contract()).status_code == 404
    assert bare.shopify("orders/paid", {}).status_code == 404
    assert bare.client.post("/api/v1/billing/flow", json={}).status_code == 404


def test_a_bad_signature_changes_nothing(w):
    wrong = "whsec_" + base64.b64encode(b"somebody-else").decode()
    assert w.appstle("subscription.created", w.contract(), key=wrong).status_code == 401
    assert w.appstle("subscription.created", w.contract(), stamp=T0 - 3600).status_code == 401     # a replay
    assert w.shopify("orders/paid", {"id": 1}, key="nope").status_code == 401
    assert w.client.post("/api/v1/billing/flow", json={}, headers={"x-starfront-secret": "no"}).status_code == 401
    assert w.store.subscriptions() == [] and w.allowed()["available"] is False


def test_buying_a_plan_opens_personal_projects(w):
    before = w.allowed()
    assert before["available"] is False and "need a Starfront plan" in before["why"]
    offer = before["billing"]["plans"][0]
    assert offer["name"] == "Hobbyist" and offer["price"] == "$1 a month"
    assert offer["url"] == "https://shop.example/products/auto?variant=111&sf=" + w.billing.link_code("u-bray")
    assert w.client.post("/api/v1/agent/personal", json={"target": "M31"}, headers=RIG).status_code == 402
    assert w.appstle("subscription.created", w.contract()).json() == {"ok": True, "result": "subscription.created"}
    assert w.appstle("subscription.created", w.contract()).json() == {"ok": True, "repeat": True}
    after = w.allowed()
    assert after["available"] and after["plan"] == "Hobbyist" and after["storageBytes"] == 100 * 1024 ** 3
    assert after["billing"]["state"] == "active" and after["billing"]["manageUrl"].startswith("https://shop.example")
    assert w.client.post("/api/v1/agent/personal", json={"target": "M31"}, headers=RIG).status_code == 200
    assert w.allowed(ZOE)["available"] is False                      # and nobody else's


def test_payment_failed_grace_then_uploads_stop_and_data_stays(w):
    w.appstle("subscription.created", w.contract())
    made = w.client.post("/api/v1/agent/personal", json={"target": "M31"}, headers=RIG).json()["project"]
    w.now += 30 * DAY + 60
    w.appstle("subscription.billing-failure", {"contractId": 9001, "billingDate": "2027-01-15T00:00:00Z"}, "m2")
    late = w.allowed()
    assert late["available"] and late["billing"]["state"] == "grace" and "did not go through" in late["billing"]["notice"]
    w.now += 8 * DAY
    gone = w.allowed()
    assert gone["available"] is False and gone["billing"]["state"] == "lapsed" and "are kept until" in gone["billing"]["notice"]
    assert [p["id"] for p in gone["projects"]] == [made]             # still there to see
    assert w.client.post("/api/v1/agent/personal", json={"target": "M31"}, headers=RIG).status_code == 402
    assert w.client.post(f"/api/v1/releases/{made}/stack", headers=BRAY).status_code == 402
    # The retry goes through: back on, with nothing to do on the server.
    w.appstle("subscription.billing-success", {"contractId": 9001, "orderId": 701, "orderName": "#1300"}, "m3")
    assert w.allowed()["billing"]["state"] == "active"
    assert w.client.post(f"/api/v1/releases/{made}/stack", headers=BRAY).status_code == 202


def test_cancel_runs_to_the_end_of_what_was_paid(w):
    w.appstle("subscription.created", w.contract())
    w.now += 5 * DAY
    w.appstle("subscription.cancelled", w.contract(status="CANCELLED", days=25), "m2")
    d = w.allowed()
    assert d["available"] and d["billing"]["state"] == "ending" and "ends on" in d["billing"]["notice"]
    w.now += 26 * DAY
    assert w.allowed()["available"] is False


def test_changing_plan_changes_the_room(w):
    w.appstle("subscription.created", w.contract())
    w.appstle("subscription.updated", w.contract(variant="gid://shopify/ProductVariant/9", days=30) | {
        "lines": {"nodes": [{"variantId": "gid://shopify/ProductVariant/9", "sku": "SF-BIG"}]}}, "m2")
    d = w.allowed()
    assert d["plan"] == "Observatory" and d["storageBytes"] == 1000 * 1024 ** 3


def test_other_subscriptions_in_the_shop_are_not_ours(w):
    pier = w.contract(variant="gid://shopify/ProductVariant/31337")
    assert w.appstle("subscription.created", pier).json()["result"] == "ignored: not a plan"
    assert w.appstle("subscription.billing-success", {"contractId": 4242}, "m2").json()["result"] == "ignored: not a plan"
    assert w.store.subscriptions() == []


def test_no_code_or_a_made_up_one_is_unlinked_until_claimed(w):
    forged = "sf1.u-zoe." + "0" * 16
    w.appstle("subscription.created", w.contract(user="", code=forged))
    assert w.allowed(ZOE)["available"] is False and w.allowed()["available"] is False
    listing = w.client.get("/api/v1/admin/billing", headers=BOSS).json()
    assert listing["unlinked"] == ["9001"] and w.client.get("/api/v1/admin/billing", headers=BRAY).status_code == 403
    claim = {"order": "1234", "email": "wrong@example.com"}
    assert w.client.post("/api/v1/me/billing/link", json=claim, headers=BRAY).status_code == 404
    assert w.client.post("/api/v1/me/billing/link", json=claim).status_code == 401
    claim["email"] = "BRAY@example.com"
    assert w.client.post("/api/v1/me/billing/link", json=claim, headers=BRAY).json() == {"linked": True}
    assert w.allowed()["available"] is True
    assert w.client.post("/api/v1/me/billing/link", json=claim, headers=ZOE).status_code == 404    # taken
    # An owner can move it, or give a plan by hand.
    w.client.post("/api/v1/admin/billing/link", json={"contract": "9001", "user": "u-zoe"}, headers=BOSS)
    assert w.allowed()["available"] is False and w.allowed(ZOE)["available"] is True
    r = w.client.post("/api/v1/admin/billing/grant", json={"user": "u-bray", "plan": "big", "days": 10}, headers=BOSS)
    assert r.status_code == 200
    d = w.allowed()
    assert d["plan"] == "Observatory" and d["billing"]["state"] == "active" and d["billing"]["notice"] == ""
    w.now += 11 * DAY
    assert w.allowed()["available"] is False


def order(w, oid=700, user="u-bray"):
    return {"id": oid, "name": f"#{oid}", "email": "bray@example.com", "customer": {"id": 55},
            "created_at": "2027-01-15T10:00:00Z", "note_attributes": [],
            "line_items": [{"variant_id": 111, "sku": "", "properties": [
                {"name": "_starfront", "value": w.billing.link_code(user)}]}]}


def test_shopify_orders_alone_are_enough_and_refunds_end_it(w):
    w.now = 1_800_010_000.0
    assert w.shopify("orders/paid", order(w)).json()["result"] == "paid"
    assert w.shopify("orders/paid", {"id": 1, "line_items": [{"variant_id": 5}]}, "h0").json()["result"] == "ignored: not a plan"
    assert w.allowed()["billing"]["state"] == "active"
    # Appstle then names the same purchase: one subscription, not two.
    w.appstle("subscription.created", w.contract())
    assert [s["contract"] for s in w.store.subscriptions()] == ["9001"]
    # A renewal order extends it; no order, and it lapses by itself.
    through = w.store.subscription("9001")["paid_through"]
    w.shopify("orders/paid", order(w, 801) | {"created_at": "2027-03-01T10:00:00Z"}, "h2")
    assert w.store.subscription("9001")["paid_through"] > through
    refund = {"order_id": 801, "refund_line_items": [{"line_item": {"variant_id": 111}}]}
    assert w.shopify("refunds/create", refund, "h3").json()["result"] == "refunded"
    assert w.allowed()["available"] is False
    w.appstle("subscription.updated", w.contract(), "m9")            # still active at the shop: stays refunded
    assert w.allowed()["available"] is False


def test_flow_door(w):
    body = {"event": "created", "contract": "gid://shopify/SubscriptionContract/77", "status": "ACTIVE",
            "email": "z@example.com", "lines": [{"sku": "SF-BIG"}], "link": w.billing.link_code("u-zoe")}
    send = lambda b: w.client.post("/api/v1/billing/flow", json=b, headers={"x-starfront-secret": KEYS["flow"]})  # noqa: E731
    assert send(body).json()["result"] == "created"
    assert w.allowed(ZOE)["plan"] == "Observatory"
    assert send({"event": "cancelled", "contract": "77"}).json()["result"] == "cancelled"
    assert w.allowed(ZOE)["billing"]["state"] == "ending"


def test_reconcile_puts_right_what_a_missed_message_left(w):
    w.appstle("subscription.created", w.contract())
    w.appstle_api = [
        {"subscriptionContractId": 9001, "status": "cancelled", "customerEmail": "bray@example.com",
         "contractDetailsJSON": json.dumps([{"variantId": "gid://shopify/ProductVariant/111"}])},
        {"subscriptionContractId": 9002, "status": "active", "lastPaymentStatus": "SUCCEEDED",
         "nextBillingDate": "2027-06-01T00:00:00Z", "customerEmail": "zoe@example.com",
         "contractDetailsJSON": json.dumps([{"sku": "SF-BIG"}]),
         "orderNoteAttributes": json.dumps([{"name": "x", "value": w.billing.link_code("u-zoe")}])},
        {"subscriptionContractId": 5, "status": "active", "contractDetailsJSON": "[{\"sku\": \"PIER\"}]"}]
    assert w.client.post("/api/v1/admin/billing/reconcile", headers=BRAY).status_code == 403
    r = w.client.post("/api/v1/admin/billing/reconcile", headers=BOSS).json()
    assert r == {"seen": 2, "changed": 2}
    assert w.allowed()["billing"]["state"] == "ending" and w.allowed(ZOE)["plan"] == "Observatory"
    assert w.client.post("/api/v1/admin/billing/reconcile", headers=BOSS).json() == {"seen": 2, "changed": 0}


def test_beta_people_keep_beta_and_a_plan_wins_when_they_buy(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, beta=("u-bray",))
    d = w.allowed()
    assert d["available"] and d["plan"] == "beta" and d["billing"]["state"] == "beta"
    w.appstle("subscription.created", w.contract())
    assert w.allowed()["plan"] == "Hobbyist"
    w.now += 60 * DAY
    assert w.allowed()["plan"] == "beta"                              # plan over, still on the list


def test_no_secret_is_ever_shown(w):
    w.appstle("subscription.created", w.contract())
    text = w.client.get("/api/v1/admin/billing", headers=BOSS).text + json.dumps(w.allowed())
    for value in KEYS.values():
        assert value not in text
