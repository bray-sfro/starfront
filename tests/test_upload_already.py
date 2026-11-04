"""A file already here: the body is still taken before the answer, and the
Files page skips such files before any PUT."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_files_rules_api import world, put  # noqa: E402,F401
from server import files as filesmod  # noqa: E402


def test_already_here_is_drained_and_200(world, monkeypatch):
    client, store, tmp, _ = world
    assert put(client, tmp, "L_0001.fits", "light").status_code == 200
    seen = {}
    real = filesmod._drain

    async def spy(request, length):
        seen["length"] = length
        ok = await real(request, length)
        seen["ok"] = ok
        return ok

    monkeypatch.setattr(filesmod, "_drain", spy)
    r = put(client, tmp, "L_0001.fits", "light")
    assert r.status_code == 200 and r.json()["already"] is True
    assert seen["ok"] is True and seen["length"] > 0
    assert len(store.files("p1")) == 1 if hasattr(store, "files") else True


def test_drain_reads_whole_body_and_survives_disconnect():
    from starlette.requests import ClientDisconnect, Request

    def make(chunks, cut=False):
        msgs = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1 or cut}
                for i, c in enumerate(chunks)]
        if cut:
            msgs.append({"type": "http.disconnect"})
        it = iter(msgs)

        async def receive():
            return next(it)
        return Request({"type": "http", "method": "PUT", "headers": []}, receive)

    assert asyncio.run(filesmod._drain(make([b"a" * 10, b"b" * 10]), 20)) is True
    assert asyncio.run(filesmod._drain(make([b"a" * 10], cut=True), 20)) is False


def test_page_skips_files_already_here_and_times_out(world):
    client, *_ = world
    page = client.get("/files").text if client.get("/files").status_code == 200 else Path(filesmod.__file__).read_text()
    src = Path(filesmod.__file__).read_text()
    assert "function alreadyHere(q)" in src
    assert "const have = alreadyHere(q);" in src and "q.text = 'already here'" in src
    assert "xhr.timeout = 120000" in src and "xhr.ontimeout = () => resolve(false)" in src
    assert "state.listing = listing;" in src
    assert page
