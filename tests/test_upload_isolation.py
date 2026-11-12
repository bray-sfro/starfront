"""Uploads in their own process: which paths are uploads, and what each
process role answers."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("STARFRONT_DATA", tempfile.mkdtemp(prefix="sf-iso-"))
os.environ.setdefault("STARFRONT_ADMIN_TOKEN", "iso-admin")

from server import app as server  # noqa: E402


def test_upload_paths() -> None:
    up = server.is_upload
    assert up("PUT", "/api/v1/projects/d07d82eb861e/files/L_0001.fits")
    assert up("POST", "/api/v1/agent/uploads")
    assert up("PUT", "/api/v1/agent/uploads/abc?offset=0".split("?")[0])
    assert up("POST", "/api/v1/agent/uploads/abc/finish")
    assert up("GET", "/api/v1/agent/uploads")
    # Not uploads: control calls, downloads, deletes, the rules, rechecks.
    assert not up("PUT", "/api/v1/projects/d07d82eb861e")
    assert not up("GET", "/api/v1/projects/d07d82eb861e/files")
    assert not up("DELETE", "/api/v1/projects/d07d82eb861e/files/L_0001.fits")
    assert not up("GET", "/api/v1/projects/p/files/u/light/L_0001.fits")
    assert not up("PUT", "/api/v1/projects/p/upload-rules")
    assert not up("POST", "/api/v1/projects/p/files/recheck")
    assert not up("GET", "/api/v1/agent/projects")
    assert not up("POST", "/api/v1/agent/report")


def test_roles(monkeypatch) -> None:
    client = TestClient(server.app)
    monkeypatch.setattr(server, "ROLE", "control")
    r = client.put("/api/v1/projects/x/files/a.fits", content=b"x")
    assert r.status_code == 503 and r.headers.get("retry-after") == "30"
    assert client.get("/api/v1/health").json()["role"] == "control"
    assert client.get("/api/v1/projects").status_code == 200

    monkeypatch.setattr(server, "ROLE", "uploads")
    assert client.get("/api/v1/projects").status_code == 404
    assert client.put("/api/v1/projects/x", json={"status": "closed"}).status_code == 404
    assert client.get("/api/v1/health").status_code == 200
    # An upload reaches the handler (and is refused there: nobody signed in).
    assert client.put("/api/v1/projects/x/files/a.fits", content=b"x").status_code == 401

    monkeypatch.setattr(server, "ROLE", "all")
    assert client.get("/api/v1/projects").status_code == 200
