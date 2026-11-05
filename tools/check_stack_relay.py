"""A fake hub against the real Starfront server, end to end.

    python tools/check_stack_relay.py

Live stacking is a loop between three machines that never meet: a telescope's
N.I.N.A. plugin uploads frames to the server, a hub at somebody's home fetches
them from the server (it cannot be reached, so it asks), stacks them and pushes
the results back, and a browser reads those results off the server. Every
other check here covers one leg with the other legs stubbed. This one runs the
whole loop the way the real pieces will, over real HTTP, against the real
`server.app` on a loopback port with an empty database:

  1. a person registers a hub and starts a collaboration bound to it, and a
     telescope joins and is handed a grant;
  2. as the plugin would: the grant, `resolve`/`project`/`join`/`rigs`, then
     `frames/init` and the PUT of every part to the ABSOLUTE URLs it returned
     (three parts, out of order, the plugin's 16 MiB layout with a short last
     part), then `complete`;
  3. as the hub would (a small fake hub that holds nothing but its hub token):
     pull the change feed, list the uploaded frames, fetch the bytes in two
     `Range` requests, check the sha256, ack;
  4. the hub pushes back a verdict, `status.json` (held to RSC's
     `status.schema.json`), a preview, a deep-zoom tar and a gold run;
  5. the pages see all of it, with the right cache and CSP headers;
  6. retention: with `STARFRONT_STACK_RELAY_KEEP_DAYS=0` the bytes go at the
     ack and the row and the verdict stay.

Faults this exists to catch are the seams between the pieces: a relative part
URL (the plugin's client has no base address, so every upload would fail), a
part layout the server and the plugin disagree on, a Range that returns the
wrong bytes, an ack that does not take a frame out of the list, a change row
the hub never sees, a hub-visible response carrying a Discord id instead of the
collab id, a push the pages cannot read, a heartbeat the dashboard cannot use,
a gold announced twice or not at all, and retention that deletes the verdict
with the bytes.

No test framework, for the same reason as the other checks here.
"""

import hashlib
import io
import json
import os
import random
import re
import socket
import struct
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = Path(tempfile.mkdtemp())
ADMIN = "test-admin-token"
os.environ["STARFRONT_DATA"] = str(DATA)
os.environ["STARFRONT_ADMIN_TOKEN"] = ADMIN
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())
os.environ.pop("STARFRONT_STACK_RELAY_KEEP_DAYS", None)

STATUS_SCHEMA = Path("/Users/eddie/Develop/EKEServices/Astro/Rust/rapid-stacker-collab/docs/api/status.schema.json")

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"

# ------------------------------------------------------------ a fake Discord
# Just enough to sign somebody in: authorize (straight back with a code), the
# token exchange, who-am-I and the guild membership. Same as check_server.py's;
# that file runs its whole check on import, so the minimum is copied.
DISCORD_PORT = free_port()
DISCORD_BASE = f"http://127.0.0.1:{DISCORD_PORT}"
GUILD = "guild-777"
ROLE = "role-imager"
PEOPLE = {
    "code-alice": {"id": "1001", "username": "alice", "global_name": "Alice",
                   "member": True, "roles": [ROLE]},
    "code-bob": {"id": "1002", "username": "bob", "global_name": "Bob",
                 "member": True, "roles": []},
}


def fake_discord():
    import uvicorn
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.responses import RedirectResponse
    fake = FastAPI()

    @fake.get("/oauth2/authorize")
    def authorize(redirect_uri: str, state: str, who: str = "code-alice"):
        return RedirectResponse(f"{redirect_uri}?code={who}&state={state}")

    @fake.post("/oauth2/token")
    async def token(request: Request):
        form = dict(urllib.parse.parse_qsl((await request.body()).decode()))
        code = form.get("code", "")
        if form.get("client_secret") != "shh" or code not in PEOPLE:
            raise HTTPException(status_code=400, detail="bad code")
        return {"access_token": f"bearer-{code}", "token_type": "Bearer"}

    def person(authorization: str):
        return PEOPLE.get(authorization.replace("Bearer bearer-", ""))

    @fake.get("/users/@me")
    def me(authorization: str = Header(default="")):
        p = person(authorization)
        return {"id": p["id"], "username": p["username"], "global_name": p["global_name"], "avatar": ""}

    @fake.get("/users/@me/guilds/{guild}/member")
    def member(guild: str, authorization: str = Header(default="")):
        p = person(authorization)
        if guild != GUILD or not p["member"]:
            raise HTTPException(status_code=404, detail="Unknown Guild")
        return {"roles": p["roles"], "nick": None}

    uvicorn.run(fake, host="127.0.0.1", port=DISCORD_PORT, log_level="error")


threading.Thread(target=fake_discord, daemon=True).start()

