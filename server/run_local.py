#!/usr/bin/env python3
"""Run the collaboration server on this machine, with a stand-in for Discord.

    python server/run_local.py
    python server/run_local.py --lan

For trying the server, the Starfront program and the N.I.N.A. plugin out
locally. The real server signs people in through Discord, which needs a
Discord application with a client secret and a redirect Discord will accept.
This runs the same server with a small Discord of its own: the sign-in page
asks only *which name*, and anyone who presses Continue is signed in as that
person. The name given with --name (ncedelliott unless told otherwise) owns
the server, so the first sign-in can do everything; any other name typed on
that page is an ordinary member, for seeing what somebody else's telescope
sees.

    --name NAME     the owner's Discord name (default ncedelliott)
    --port N        listen port (default 8800)
    --data DIR      where the database and token live (default ~/starfront-local)
    --lan           listen on the whole network (0.0.0.0), so another PC on the
                    same home network, such as the Windows PC running Starfront,
                    can reach the server directly
    --public-host H with --lan, the name other PCs use (e.g. omarchy.lan), instead
                    of this machine's first LAN address
    --nina-dir DIR  a folder of plugin builds (zip + manifest) for the /nina feed
                    to offer instead of server/nina_plugin: a test build for the
                    PCs on this network. Its install-nina-plugin.ps1, if it has
                    one, is served at /_local/install-nina-plugin.ps1 with this
                    server's address filled in, so a PC that runs N.I.N.A. needs
                    one line: irm <server>/_local/install-nina-plugin.ps1 | iex

The stand-in is served by the server itself, under /_local/discord, so there
is one address to reach. By default that is this machine only (127.0.0.1); an
SSH tunnel to 8800 is the alternative for another PC. With --lan the server
listens on every interface and hands out this machine's LAN address (or its
.local name if it has none), so device sign-in links and the /stack/api base
work from the other PC. Anyone on that network can then sign in as anyone.

**Never on a real server's data.** The stand-in signs in whoever asks, as the
owner if they like. A folder with a discord.env in it, or the default folder a
real server would use, is refused, with or without --lan: signing in to a real
database through a door with no lock is exactly the mistake this must not make
easy. --lan is refused unless --data is a folder of its own (the default
~/starfront-local, or one passed explicitly) that is not inside a real server's
folder. Discord settings already in the environment (a shell that has the real
secret exported) are overridden, never mixed in.
"""

# No `from __future__ import annotations`: FastAPI reads the stand-in's
# parameter types at runtime, and `Request` is imported inside the function.
import argparse
import hashlib
import html
import os
import re
import secrets
import socket
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Where the stand-in Discord lives, on the server's own address.
PREFIX = "/_local/discord"

#: The guild every stand-in person is a member of.
GUILD = "local"

#: What a Discord username may be; checked before anything is echoed back.
_NAME = re.compile(r"^[a-z0-9_.]{2,32}$")


def person_id(name: str) -> str:
    """A Discord-like id that is the same for the same name every run, so a
    restart finds the same person (and their projects) in the database."""
    digest = int(hashlib.sha256(name.encode()).hexdigest()[:15], 16)
    return str(900_000_000_000_000_000 + digest % 10**17)


