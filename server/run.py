#!/usr/bin/env python3
"""Start the collaboration server.

    python server/run.py

That is the whole thing. It makes up a coordinator token on the first run,
keeps it beside the database, and prints it so it can be pasted into the Collab
tab. Nothing to configure and nothing to remember.

    --host 0.0.0.0    accept from other machines on the network as well

Bound to this machine only unless that flag is given. A telescope on another PC
needs it; nothing else does, and a service that quietly listened to the network
on somebody's behalf would be a poor default.

Environment, where any of it needs overriding:
    ASTROCOLLAB_DATA          where the database and the token live
    ASTROCOLLAB_ADMIN_TOKEN   the coordinator's credential, if it should come
                              from somewhere else - a password manager, or a
                              service definition
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: The token file, beside the database. Deliberately *not* in the program
#: folder: that one is synchronised to Dropbox, and a credential that can
#: rewrite every project on the server has no business in a synchronised folder
#: or in a launcher script somebody might share.
TOKEN_FILE = "admin-token.txt"


def data_dir() -> Path:
    """Where the server keeps things. The same answer `server.app` will reach."""
    override = os.environ.get("ASTROCOLLAB_DATA")
    if override:
        return Path(override)
    from astrocontrol.config import data_root
    return data_root() / "collab"


def admin_token(folder: Path) -> tuple[str, bool]:
    """The coordinator's credential, made up once and kept.

    Returns the token and whether it had to be created. An environment variable
    wins where one is set, so a service definition or a password manager can own
    it instead; otherwise it lives in a file, because a token that changed every
    time the server restarted would have to be re-pasted into the Collab tab
    every time, and would be abandoned within a week.
    """
    from_env = os.environ.get("ASTROCOLLAB_ADMIN_TOKEN", "").strip()
    if from_env:
        return from_env, False

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / TOKEN_FILE
    if path.is_file():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored, False

    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        # Readable by this account and nobody else. Best effort: this is a
        # nicety on Windows, where the ACL does the real work.
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return token, True


def main() -> None:
    parser = argparse.ArgumentParser(description="Starfront collaboration server")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; 0.0.0.0 to accept from other machines")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--reload", action="store_true", help="development only")
    parser.add_argument("--show-token", action="store_true",
                        help="print the coordinator token and exit")
    args = parser.parse_args()

    folder = data_dir()
    token, fresh = admin_token(folder)
    os.environ["ASTROCOLLAB_ADMIN_TOKEN"] = token

    if args.show_token:
        print(token)
        return

    from server import auth
    auth.load_env_file(folder)
    discord = auth.settings_from_env()

    print("Starfront collaboration server")
    print(f"  data      {folder}")
    print(f"  listening http://{args.host}:{args.port}")
    if args.host == "127.0.0.1":
        print("            (this machine only - use --host 0.0.0.0 for the network,"
              " or put Caddy in front of it)")
    if discord.configured():
        print(f"  discord   sign-in on, guild {discord.guild}"
              + (f", role {discord.role} to start projects" if discord.role else "")
              + f", public at {discord.public_url}")
    else:
        print(f"  discord   not set up - owner only. See {folder / auth.ENV_FILE}"
              " (server/deploy/discord.env.example)")
    print()
    # The token is shown on a console somebody is looking at, never into a
    # service's log: a journal is read by more people than the one it was
    # made for, and lives longer.
    if sys.stdout.isatty():
        print("An owner token has been made up for you and saved:" if fresh
              else "Your owner token:")
        print(f"\n    {token}\n")
        print("For scripts and emergencies; people sign in with Discord, and "
              "owners are the Discord ids in discord.env.")
    else:
        print("Owner token: not shown here (no console) -"
              " `python server/run.py --show-token` prints it.")
    print(f"It is kept in {folder / TOKEN_FILE} and will not change.\n")

    import uvicorn
    uvicorn.run("server.app:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
