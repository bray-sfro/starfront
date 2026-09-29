"""Where the server keeps things.

SQLite, on purpose.  This is a few hundred people, a few thousand tasks and
some tens of thousands of contribution rows — a scale at which a single file is
not a compromise but the right answer, and one you can back up by copying it.
If it ever stops being enough, the schema here moves to Postgres unchanged.

Every row that crosses the wire is stored as JSON in a `payload` column
alongside the handful of fields worth indexing.  That is deliberate: the shape
of a task and a contribution is defined in `astrocontrol.collab` and will keep
moving while this is being built, and a migration per field would slow that to
a crawl.  The columns exist for the questions the server actually asks — whose
task is this, which project, what night — and nothing else.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    token       TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    owner       TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL,
    seen        REAL NOT NULL DEFAULT 0,
    profile     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    coordinator TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    agent       TEXT NOT NULL,
    state       TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    issued      REAL NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_by_agent ON tasks (agent, state);

CREATE TABLE IF NOT EXISTS contributions (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    agent       TEXT NOT NULL,
    task        TEXT NOT NULL DEFAULT '',
    night       TEXT NOT NULL DEFAULT '',
    filter      TEXT NOT NULL DEFAULT '',
    seconds     REAL NOT NULL DEFAULT 0,
    accepted    INTEGER NOT NULL DEFAULT 0,
    overridden  INTEGER NOT NULL DEFAULT 0,
    received    REAL NOT NULL,
    payload     TEXT NOT NULL,
    verdict     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS contributions_by_project ON contributions (project);

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    token       TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    avatar      TEXT NOT NULL DEFAULT '',
    roles       TEXT NOT NULL DEFAULT '[]',
    created     REAL NOT NULL,
    seen        REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS logins (
    code        TEXT PRIMARY KEY,
    created     REAL NOT NULL,
    user        TEXT NOT NULL DEFAULT '',
    claimed     INTEGER NOT NULL DEFAULT 0
);
"""

#: Run after the schema, on a database that may predate the `panel` column.
#: One report per agent per night per filter per *panel* per task. A sequence
#: that ends twice, or an agent that retries a report it already sent, must not
#: count the same hours again - which is the one way a ledger silently becomes
#: wrong. The panel is part of the key because twelve panels shot through Ha
#: on one night are twelve contributions, not one and eleven duplicates; the
#: first version keyed on the filter alone and would have thrown away every
#: panel after the first.
MIGRATE = """
DROP INDEX IF EXISTS contributions_once;
CREATE UNIQUE INDEX IF NOT EXISTS contributions_once_per_panel
    ON contributions (agent, task, night, filter, panel);
"""


