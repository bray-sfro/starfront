"""Who a person is: Discord sign-in for the collaboration server.

Telescopes have tokens; people have Discord. A collaboration is a Discord
server's worth of people, so membership of that server *is* the account: no
passwords here, no registration, nothing to reset. Somebody signs in with
Discord once from Starfront, the server checks they are in the guild (and,
if the coordinator wants, that they hold a role), and hands back a user token
their program keeps.

**The device-code flow, because Starfront is a desktop program.** A browser
redirect cannot land back inside a desktop window on a port that changes every
launch, so it is done the way televisions do it: Starfront asks this server for
a short *login code* and opens the browser on it; the person signs in with
Discord in the browser; the server binds their identity to that code; Starfront,
polling with the code, is handed the user token. Nothing is copied by hand and
the client secret never leaves this machine.

Discord's API base is overridable so the whole flow can be exercised against a
fake Discord in a test, which is the only way the callback gets tested at all.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The file the deploy script writes. Beside the database and the admin token,
#: outside any synchronised folder, because the client secret is a credential.
ENV_FILE = "discord.env"

#: What we ask Discord for. `identify` is who they are; `guilds.members.read`
#: is whether they are in the coordinator's server and which roles they hold.
#: Nothing else - no email, no DMs, no bot.
SCOPES = "identify guilds.members.read"

#: How long a login code is good for once Starfront has shown it. Long enough
#: to find the browser window and press the button; short enough that a code
#: left on a screen is not a way in tomorrow.
LOGIN_CODE_SECONDS = 600.0


@dataclass
class DiscordSettings:
    client_id: str = ""
    client_secret: str = ""
    guild: str = ""
    #: A role somebody must hold to *start* collaborations. Blank: any member
    #: of the guild may. Joining a collaboration never needs a role - that is
    #: what enrolling a telescope is for.
    role: str = ""
    #: Discord user ids who own this server: they may do anything, the same
    #: as the admin token, but sign in like everybody else. The way the
    #: person running the server gets their powers without a token to paste.
    owners: list[str] = field(default_factory=list)
    #: Where this server is reachable from the outside, for the redirect URL.
    public_url: str = ""
    #: Discord's API. Overridden in tests.
    api: str = "https://discord.com/api/v10"
    authorize: str = "https://discord.com/oauth2/authorize"

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.guild
                    and self.public_url)

    def redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + "/auth/discord/callback"


def load_env_file(folder: Path) -> None:
    """Put `discord.env` into the environment, without overriding what is set.

    A service definition may already carry these; the file is for the case
    where it does not, which is most installs.
    """
    path = folder / ENV_FILE
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def settings_from_env() -> DiscordSettings:
    env = os.environ
    return DiscordSettings(
        client_id=env.get("ASTROCOLLAB_DISCORD_CLIENT_ID", "").strip(),
        client_secret=env.get("ASTROCOLLAB_DISCORD_CLIENT_SECRET", "").strip(),
        guild=env.get("ASTROCOLLAB_DISCORD_GUILD", "").strip(),
        role=env.get("ASTROCOLLAB_DISCORD_ROLE", "").strip(),
        owners=[part.strip() for part in
                env.get("ASTROCOLLAB_DISCORD_OWNERS", "").replace(";", ",").split(",")
                if part.strip()],
        public_url=env.get("ASTROCOLLAB_PUBLIC_URL", "").strip(),
        api=env.get("ASTROCOLLAB_DISCORD_API", "https://discord.com/api/v10").rstrip("/"),
        authorize=env.get("ASTROCOLLAB_DISCORD_AUTHORIZE",
                          "https://discord.com/oauth2/authorize"),
    )


class AuthError(RuntimeError):
    """Discord said no, or the person is not who the guild wants."""


@dataclass
class Identity:
    """What Discord told us about somebody, reduced to what the server keeps."""

    id: str
    name: str
    avatar: str = ""
    roles: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "avatar": self.avatar,
                "roles": list(self.roles)}


class Discord:
    """The three calls to Discord that a sign-in takes."""

    def __init__(self, settings: DiscordSettings) -> None:
        self.settings = settings

    def authorize_url(self, state: str) -> str:
        query = urllib.parse.urlencode({
            "client_id": self.settings.client_id,
            "response_type": "code",
            "redirect_uri": self.settings.redirect_uri(),
            "scope": SCOPES,
            "state": state,
            "prompt": "none",
        })
        return f"{self.settings.authorize}?{query}"

    def _post_form(self, path: str, form: dict[str, str]) -> dict[str, Any]:
        data = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(
            f"{self.settings.api}{path}", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "Starfront collaboration server"})
        return self._send(request)

    def _get(self, path: str, bearer: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.settings.api}{path}", method="GET",
            headers={"Authorization": f"Bearer {bearer}",
                     "User-Agent": "Starfront collaboration server"})
        return self._send(request)

    @staticmethod
    def _send(request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise AuthError(f"Discord answered {exc.code}: {detail}") from exc
        except Exception as exc:                  # noqa: BLE001
            raise AuthError(f"Discord could not be reached: {exc}") from exc
        try:
            return json.loads(text) if text else {}
        except ValueError as exc:
            raise AuthError("Discord answered with something that is not JSON") from exc

    def exchange(self, code: str) -> str:
        """Turn the code Discord sent back into a bearer token for its API."""
        answer = self._post_form("/oauth2/token", {
            "client_id": self.settings.client_id,
            "client_secret": self.settings.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri(),
        })
        token = str(answer.get("access_token") or "")
        if not token:
            raise AuthError("Discord did not hand back an access token")
        return token

    def identify(self, bearer: str) -> Identity:
        """Who this is, and whether they are in the guild.

        Membership is the account. Somebody not in the coordinator's Discord
        server is turned away here, with a message that says so, rather than
        let in as a stranger.
        """
        me = self._get("/users/@me", bearer)
        user_id = str(me.get("id") or "")
        if not user_id:
            raise AuthError("Discord did not say who you are")
        name = str(me.get("global_name") or me.get("username") or user_id)
        avatar = str(me.get("avatar") or "")
        try:
            member = self._get(f"/users/@me/guilds/{self.settings.guild}/member", bearer)
        except AuthError as exc:
            raise AuthError("you are not a member of this collaboration's Discord "
                            f"server ({exc})") from exc
        roles = [str(role) for role in (member.get("roles") or [])]
        nick = member.get("nick")
        if nick:
            name = str(nick)
        return Identity(id=user_id, name=name, avatar=avatar, roles=roles)


def new_login_code() -> str:
    """Short, unambiguous, typed by nobody: it travels in a URL and a poll."""
    return secrets.token_urlsafe(9)


def new_user_token() -> str:
    return secrets.token_urlsafe(32)


def expired(created: float) -> bool:
    return time.time() - float(created or 0.0) > LOGIN_CODE_SECONDS


# ---------------------------------------------------------------------------
# The two pages a person sees in the browser
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Starfront</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ margin: 0; background: #0d1017; color: #dbe3ee; font: 16px/1.5
         system-ui, sans-serif; display: grid; place-items: center;
         min-height: 100vh; }}
  main {{ max-width: 420px; padding: 32px; text-align: center; }}
  h1 {{ font-size: 22px; margin: 0 0 12px; }}
  p {{ color: #9aa5b8; margin: 8px 0; }}
  .ok {{ color: #7ee7a5; }} .no {{ color: #ff8a7a; }}
</style></head><body><main>
<h1 class="{tone}">{title}</h1>
<p>{body}</p>
</main></body></html>"""


def page(title: str, body: str, ok: bool = True) -> str:
    return _PAGE.format(title=title, body=body, tone="ok" if ok else "no")