os.environ["STARFRONT_DISCORD_CLIENT_ID"] = "app-1"
os.environ["STARFRONT_DISCORD_CLIENT_SECRET"] = "shh"
os.environ["STARFRONT_DISCORD_GUILD"] = GUILD
os.environ["STARFRONT_DISCORD_ROLE"] = ROLE
os.environ["STARFRONT_DISCORD_OWNERS"] = "1001"
os.environ["STARFRONT_PUBLIC_URL"] = BASE
os.environ["STARFRONT_DISCORD_API"] = DISCORD_BASE
os.environ["STARFRONT_DISCORD_AUTHORIZE"] = f"{DISCORD_BASE}/oauth2/authorize"

# ------------------------------------------------- a fake Discord webhook
# The collaboration's webhook points here. The server only accepts
# discord.com URLs, so its pattern is widened to this one loopback address.
HOOK_PORT = free_port()
HOOK = f"http://127.0.0.1:{HOOK_PORT}/api/webhooks/1234567890/tok_en-ABC"
MESSAGES = []


class Webhook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        MESSAGES.append(json.loads(body or b"{}").get("content", ""))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", HOOK_PORT), Webhook).serve_forever,
                 daemon=True).start()

from server import notify                                        # noqa: E402
notify._WEBHOOK = re.compile(rf"http://127\.0\.0\.1:{HOOK_PORT}/api/webhooks/[0-9]+/[A-Za-z0-9_-]+")


# --------------------------------------------------------------- the server
def serve():
    import uvicorn
    from server.app import app
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")


threading.Thread(target=serve, daemon=True).start()
for _ in range(150):
    try:
        urllib.request.urlopen(f"{BASE}/api/v1/health", timeout=1).read()
        break
    except Exception:
        time.sleep(0.2)
else:
    print("FAIL  the server did not start")
    sys.exit(1)

from server import app as server_app                         # noqa: E402
from server.stack.frames import PART_SIZE                        # noqa: E402

STORE = server_app.store
RELAY = DATA / "stack" / "relay"
PUBLIC = DATA / "stack" / "public"