def stand_in_discord(secret: str, callback: str, owner: str):
    """Enough of Discord for a sign-in: the authorize page, the code exchange,
    who-am-I and the guild membership lookup (server/auth.py's three calls)."""
    from fastapi import APIRouter, HTTPException, Header, Query, Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from server import auth

    router = APIRouter(prefix=PREFIX)
    codes: dict[str, str] = {}       # authorization code -> name, used once
    bearers: dict[str, str] = {}     # access token -> name

    def back(redirect_uri: str, **query: str) -> RedirectResponse:
        # Only ever back to this server's own callback: a stand-in that sent
        # people wherever the query said would be an open redirect.
        if redirect_uri != callback:
            raise HTTPException(status_code=400, detail="unknown redirect_uri")
        return RedirectResponse(f"{callback}?{urllib.parse.urlencode(query)}",
                                status_code=303)

    @router.get("/oauth2/authorize")
    async def authorize(redirect_uri: str = Query(default=""),
                        state: str = Query(default="", max_length=64)):
        if redirect_uri != callback:
            raise HTTPException(status_code=400, detail="unknown redirect_uri")
        hidden = (f'<input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri)}">'
                  f'<input type="hidden" name="state" value="{html.escape(state)}">')
        body = (
            "This is the stand-in for Discord that <code>server/run_local.py</code> runs. "
            f"<b>@{html.escape(owner)}</b> owns this server; any other name is an "
            "ordinary member.</p>"
            f'<form action="{PREFIX}/oauth2/approve" method="get">{hidden}'
            f'<p><input name="name" value="{html.escape(owner)}" size="20" '
            'pattern="[a-z0-9_.]{2,32}" autofocus> '
            '<button type="submit">Continue</button></p></form>'
            f'<form action="{PREFIX}/oauth2/deny" method="get">{hidden}'
            '<p><button type="submit">Cancel</button>')
        return HTMLResponse(auth.page("Sign in to Starfront (local)", body))

    @router.get("/oauth2/approve")
    async def approve(redirect_uri: str = Query(default=""),
                      state: str = Query(default="", max_length=64),
                      name: str = Query(default="")):
        name = name.strip().lstrip("@").lower()
        if not _NAME.match(name):
            raise HTTPException(status_code=400,
                                detail="a name is 2 to 32 of a-z, 0-9, _ and .")
        code = secrets.token_urlsafe(16)
        codes[code] = name
        return back(redirect_uri, code=code, state=state)

    @router.get("/oauth2/deny")
    async def deny(redirect_uri: str = Query(default=""),
                   state: str = Query(default="", max_length=64)):
        return back(redirect_uri, error="access_denied", state=state)

    @router.post("/oauth2/token")
    async def token(request: Request):
        form = dict(urllib.parse.parse_qsl((await request.body()).decode()))
        name = codes.pop(form.get("code", ""), None)
        if form.get("client_secret") != secret or name is None \
                or form.get("redirect_uri") != callback:
            raise HTTPException(status_code=400, detail="invalid_grant")
        bearer = secrets.token_urlsafe(24)
        bearers[bearer] = name
        return {"access_token": bearer, "token_type": "Bearer"}

    def who(authorization: str) -> str:
        name = bearers.get(authorization.removeprefix("Bearer ").strip())
        if name is None:
            raise HTTPException(status_code=401, detail="401: Unauthorized")
        return name

    @router.get("/users/@me")
    async def me(authorization: str = Header(default="")):
        name = who(authorization)
        return {"id": person_id(name), "username": name, "global_name": None,
                "avatar": ""}

    @router.get("/users/@me/guilds/{guild}/member")
    async def member(guild: str, authorization: str = Header(default="")):
        who(authorization)
        if guild != GUILD:
            raise HTTPException(status_code=404, detail="Unknown Guild")
        return {"roles": [], "nick": None}

    return router


#: What install-nina-plugin.ps1 has where this server's address goes.
SERVER_PLACEHOLDER = "__STARFRONT_SERVER__"


def plugin_installer(nina_dir: Path, public: str):
    """The test plugin's install script, with this server's address in it.
    Read on each request, so a new copy needs no restart; only the address is
    filled in, nothing a request sends."""
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import PlainTextResponse

    router = APIRouter(prefix="/_local")

    @router.get("/install-nina-plugin.ps1")
    async def install_script():
        path = nina_dir / "install-nina-plugin.ps1"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no install script in --nina-dir")
        text = path.read_text(encoding="utf-8").replace(SERVER_PLACEHOLDER, public)
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    return router


def lan_addresses() -> list[str]:
    """This machine's IPv4 addresses on the network, the one the route out
    uses first. Loopback and link-local (169.254.x.x) are not reachable from
    another PC, so they are left out. Empty when there is no network."""
    found: list[str] = []
    try:
        # A UDP connect sends nothing; it only asks the system which local
        # address it would route from. No network makes it fail, which is fine.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            found.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    usable = [a for a in dict.fromkeys(found)
              if not a.startswith(("127.", "169.254.", "0."))]
    return usable


def local_name() -> str:
    """This machine's .local (mDNS) name, which another PC can often use when
    the address is not known."""
    return socket.gethostname().split(".")[0] + ".local"


def refuse(message: str) -> None:
    print(f"run_local: {message}", file=sys.stderr)
    sys.exit(2)