class Store:
    """The database, behind a lock.

    One connection shared across threads with a lock around it, rather than a
    connection per thread. At this traffic the lock is free and it removes a
    whole class of "which connection has the write" confusion.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # Write-ahead logging: readers do not block the writer, which is what
        # keeps a polling agent from ever waiting on a coordinator's edit.
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            # Columns added since the first release, on a database that may
            # not have them. SQLite has no ADD COLUMN IF NOT EXISTS.
            have = {row[1] for row in self._db.execute(
                "PRAGMA table_info(contributions)")}
            if "panel" not in have:
                self._db.execute("ALTER TABLE contributions ADD COLUMN panel "
                                 "TEXT NOT NULL DEFAULT ''")
            # Who a thing belongs to, as a Discord user id. `owner` and
            # `coordinator` hold a *name* for display; this is what
            # permissions are decided on, and a name is not that.
            for table in ("agents", "projects"):
                columns = {row[1] for row in self._db.execute(
                    f"PRAGMA table_info({table})")}
                if "owner_id" not in columns:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN owner_id "
                                     "TEXT NOT NULL DEFAULT ''")
            # Where each telescope is pointing and what it is doing, as it
            # last said: what the group sees of each other on the chart.
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(agents)")}
            if "presence" not in columns:
                self._db.execute("ALTER TABLE agents ADD COLUMN presence "
                                 "TEXT NOT NULL DEFAULT '{}'")
            self._db.executescript(MIGRATE)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- helpers -----------------------------------------------------------
    def _run(self, sql: str, *args: Any) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._db.execute(sql, args)
            self._db.commit()
            return cursor

    def _all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, args)]

    def _one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        rows = self._all(sql, *args)
        return rows[0] if rows else None

    # -- agents ------------------------------------------------------------
    def add_agent(self, agent_id: str, token: str, name: str,
                  owner: str = "", owner_id: str = "") -> dict[str, Any]:
        self._run(
            "INSERT INTO agents (id, token, name, owner, owner_id, created, seen,"
            " profile) VALUES (?, ?, ?, ?, ?, ?, 0, '{}')",
            agent_id, token, name, owner, owner_id, time.time())
        return self.agent(agent_id)

    def agents_of(self, owner_id: str) -> list[dict[str, Any]]:
        return [self._unpack(row) for row in self._all(
            "SELECT * FROM agents WHERE owner_id = ? ORDER BY name", owner_id)]

    def agent(self, agent_id: str) -> dict[str, Any] | None:
        return self._unpack(self._one("SELECT * FROM agents WHERE id = ?", agent_id))

    def agent_by_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        return self._unpack(
            self._one("SELECT * FROM agents WHERE token = ?", token))

    def agents(self) -> list[dict[str, Any]]:
        return [self._unpack(row)
                for row in self._all("SELECT * FROM agents ORDER BY name")]

    def seen(self, agent_id: str, profile: dict[str, Any] | None = None,
             presence: dict[str, Any] | None = None) -> None:
        sets = ["seen = ?"]
        args: list[Any] = [time.time()]
        if profile is not None:
            sets.append("profile = ?")
            args.append(json.dumps(profile))
        if presence is not None:
            sets.append("presence = ?")
            args.append(json.dumps(presence))
        args.append(agent_id)
        self._run(f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", *args)

    def agents_on(self, project_id: str,
                  states: tuple[str, ...] = ("offered", "accepted", "complete")
                  ) -> list[str]:
        """The telescopes with a task on a project, each once."""
        marks = ",".join("?" for _ in states)
        rows = self._all(
            f"SELECT DISTINCT agent FROM tasks WHERE project = ? AND state IN ({marks})",
            project_id, *states)
        return [row["agent"] for row in rows]

    @staticmethod
    def _unpack(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row = dict(row)
        if "profile" in row:
            row["profile"] = json.loads(row["profile"] or "{}")
        if "presence" in row:
            try:
                row["presence"] = json.loads(row["presence"] or "{}")
            except (TypeError, ValueError):
                row["presence"] = {}
        return row

    # -- projects ----------------------------------------------------------
    def add_project(self, project_id: str, name: str, coordinator: str,
                    payload: dict[str, Any], owner_id: str = "") -> dict[str, Any]:
        self._run(
            "INSERT INTO projects (id, name, coordinator, owner_id, created,"
            " status, payload) VALUES (?, ?, ?, ?, ?, 'open', ?)",
            project_id, name, coordinator, owner_id, time.time(),
            json.dumps(payload))
        return self.project(project_id)

    # -- people ------------------------------------------------------------
    def upsert_user(self, user_id: str, token: str, name: str, avatar: str,
                    roles: list[str]) -> dict[str, Any]:
        """Somebody signing in: new, or back again with a fresh token.

        One token per person, replaced on every sign-in. Signing in from a
        second machine signs the first out, which is the simplest honest rule
        and the one a person can reason about.
        """
        self._run(
            "INSERT INTO users (id, token, name, avatar, roles, created, seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET token = excluded.token,"
            " name = excluded.name, avatar = excluded.avatar,"
            " roles = excluded.roles, seen = excluded.seen",
            user_id, token, name, avatar, json.dumps(roles), time.time(),
            time.time())
        return self.user(user_id)

    def user(self, user_id: str) -> dict[str, Any] | None:
        return self._unpack_user(self._one("SELECT * FROM users WHERE id = ?", user_id))

    def user_by_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        return self._unpack_user(
            self._one("SELECT * FROM users WHERE token = ?", token))

    def seen_user(self, user_id: str) -> None:
        self._run("UPDATE users SET seen = ? WHERE id = ?", time.time(), user_id)

    def forget_user_token(self, user_id: str) -> None:
        """Sign somebody out: the token stops working, the record stays."""
        self._run("UPDATE users SET token = ? WHERE id = ?",
                  "signed-out:" + user_id + ":" + str(time.time()), user_id)

    @staticmethod
    def _unpack_user(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row = dict(row)
        row["roles"] = json.loads(row.get("roles") or "[]")
        return row

    def add_login(self, code: str) -> None:
        self._run("INSERT INTO logins (code, created, user, claimed)"
                  " VALUES (?, ?, '', 0)", code, time.time())

    def login(self, code: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM logins WHERE code = ?", code)

    def bind_login(self, code: str, user_id: str) -> None:
        self._run("UPDATE logins SET user = ? WHERE code = ?", user_id, code)

    def claim_login(self, code: str) -> None:
        self._run("UPDATE logins SET claimed = 1 WHERE code = ?", code)

    def sweep_logins(self, older_than: float) -> None:
        self._run("DELETE FROM logins WHERE created < ?", older_than)

    def project(self, project_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM projects WHERE id = ?", project_id)
        if row is None:
            return None
        row["payload"] = json.loads(row["payload"])
        return row

    def projects(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM projects ORDER BY created DESC")
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def projects_of(self, owner_id: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM projects WHERE owner_id = ?"
                         " ORDER BY created DESC", owner_id)
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def rename_project(self, project_id: str, name: str) -> None:
        self._run("UPDATE projects SET name = ? WHERE id = ?", name, project_id)

    def set_project(self, project_id: str, payload: dict[str, Any],
                    status: str | None = None) -> dict[str, Any] | None:
        if status is None:
            self._run("UPDATE projects SET payload = ? WHERE id = ?",
                      json.dumps(payload), project_id)
        else:
            self._run("UPDATE projects SET payload = ?, status = ? WHERE id = ?",
                      json.dumps(payload), status, project_id)
        return self.project(project_id)

    # -- tasks -------------------------------------------------------------
    def add_task(self, task: dict[str, Any]) -> dict[str, Any]:
        self._run(
            "INSERT INTO tasks (id, project, agent, state, version, issued, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            task["id"], task["project"], task["agent"], task["state"],
            task["version"], task["issued"], json.dumps(task))
        return self.task(task["id"])

    def task(self, task_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT payload FROM tasks WHERE id = ?", task_id)
        return json.loads(row["payload"]) if row else None

    def tasks_for(self, agent_id: str,
                  states: tuple[str, ...] = ("offered", "accepted")) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in states)
        rows = self._all(
            f"SELECT payload FROM tasks WHERE agent = ? AND state IN ({marks})"
            " ORDER BY issued", agent_id, *states)
        return [json.loads(row["payload"]) for row in rows]

    def tasks_in(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT payload FROM tasks WHERE project = ?"
                         " ORDER BY issued", project_id)
        return [json.loads(row["payload"]) for row in rows]

    def set_task(self, task: dict[str, Any]) -> dict[str, Any]:
        self._run(
            "UPDATE tasks SET state = ?, version = ?, payload = ? WHERE id = ?",
            task["state"], task["version"], json.dumps(task), task["id"])
        return self.task(task["id"])

    # -- contributions -----------------------------------------------------
    def add_contribution(self, row_id: str, payload: dict[str, Any],
                         verdict: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Record one night. False when it was already there.

        Reporting the same night twice must not count the hours twice — an
        agent that retries after a dropped connection is the normal case, not
        an error, so a repeat is answered with what is already stored.
        """
        panel = str(payload.get("panel") or "")
        try:
            self._run(
                "INSERT INTO contributions (id, project, agent, task, night,"
                " filter, panel, seconds, accepted, overridden, received,"
                " payload, verdict)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                row_id, payload.get("project", ""), payload.get("agent", ""),
                payload.get("task", ""), payload.get("night", ""),
                payload.get("filterName", ""), panel,
                float(payload.get("seconds") or 0.0),
                1 if verdict.get("accepted") else 0, time.time(),
                json.dumps(payload), json.dumps(verdict))
        except sqlite3.IntegrityError:
            existing = self._one(
                "SELECT * FROM contributions WHERE agent = ? AND task = ?"
                " AND night = ? AND filter = ? AND panel = ?",
                payload.get("agent", ""), payload.get("task", ""),
                payload.get("night", ""), payload.get("filterName", ""), panel)
            existing = self._unpack_contribution(existing)
            # A night that grew after it was first sent - the program reports
            # a panel as soon as it has frames on it, and keeps shooting - is
            # the same night with more in it, and the larger figure is the
            # record. A coordinator's overruling stands: their verdict on the
            # night is not undone by more frames arriving.
            seconds = float(payload.get("seconds") or 0.0)
            if existing is not None and seconds > float(existing.get("seconds") or 0.0):
                overridden = bool(existing.get("overridden"))
                self._run(
                    "UPDATE contributions SET seconds = ?, payload = ?,"
                    " accepted = ?, verdict = ?, received = ? WHERE id = ?",
                    seconds, json.dumps(payload),
                    (1 if existing["accepted"] else 0) if overridden
                    else (1 if verdict.get("accepted") else 0),
                    json.dumps(existing["verdict"] if overridden else verdict),
                    time.time(), existing["id"])
                return self.contribution(existing["id"]), False
            return existing, False
        return self.contribution(row_id), True

    def contribution(self, row_id: str) -> dict[str, Any] | None:
        return self._unpack_contribution(
            self._one("SELECT * FROM contributions WHERE id = ?", row_id))

    def contributions(self, project_id: str | None = None,
                      agent_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM contributions"
        args: list[Any] = []
        where = []
        if project_id:
            where.append("project = ?")
            args.append(project_id)
        if agent_id:
            where.append("agent = ?")
            args.append(agent_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY received"
        return [self._unpack_contribution(row) for row in self._all(sql, *args)]

    def set_verdict(self, row_id: str, accepted: bool, verdict: dict[str, Any],
                    overridden: bool = True) -> dict[str, Any] | None:
        self._run(
            "UPDATE contributions SET accepted = ?, overridden = ?, verdict = ?"
            " WHERE id = ?",
            1 if accepted else 0, 1 if overridden else 0,
            json.dumps(verdict), row_id)
        return self.contribution(row_id)

    @staticmethod
    def _unpack_contribution(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row = dict(row)
        row["payload"] = json.loads(row["payload"])
        row["verdict"] = json.loads(row["verdict"] or "{}")
        row["accepted"] = bool(row["accepted"])
        row["overridden"] = bool(row["overridden"])
        return row