# ------------------------------------------------------------------ HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(method, url, body=None, headers=None, token=None, cookie=None, raw=None):
    """One request. Returns (status, parsed body or bytes, headers).

    `body` is sent as JSON, `raw` as given. A JSON answer is parsed, anything
    else comes back as bytes."""
    head = dict(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        head["Content-Type"] = "application/json"
    if token:
        head["Authorization"] = f"Bearer {token}"
    if cookie:
        head["Cookie"] = cookie
    request = urllib.request.Request(url if url.startswith("http") else BASE + url,
                                     data=data, method=method, headers=head)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, payload, got = response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        status, payload, got = exc.code, exc.read(), exc.headers
    if "json" in (got.get("Content-Type") or ""):
        try:
            return status, json.loads(payload.decode() or "{}"), got
        except ValueError:
            pass
    return status, payload, got


def call(method, path, body=None, token=None, **kw):
    status, got, _ = http(method, path, body, token=token, **kw)
    return status, got


def sign_in(who):
    """The device-code flow end to end; returns the program token."""
    _, begun = call("POST", "/api/v1/auth/login")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        opener.open(urllib.request.Request(begun["url"]), timeout=10)
    except urllib.error.HTTPError as hop:
        to_discord = hop.headers.get("Location") + f"&who={who}"
    try:
        opener.open(urllib.request.Request(to_discord), timeout=10)
    except urllib.error.HTTPError as hop:
        back = hop.headers.get("Location")
    urllib.request.urlopen(back, timeout=10).read()
    return call("GET", f"/api/v1/auth/poll?code={begun['code']}")[1]["token"]


def browser_cookie(who):
    """A browser signing in on the files page: the `starfront_web=...` cookie."""
    opener = urllib.request.build_opener(_NoRedirect())

    def hop(url):
        try:
            opener.open(urllib.request.Request(url), timeout=10)
        except urllib.error.HTTPError as h:
            return h
    to_start = hop(f"{BASE}/files/signin").headers["Location"]
    to_discord = hop(BASE + to_start).headers["Location"] + f"&who={who}"
    back = hop(to_discord).headers["Location"]
    return hop(back).headers["Set-Cookie"].split(";", 1)[0]


# ------------------------------------------------------- the status schema
def validate(schema, value, root, path="$"):
    """The parts of JSON Schema status.schema.json uses: $ref, const, enum,
    type, required, additionalProperties, properties, items, oneOf, pattern,
    minimum, minLength. Returns a list of problems (empty when it fits)."""
    while "$ref" in schema:
        node = root
        for key in schema["$ref"].lstrip("#/").split("/"):
            node = node[key]
        schema = node
    if "oneOf" in schema:
        tries = [validate(s, value, root, path) for s in schema["oneOf"]]
        return [] if any(not t for t in tries) else [f"{path}: matches none of oneOf ({tries[0][:1]})"]
    bad = []
    if "const" in schema and value != schema["const"]:
        bad.append(f"{path}: not {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        bad.append(f"{path}: {value!r} not in enum")
    kind = schema.get("type")
    py = {"object": dict, "array": list, "string": str, "null": type(None), "integer": int,
          "number": (int, float), "boolean": bool}.get(kind)
    if py is not None and (not isinstance(value, py) or (kind in ("integer", "number") and isinstance(value, bool))):
        return bad + [f"{path}: not a {kind}"]
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            bad.append(f"{path}: {value!r} does not match the pattern")
        if len(value) < schema.get("minLength", 0):
            bad.append(f"{path}: too short")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value < schema.get("minimum", value):
        bad.append(f"{path}: below the minimum")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                bad.append(f"{path}: missing {key}")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                bad += validate(props[key], item, root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                bad.append(f"{path}: unexpected {key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            bad += validate(schema["items"], item, root, f"{path}[{i}]")
    return bad


# ------------------------------------------------------- synthetic frames
def fits_bytes(blocks, seed):
    """A FITS file of `blocks` 2880-byte blocks: one header block, the rest data."""
    cards = [("SIMPLE", "T"), ("BITPIX", "8"), ("NAXIS", "2"), ("NAXIS1", "2880"),
             ("NAXIS2", str(blocks - 1)), ("IMAGETYP", "'Light Frame'"), ("FILTER", "'Ha'")]
    header = "".join(f"{k:<8}= {v:>20}".ljust(80) for k, v in cards) + "END".ljust(80)
    return header.ljust(2880).encode("ascii") + random.Random(seed).randbytes(2880 * (blocks - 1))


def frame_header(size_blocks, stamp):
    return {"IMAGETYP": "Light Frame", "FILTER": "Ha", "EXPTIME": 300.0, "XBINNING": 1, "YBINNING": 1,
            "INSTRUME": "ASI2600MM", "TELESCOP": "Check 80", "FOCALLEN": 389.0, "XPIXSZ": 3.76,
            "DATE-OBS": stamp, "OBJECT": "Orion Check", "BITPIX": 8, "NAXIS1": 2880,
            "NAXIS2": size_blocks - 1}


def png_bytes():
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b""))


def iso(offset=0.0):
    t = time.time() + offset
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def multipart(fields, files):
    """(content type, body) of a multipart/form-data with plain fields and files."""
    boundary = "----check" + os.urandom(8).hex()
    out = io.BytesIO()
    for name, text in fields:
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.write(text.encode() + b"\r\n")
    for name, data in files:
        out.write((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name.split("/")[-1]}"\r\n'
                   "Content-Type: application/octet-stream\r\n\r\n").encode())
        out.write(data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", out.getvalue()


# --------------------------------------------------------------- the fake hub
class FakeHub:
    """A hub as the server sees one: it knows its id and its hub token and
    nothing else (no grant secret, no Discord, no database)."""

    def __init__(self, hub_id, token):
        self.id, self.token = hub_id, token
        self.cursor = 0

    def api(self, method, path, body=None, headers=None, raw=None):
        return http(method, f"/api/v1/hubs/{self.id}{path}", body, headers, token=self.token, raw=raw)

    def pull(self, since=None):
        status, got, _ = self.api("GET", f"/pull?since={self.cursor if since is None else since}&wait=0")
        if status == 200 and since is None:
            self.cursor = got["seq"]
        return status, got

    def uploaded(self):
        status, got, _ = self.api("GET", "/frames?state=uploaded&limit=100")
        return status, got

    def fetch(self, frame_id, size):
        """The bytes in two Range requests (a hub resumes this way)."""
        half = size // 2
        s1, a, h1 = self.api("GET", f"/frames/{frame_id}/bytes", headers={"Range": f"bytes=0-{half - 1}"})
        s2, b, h2 = self.api("GET", f"/frames/{frame_id}/bytes", headers={"Range": f"bytes={half}-"})
        ok = (s1 == 206 and s2 == 206 and isinstance(a, bytes) and isinstance(b, bytes)
              and h1.get("Content-Range") == f"bytes 0-{half - 1}/{size}"
              and h2.get("Content-Range") == f"bytes {half}-{size - 1}/{size}"
              and len(a) == half and len(b) == size - half)
        return ok, (a + b if ok else b"")

    def ack(self, frame_id, sha):
        status, got, _ = self.api("POST", f"/frames/{frame_id}/ack", {"sha256": sha})
        return status, got

    def heartbeat(self, **status):
        return self.api("POST", "/heartbeat", status)[0]

    def push(self, records, files):
        ctype, body = multipart([("records", json.dumps(records))], files)
        status, got, _ = self.api("POST", "/push", headers={"Content-Type": ctype}, raw=body)
        return status, got

    def push_master(self, project_id, name, data):
        """A master data file, alone, streamed route (T421)."""
        return self.api("PUT", f"/push/{project_id}/masters/{name}", raw=data,
                        headers={"X-Content-SHA256": hashlib.sha256(data).hexdigest(),
                                 "Content-Type": "application/octet-stream"})[:2]


# =========================================================== 1. who and what
print("-- a person registers a hub and starts a collaboration on it --")
BOB = sign_in("code-bob")
ALICE = sign_in("code-alice")
status, reg = call("POST", "/api/v1/hubs", {"name": "Relay hub", "lanUrl": "http://10.0.0.9:8790"}, token=BOB)
HUB = FakeHub(reg.get("hubId", ""), reg.get("hubToken", ""))
case("a person registers a hub and gets an id and a token", status == 200 and HUB.token.startswith("sfh_"), str(reg)[:100])

REGION = {"ra": 84.0, "dec": 0.0, "width": 16.0, "height": 8.0}
status, made = call("POST", "/api/v1/projects", {
    "name": "Relay Check", "region": REGION, "requirements": {"filters": {"Ha": 3.0}},
    "stacking": {"backend": "hub", "hubId": HUB.id}}, token=BOB)
SFP = made["project"]["id"]
STACK = STORE.stack_project_of(SFP) or {}
PID, SLUG = STACK.get("project_id", ""), STACK.get("slug", "")
case("they start a collaboration bound to it, which creates the stacking project",
     status == 200 and PID.startswith("prj_") and STACK.get("hub") == HUB.id, str(made)[:160])

# A telescope belonging to Alice, enrolled with a profile, joins the collaboration.
_, enrolled = call("POST", "/api/v1/agents", {"name": "Alice's scope"}, token=ALICE)
SCOPE = enrolled["token"]
call("POST", "/api/v1/agent/hello", {"protocol": 1, "profile": {
    "name": "Alice's scope", "focalLength": 389.0, "pixelSize": 3.76, "sensorWidth": 9576,
    "sensorHeight": 6388, "binning": 1, "filters": {"Ha": 3.0}, "hoursPerNight": 6.0,
    "scale": 206.265 * 3.76 / 389.0,
    "field": [206.265 * 3.76 / 389.0 * 9576 / 3600.0, 206.265 * 3.76 / 389.0 * 6388 / 3600.0]}}, token=SCOPE)
status, _ = call("POST", f"/api/v1/agent/projects/{SFP}/join", {}, token=SCOPE)
case("a telescope joins the collaboration", status == 200)
_, browse = call("GET", "/api/v1/agent/projects", token=SCOPE)
listing = next((p for p in browse["projects"] if p["id"] == SFP), {})
block = listing.get("stacking") or {}
GRANT = block.get("grant", "")
case("the agent listing carries a grant and says where the front door is",
     GRANT.startswith("sfg_") and block.get("collabProjectId") == PID and block.get("apiBase") == BASE + "/stack/api/",
     str(block)[:200])

# =========================================================== 2. the plugin
print("\n-- as the plugin: join, rig, upload --")
API = "/stack/api"
CODE = STACK["join_code"]
status, got = call("POST", f"{API}/projects/resolve-join-code", {"code": CODE}, token=GRANT)
case("resolve: the join code names the project", status == 200 and got["project"]["project_id"] == PID, str(got)[:160])
terms = got.get("terms_version", "")
status, got = call("GET", f"{API}/projects/{PID}", token=GRANT)
case("project: the grant reads it", status == 200 and got["project"]["slug"] == SLUG)
status, got = call("POST", f"{API}/projects/{PID}/join",
                   {"display_name": "Alice", "accepted_terms_version": terms}, token=GRANT)
case("join: the telescope becomes a member", status == 200 and got["membership"]["status"] == "active", str(got)[:160])
COLLAB_ALICE = got.get("membership", {}).get("user_id", "")
status, got = call("POST", f"{API}/rigs", {"camera": "ASI2600MM", "telescope": "Check 80", "focal_length_mm": 389.0,
                                           "pixel_size_um": 3.76, "binning": 1}, token=GRANT)
RIG = got.get("rig", {}).get("rig_id", "")
case("rigs: the telescope's rig is registered", status == 200 and RIG, str(got)[:160])
status, got = call("GET", f"{API}/rigs", token=GRANT)
case("...and listed", status == 200 and [r["rig_id"] for r in got["rigs"]] == [RIG])

BLOCKS = 11652                      # 33 557 760 bytes: two full 16 MiB parts and a short third
FRAME = fits_bytes(BLOCKS, 1)
SHA = hashlib.sha256(FRAME).hexdigest()
status, init = call("POST", f"{API}/projects/{PID}/frames/init", {
    "sha256": SHA, "size": len(FRAME), "filename": "Orion_Ha_300s_0001.fits", "stripped": True,
    "header": frame_header(BLOCKS, iso(-3600)), "rig_hint": {"rig_id": RIG}}, token=GRANT)
FID = init.get("frame_id", "")
up = init.get("upload", {})
urls = up.get("part_urls", [])
case("init: a frame id and a multipart plan", status == 200 and FID and up.get("type") == "multipart", str(init)[:200])
case("...in the plugin's 16 MiB layout, the last part short",
     up.get("part_size") == PART_SIZE == 16 * 1024 * 1024 and up.get("part_count") == 3
     and [u["size"] for u in urls] == [PART_SIZE, PART_SIZE, len(FRAME) - 2 * PART_SIZE]
     and urls[-1]["size"] < PART_SIZE, str([u["size"] for u in urls]))
case("...and every part URL is absolute (the plugin's client has no base address)",
     all(u["url"].startswith(BASE + "/stack/s3/") for u in urls), str([u["url"][:40] for u in urls]))

etags = []
for u in (urls[2], urls[0], urls[1]):            # out of order
    n = u["part_number"]
    chunk = FRAME[(n - 1) * PART_SIZE:(n - 1) * PART_SIZE + u["size"]]
    status, _, got = http("PUT", u["url"], raw=chunk)       # no Authorization: the URL is the credential
    etag = (got.get("ETag") or "")
    etags.append({"part_number": n, "etag": etag})
    case(f"part {n} is accepted and its ETag is the quoted MD5",
         status == 200 and etag == f'"{hashlib.md5(chunk).hexdigest()}"', f"{status} {etag}")
status, got = call("POST", f"{API}/projects/{PID}/frames/{FID}/complete",
                   {"etags": sorted(etags, key=lambda e: e["part_number"])}, token=GRANT)
case("complete: the frame is uploaded", status == 200 and got.get("status") == "uploaded", str(got)[:160])

# ============================================================= 3. the hub
print("\n-- as the hub: pull, list, fetch, ack --")
status, got = HUB.pull(0)
rows = got.get("changes", [])
kinds = {(r["kind"]) for r in rows}
case("pull sees the project, member, rig and frame change rows",
     status == 200 and {"project", "member", "rig", "frame"} <= kinds, str(sorted(kinds)))
keys = {r["kind"]: r["key"] for r in rows}
case("...keyed by the stacking project and the collab ids, not Starfront's",
     keys.get("project") == PID and keys.get("frame") == f"{PID}/{FID}"
     and keys.get("member") == f"{PID}/{COLLAB_ALICE}" and keys.get("rig") == f"{COLLAB_ALICE}/{RIG}"
     and "1001" not in json.dumps(rows) and ALICE not in json.dumps(rows), str(keys))
HUB.cursor = got["seq"]
status, got = HUB.pull()
case("...and a second pull from the cursor is empty", status == 200 and got["changes"] == [])

status, got, _ = HUB.api("POST", "/records/batch", {"member": [r["key"].split("/") for r in rows if r["kind"] == "member"],
                                                   "rig": [r["key"].split("/") for r in rows if r["kind"] == "rig"]})
case("the hub turns the member and rig rows into full records, with the real display name",
     status == 200 and [m.get("display_name") for m in got.get("member", [])] == ["Alice"] and not got.get("missing")
     and [r.get("rig_id") for r in got.get("rig", [])] == [RIG] and "1001" not in json.dumps(got) and ALICE not in json.dumps(got),
     str(got)[:200])
status, got, _ = HUB.api("GET", f"/records/member/{PID}/{COLLAB_ALICE}")
case("...and a single member read says the same", status == 200 and got.get("display_name") == "Alice" and got.get("status") == "active", str(got)[:160])

status, listed = HUB.uploaded()
frames = listed.get("frames", [])
rec = frames[0] if frames else {}
case("the uploaded frame is listed, by its collab owner",
     status == 200 and [f["frame_id"] for f in frames] == [FID] and rec["user_id"] == COLLAB_ALICE
     and rec["size"] == len(FRAME) and rec["sha256"] == SHA and rec["rig_id"] == RIG
     and rec["header"]["FILTER"] == "Ha" and "1001" not in json.dumps(listed), str(rec)[:200])
ok, body = HUB.fetch(FID, len(FRAME))
case("the bytes come in two Range requests and reassemble", ok and len(body) == len(FRAME))
case("...to the same sha256", hashlib.sha256(body).hexdigest() == SHA)
status, _ = HUB.ack(FID, "0" * 64)
case("an ack with the wrong sha256 is refused", status == 409)
status, _ = http("GET", f"/api/v1/hubs/{HUB.id}/frames?state=uploaded", token=GRANT)[:2]
case("a grant is not a hub token", status in (401, 403, 404), str(status))
status, got = HUB.ack(FID, SHA)
case("the right ack is taken", status == 200 and got.get("ok") is True, str(got))
status, got = HUB.uploaded()
case("the frame is no longer listed", status == 200 and got["frames"] == [])
status, _ = HUB.ack(FID, SHA)
case("a repeated ack is harmless", status == 200)
case("with the default retention the relay copy is still there for now",
     any((RELAY / PID).rglob("*.fits")))

# ============================================================== 4. the push
print("\n-- the hub pushes back --")
HUB.heartbeat(workers=2, version="check")
grid = dict(STORE.stack_project(PID)["doc"]["grid"])
grid.setdefault("rotation_deg", 0.0)
now = iso()
preview = {"full": f"/public/{PID}/previews/HA/live_full.png", "thumb": f"/public/{PID}/previews/HA/live_thumb.png",
           "updated_at": now}
status_doc = {
    "schema_version": 1, "generated_at": now, "access": "public",
    "project": {"project_id": PID, "slug": SLUG, "name": "Relay Check", "target_name": "Relay Check",
                "status": "open", "visibility": "unlisted", "licence": "members_only", "palette": "HOO",
                "filters": ["HA"], "grid": grid},
    "totals": {"frames_accepted": 1, "frames_rejected": 0, "integration_s": 300.0, "streams": 1,
               "sessions": 1, "contributors": 1, "member_count": 1, "last_update": now,
               "gold_seq_max": 1, "rejected_by_reason": {}},
    "streams": [{"stream_key": "HA", "filter": "HA", "sensor_kind": "mono", "bandwidth_nm": None,
                 "frames_accepted": 1, "frames_rejected": 0, "integration_s": 300.0, "contributors": 1,
                 "last_update": now, "gold_seq": 1, "live_n": 1, "median_fwhm_arcsec": 2.9,
                 "preview_urls": {"live": preview, "gold": None},
                 "by_contributor": [{"display_name": "Alice", "frames_accepted": 1, "integration_s": 300.0}]}],
    # A second palette whose channels are complete (RSC contract 1.30.0): its images sit in a folder of their own.
    "combined": {"palette": "HOO", "live": None, "gold": None,
                 "alternates": [{"palette": "LRGB", "gold": None,
                                 "live": {"full": f"/public/{PID}/combined/lrgb/live.png",
                                          "thumb": f"/public/{PID}/combined/lrgb/live_thumb.png", "updated_at": now}}]},
    "coverage": None,
    "contributors": [{"rank": 1, "display_name": "Alice", "integration_s": 300.0, "frames_accepted": 1, "streams": ["HA"]}],
    "timeline": [{"ts": now, "date": now[:10], "kind": "gold_published", "text": "Gold 1 for HA",
                  "frames_accepted": 1, "integration_s": 300.0}],
}
if STATUS_SCHEMA.is_file():
    schema = json.loads(STATUS_SCHEMA.read_text("utf-8"))
    problems = validate(schema, status_doc, schema)
    case("the status document satisfies RSC's status.schema.json", not problems, "; ".join(problems[:3]))
else:
    print(f"note  {STATUS_SCHEMA} is not here: the status document was not held to the schema")

dzi = b'<?xml version="1.0"?><Image xmlns="http://schemas.microsoft.com/deepzoom/2008" Format="jpg" Overlap="1" TileSize="512"><Size Width="8" Height="8"/></Image>'
tar_io = io.BytesIO()
with tarfile.open(fileobj=tar_io, mode="w") as tar:
    for name, data in (("HA/g000001/image.dzi", dzi), ("HA/g000001/image_files/0/0_0.jpg", b"\xff\xd8tile-a"),
                       ("HA/g000001/image_files/1/0_0.jpg", b"\xff\xd8tile-b")):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
PNG = png_bytes()
# Notifications come from the join before this point only; the webhook is set
# now, so the one message that must arrive is the gold.
status, _ = call("PUT", f"/api/v1/projects/{SFP}", {"discordWebhookUrl": HOOK}, token=BOB)
case("the owner points the collaboration's webhook at the fake", status == 200)

verdict = {"frame_id": FID, "project_id": PID, "status": "accepted", "verdict_at": iso(),
           "metrics": {"fwhm_arcsec": 2.9, "eccentricity": 0.31, "stars": 412}, "integration_s": 300.0}
records = {"frames": [verdict], "events": [{"project_id": PID, "ts": iso(), "type": "gold_published",
                                            "text": "Gold 1 published for HA"}],
           "streams": [{"project_id": PID, "stream_key": "HA", "gold_seq": 1, "gold_published_at": iso(),
                        "stats": {"integration_s": 300.0, "contributors": 1, "frames_accepted": 1}}]}
files = [("status.json", json.dumps(status_doc).encode()), ("previews/HA/live_full.png", PNG),
         ("combined/lrgb/live.png", PNG), ("combined/lrgb/live_thumb.png", PNG),
         ("deepzoom.tar", tar_io.getvalue())]
status, got = HUB.push(records, files)
case("the push is taken: a verdict, status.json, three PNGs and two tiles",
     status == 200 and got.get("frames", {}).get("merged") == 1 and got["files"]["written"] == 7, str(got)[:240])
status, again = HUB.push(records, files)
case("the same push again changes nothing", status == 200 and again["files"]["written"] == 0
     and again["frames"]["merged"] == 0, str(again)[:200])
notify.drain()
golds = [m for m in MESSAGES if "Gold integration" in m]
case("exactly one gold message reached the webhook, for the first push only",
     len(MESSAGES) == 1 and len(golds) == 1 and "Gold integration 1 published for HA" in golds[0], str(MESSAGES))

master = os.urandom(3 << 19)                                  # 1.5 MiB
record = {"stream_key": "HA", "gold_seq": 1, "format": "xisf", "integration_s": 300.0, "frames": 1,
          "sessions": 1, "contributors": 1, "licence": "members_only", "created_at": iso()}
status, got = HUB.push({}, [(f"{PID}/masters/HA_g000001_xisf.json", json.dumps(record).encode())])
case("the hub pushes a master's record by the multipart push", status == 200, str(got)[:200])
status, got = HUB.push_master(PID, "HA_g000001_xisf.xisf", master)
case("...and the data file alone by the streamed route", status == 200 and got.get("written") is True, f"{status} {got}")
status, again = HUB.push_master(PID, "HA_g000001_xisf.xisf", master)
case("...sending it again rewrites nothing", status == 200 and again.get("written") is False, f"{status} {again}")
status, got = call("GET", f"{API}/projects/{PID}/masters", token=GRANT)
listed = got.get("masters", [])
case("a member's masters list shows the master", status == 200 and len(listed) == 1
     and listed[0]["size_bytes"] == len(master) and listed[0]["stream_key"] == "HA", str(got)[:240])
status, body, _ = http("GET", listed[0]["url"], token=GRANT)
case("...and the member downloads exactly the bytes the hub sent", status == 200 and body == master, str(status))

# ============================================================== 5. the pages
print("\n-- the pages see it --")
status, got = call("GET", f"{API}/projects/{PID}/status")
case("the status route returns the pushed document", status == 200 and got["status_doc"] == status_doc)
status, body, head = http("GET", f"/stack/public/{PID}/status.json")
case("status.json is served with a minute of cache", status == 200 and body == status_doc
     and head["Cache-Control"] == "public, max-age=60" and head["X-Content-Type-Options"] == "nosniff", str(dict(head))[:200])
status, body, head = http("GET", f"/stack/public/{PID}/previews/HA/live_full.png")
case("the PNG is served as an image with a minute of cache", status == 200 and body == PNG
     and head["Content-Type"] == "image/png" and head["Cache-Control"] == "public, max-age=60")
status, body, head = http("GET", f"/stack/public/{PID}/combined/lrgb/live.png")
case("a second palette's image, in its own folder, is served like the first",
     status == 200 and body == PNG and head["Content-Type"] == "image/png", str(status))
status, body, head = http("GET", f"/stack/public/{PID}/deepzoom/HA/g000001/image_files/1/0_0.jpg")
case("a deep-zoom tile is cached for a year, immutable", status == 200 and body == b"\xff\xd8tile-b"
     and head["Cache-Control"] == "public, max-age=31536000, immutable", str(head.get("Cache-Control")))
status, _, _ = http("GET", f"/stack/public/{PID}/../../collab.sqlite")
case("the public tree does not lead out of itself", status == 404)
status, page, head = http("GET", f"/stack/p/{SLUG}")
case("the project page is HTML with the CSP", status == 200 and b"<html" in page.lower()
     and "text/html" in head["Content-Type"] and "script-src 'self'" in head["Content-Security-Policy"]
     and head["Cache-Control"] == "no-cache", str(head.get("Content-Security-Policy")))
status, got = call("GET", f"{API}/projects/{PID}/frames?mine=1", token=GRANT)
mine = got.get("frames", [{}])
case("the contributor sees the verdict on their frame (frames?mine=1 with the grant)",
     status == 200 and len(mine) == 1 and mine[0]["frame_id"] == FID and mine[0]["status"] == "accepted"
     and mine[0]["metrics"]["fwhm_arcsec"] == 2.9, str(mine)[:200])
status, got = call("GET", f"{API}/projects/{PID}/events")
case("the events feed has the gold event", status == 200 and [e["type"] for e in got["events"]] == ["gold_published"])

COOKIE = browser_cookie("code-alice")
status, got = call("GET", f"{API}/projects?scope=mine", cookie=COOKIE)
item = next((p for p in got.get("projects", []) if p["project_id"] == PID), {})
by = item.get("stacked_by") or {}
case("the dashboard (cookie) lists the project with the hub's heartbeat state",
     status == 200 and by.get("hub_name") == "Relay hub" and by.get("workers") == 2 and by.get("online") is True
     and by.get("last_seen"), str(item)[:240])
case("...with no join code and the pushed counts",
     "join_code" not in item and item["counts"]["frames_accepted"] == 1 and item["counts"]["integration_s"] == 300.0,
     str(item.get("counts")))
status, got = call("GET", f"{API}/auth/me", cookie=COOKIE)
case("...and the dashboard knows the person as the collab id", status == 200 and got["user"]["user_id"] == COLLAB_ALICE)

# ========================================================== 6. retention
print("\n-- retention --")
os.environ["STARFRONT_STACK_RELAY_KEEP_DAYS"] = "0"
BLOCKS2 = 4
FRAME2 = fits_bytes(BLOCKS2, 2)
SHA2 = hashlib.sha256(FRAME2).hexdigest()
status, init = call("POST", f"{API}/projects/{PID}/frames/init", {
    "sha256": SHA2, "size": len(FRAME2), "filename": "Orion_Ha_300s_0002.fits", "stripped": True,
    "header": frame_header(BLOCKS2, iso(-3000)), "rig_hint": {"rig_id": RIG}}, token=GRANT)
FID2 = init.get("frame_id", "")
part = init["upload"]["part_urls"][0]
_, _, got = http("PUT", part["url"], raw=FRAME2)
status, _ = call("POST", f"{API}/projects/{PID}/frames/{FID2}/complete",
                 {"etags": [{"part_number": 1, "etag": got.get("ETag")}]}, token=GRANT)
case("a second, one-part frame is uploaded", status == 200 and FID2)
file2 = next((RELAY / PID).rglob(f"{FID2}.fits"), None)
case("...and its relay copy is on disk", file2 is not None and file2.stat().st_size == len(FRAME2))
_, listed = HUB.uploaded()
ok, body = HUB.fetch(FID2, len(FRAME2))
case("the hub lists and fetches it", [f["frame_id"] for f in listed["frames"]] == [FID2] and ok
     and hashlib.sha256(body).hexdigest() == SHA2)
status, _ = HUB.ack(FID2, SHA2)
case("the ack is taken", status == 200)
status, _, _ = HUB.api("GET", f"/frames/{FID2}/bytes")
case("with KEEP_DAYS=0 the relay bytes are gone at the ack (404)", status == 404 and not file2.exists(), str(status))
status, _ = HUB.push({"frames": [{"frame_id": FID2, "project_id": PID, "status": "rejected", "verdict_at": iso(),
                                  "reject_reason": "poor_fit", "reject_detail": "stars did not fit"}]}, [])
case("the hub can still push a verdict for a frame whose bytes are gone", status == 200)
status, got = call("GET", f"{API}/projects/{PID}/frames?mine=1", token=GRANT)
by_id = {f["frame_id"]: f for f in got.get("frames", [])}
case("...the row and its verdicts remain for the contributor",
     status == 200 and by_id.get(FID2, {}).get("status") == "rejected"
     and by_id[FID2]["reject_reason"] == "poor_fit" and by_id[FID]["status"] == "accepted"
     and by_id[FID2]["size"] == len(FRAME2) and by_id[FID2]["sha256"] == SHA2, str(by_id)[:240])
case("the first frame's file (kept 7 days) is untouched by the second's retention",
     any((RELAY / PID).rglob(f"{FID}.fits")))

# ====================================================== 7. deleting it (T704)
print("\n-- deleting the collaboration --")
HUB.pull()                                    # drain, so what follows is what deleting added
status, got = call("DELETE", f"/api/v1/projects/{SFP}", token=BOB)
case("the owner deletes the collaboration", status == 200 and got["deletion"]["status"] == "deleted", str(got)[:160])
status, got = HUB.pull()
case("the hub's pull carries a project row for it", status == 200
     and [r["key"] for r in got["changes"] if r["kind"] == "project"] == [PID], str(got)[:200])
status, got = call("GET", f"{API}/projects/{PID}", token=GRANT)
case("...and reading the project (what the hub does next) shows it deleted with its dates",
     status == 200 and got["project"]["status"] == "deleted" and got["project"]["delete_after"]
     and got["project"]["deleted_at"], str(got)[:200])
status, got = call("POST", f"{API}/projects/{PID}/frames/init", {
    "sha256": "0" * 64, "size": 4, "filename": "late.fits", "stripped": True,
    "header": frame_header(1, iso(-10))}, token=GRANT)
case("a late upload is refused with project_deleted (409)", status == 409 and got.get("code") == "project_deleted", str(got)[:160])
status, got = call("POST", f"/api/v1/projects/{SFP}/restore", token=BOB)
case("the owner restores it and the hub is told again", status == 200 and got["deletion"]["status"] == "open"
     and [r["key"] for r in HUB.pull()[1]["changes"] if r["kind"] == "project"] == [PID], str(got)[:160])

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