def missing_requirements() -> list[str]:
    """The server's requirements this Python lacks. python-multipart above all:
    without it the hub's results push is refused (400 "not multipart") and no
    preview ever reaches the pages, which otherwise shows only as a stack that
    never appears."""
    import importlib.util
    wanted = {"fastapi": "fastapi", "uvicorn": "uvicorn", "python_multipart": "python-multipart"}
    return [pip for mod, pip in wanted.items() if importlib.util.find_spec(mod) is None]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Starfront collaboration server, locally, with a stand-in Discord")
    parser.add_argument("--name", default="ncedelliott",
                        help="the owner's Discord name (default ncedelliott)")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--data", default=str(Path.home() / "starfront-local"),
                        help="data folder (default ~/starfront-local)")
    parser.add_argument("--lan", action="store_true",
                        help="listen on the network (0.0.0.0) for another PC to reach")
    parser.add_argument("--public-host", default="",
                        help="with --lan, the name other PCs reach this machine by (e.g. omarchy.lan) "
                             "instead of its first LAN address, which can change on a machine with "
                             "two network adapters or no DHCP reservation")
    parser.add_argument("--nina-dir", default="",
                        help="a folder of N.I.N.A. plugin builds for the /nina feed, instead of "
                             "server/nina_plugin; its install-nina-plugin.ps1 is served at "
                             "/_local/install-nina-plugin.ps1")
    args = parser.parse_args(argv)
    if args.public_host and not args.lan:
        parser.error("--public-host needs --lan")
    nina_dir = Path(args.nina_dir).expanduser().resolve() if args.nina_dir else None
    if nina_dir is not None and not nina_dir.is_dir():
        parser.error(f"--nina-dir {args.nina_dir} is not a folder")
    if args.public_host and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", args.public_host):
        parser.error("--public-host is a host name or address, e.g. omarchy.lan")
    missing = missing_requirements()
    if missing:
        refuse(f"this Python ({sys.executable}) lacks {', '.join(missing)}: "
               f"run {sys.executable} -m pip install -r requirements.txt")

    owner = args.name.strip().lstrip("@").lower()
    if not _NAME.match(owner):
        refuse("--name is 2 to 32 of a-z, 0-9, _ and .")

    data = Path(args.data).expanduser().resolve()
    from server import auth
    if (data / auth.ENV_FILE).exists():
        refuse(f"{data} has a {auth.ENV_FILE}, so it is a real server's folder."
               " Give --data a folder of its own.")
    from astrocontrol.config import data_root
    if data == (data_root() / "collab").resolve():
        refuse(f"{data} is where a real server keeps its database."
               " Give --data a folder of its own.")
    if args.lan:
        # Both refusals above already ran. The one more: a --data folder inside
        # a real server's folder is not a test folder of its own.
        real_folder = data_root() / "collab"
        if data == real_folder.resolve() or real_folder.resolve() in data.parents:
            refuse(f"{data} is inside where a real server keeps its database."
                   " Give --data a folder of its own.")
    data.mkdir(parents=True, exist_ok=True)

    # The server's own calls to the stand-in always go to loopback. What a
    # browser is sent to (sign-in page, callback, the public address) is the
    # address another PC can reach when --lan is given.
    base = f"http://127.0.0.1:{args.port}"
    public = base
    lan_ips = lan_addresses() if args.lan else []
    if args.lan:
        host = args.public_host or (lan_ips[0] if lan_ips else local_name())
        public = f"http://{host}:{args.port}"
    secret = secrets.token_urlsafe(24)
    # Whatever the shell had, under either name, gives way: a real client
    # secret or guild must not be mixed with the stand-in.
    for key in list(os.environ):
        if key.startswith(("STARFRONT_DISCORD_", "ASTROCOLLAB_DISCORD_",
                           "ASTROCOLLAB_PUBLIC_URL", "ASTROCOLLAB_DATA")):
            del os.environ[key]
    os.environ.update({
        "STARFRONT_DATA": str(data),
        "STARFRONT_PUBLIC_URL": public,
        "STARFRONT_DISCORD_CLIENT_ID": "starfront-local",
        "STARFRONT_DISCORD_CLIENT_SECRET": secret,
        "STARFRONT_DISCORD_GUILD": GUILD,
        "STARFRONT_DISCORD_OWNERS": person_id(owner),
        "STARFRONT_DISCORD_API": base + PREFIX,
        "STARFRONT_DISCORD_AUTHORIZE": public + PREFIX + "/oauth2/authorize",
    })
    if nina_dir is not None:
        os.environ.pop("ASTROCOLLAB_NINA_DIR", None)
        os.environ["STARFRONT_NINA_DIR"] = str(nina_dir)

    # The server reads its settings when it is imported, so only now; the
    # routes are added to the same app object run.py then serves.
    from server import app as server_app
    server_app.app.include_router(stand_in_discord(
        secret, public + "/auth/discord/callback", owner))
    if nina_dir is not None:
        server_app.app.include_router(plugin_installer(nina_dir, public))
        print(f"N.I.N.A. plugin feed from {nina_dir}; install script at "
              f"{public}/_local/install-nina-plugin.ps1")

    if args.lan:
        print("=" * 72)
        print("  WARNING: --lan. Anyone on this network who can reach the port")
        print("  can sign in as ANYONE, including the owner. Local testing only.")
        print("  Stop the server (Ctrl+C) when the test is over.")
        print("=" * 72)
        print("Reach it from another PC on this network at:")
        if args.public_host:
            print(f"  http://{args.public_host}:{args.port}   (what sign-in links use)")
        for ip in lan_ips:
            print(f"  http://{ip}:{args.port}")
        print(f"  http://{local_name()}:{args.port}")
        print(f"The server listens on 0.0.0.0:{args.port} (every interface).\n")
    print(f"Stand-in Discord at {public}{PREFIX}: signing in asks only for a name.")
    print(f"  @{owner} owns this server; any other name is an ordinary member.")
    if not args.lan:
        print("  Local use only: anyone who can reach this port can sign in as anyone.\n")
    from server import run
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    run.main(["--host", host, "--port", str(args.port)])


if __name__ == "__main__":
    main()
