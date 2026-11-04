"""A new collaboration collects files and takes rig auto-uploads by default
(2026-11-04); the request can still say no, and older projects are untouched."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("STARFRONT_DATA", tempfile.mkdtemp(prefix="sf-newproj-"))
os.environ.setdefault("STARFRONT_ADMIN_TOKEN", "test-admin-token-123456")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from server import app as appmod  # noqa: E402

REGION = {"ra": 5.59, "dec": -5.4, "width": 3.0, "height": 2.0}


def _client():
    if not appmod.ADMIN_TOKEN:
        appmod.ADMIN_TOKEN = "test-admin-token-123456"
    return TestClient(appmod.app), {"authorization": f"Bearer {appmod.ADMIN_TOKEN}"}


def _new(**extra):
    client, h = _client()
    r = client.post("/api/v1/projects", json={"name": "M42 test", "region": REGION, **extra}, headers=h)
    assert r.status_code == 200, r.text
    return appmod.store.project(r.json()["project"]["id"])["payload"]


def test_new_project_collects_and_autouploads_by_default():
    payload = _new()
    assert payload["collectFiles"] is True and payload["autoUpload"] is True


def test_new_project_can_opt_out():
    payload = _new(collectFiles=False, autoUpload=False)
    assert payload["collectFiles"] is False and payload["autoUpload"] is False


def test_existing_projects_are_not_changed():
    appmod.store.add_project("old1", "M31 old", "Bray", {"collectFiles": False, "region": {}}, owner_id="u")
    before = dict(appmod.store.project("old1")["payload"])
    _new()
    assert appmod.store.project("old1")["payload"] == before
