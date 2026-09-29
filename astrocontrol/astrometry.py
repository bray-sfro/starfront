"""Plate solving at astrometry.net, for when the local solver cannot.

ASTAP is fast, works offline and solves nearly everything it is pointed at —
*given a hint*. Where it struggles is the case this exists for: a file from
somewhere else. A frame off another rig, or off a phone lens, or a twenty-year
-old scan, has a scale and a pointing this observatory knows nothing about, and
a blind search over the whole sky at an unknown scale is exactly the job
astrometry.net was built for and ASTAP's index files often are not installed
for.

So it is a fallback rather than a competitor: the header first, then ASTAP,
then this. It needs the network and an API key, it takes anywhere from twenty
seconds to several minutes, and it is only ever reached when the two free and
instant answers have both failed.

The API is the public nova.astrometry.net one, which a self-hosted instance
also speaks — point `url` at that instead and nothing else changes.

No third-party HTTP library: a multipart upload is forty lines of `urllib` and
this program has no dependency on `requests` for anything else.
"""

from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from .devices.base import DeviceError

DEFAULT_URL = "https://nova.astrometry.net/api/"

#: How often to ask whether it has finished. The service is not fast and
#: hammering it helps nobody.
POLL_SECONDS = 5.0


def _post(url: str, fields: dict[str, Any], timeout: float) -> dict[str, Any]:
    """A form post of the `request-json=...` shape the API expects."""
    body = urllib.parse.urlencode(
        {"request-json": json.dumps(fields)}).encode("ascii")
    request = urllib.request.Request(
        url, data=body, headers={"User-Agent": "Starfront"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _get(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "Starfront"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _upload(url: str, session: str, path: Path, timeout: float,
            extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Multipart upload of one file, with the request JSON alongside it."""
    boundary = f"----astrocontrol{uuid.uuid4().hex}"
    payload = json.dumps({"session": session, "publicly_visible": "n",
                          "allow_modifications": "d",
                          "allow_commercial_use": "d", **(extra or {})})
    content_type = (mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream")

    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Type: text/plain\r\n')
    parts.append(b'Content-Disposition: form-data; name="request-json"\r\n\r\n')
    parts.append(payload.encode("utf-8") + b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f"Content-Type: {content_type}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n\r\n'
        .encode())
    parts.append(path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "Starfront"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def solve(path: Path, api_key: str, url: str = DEFAULT_URL,
          timeout: float = 600.0, say: Callable[[str], None] | None = None,
          should_abort: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Solve one file and return its calibration.

    Returns the service's own calibration block: `ra` and `dec` in degrees,
    `pixscale` in arcseconds, `orientation` in degrees and `parity`, plus the
    job id so a person can go and look at the result.
    """
    if not api_key:
        raise DeviceError("no astrometry.net API key is set")
    path = Path(path)
    if not path.is_file():
        raise DeviceError(f"{path} is not a file")
    base = url if url.endswith("/") else url + "/"
    note = say or (lambda message: None)
    stop = should_abort or (lambda: False)
    deadline = time.monotonic() + timeout

    def left() -> float:
        return max(1.0, deadline - time.monotonic())

    try:
        note("signing in to astrometry.net")
        login = _post(base + "login", {"apikey": api_key}, min(60.0, left()))
        if login.get("status") != "success":
            raise DeviceError(
                f"astrometry.net refused the API key: "
                f"{login.get('errormessage') or login.get('status')}")
        session = login["session"]

        note(f"uploading {path.name} ({path.stat().st_size // 1024} KB)")
        submission = _upload(base + "upload", session, path, left())
        if submission.get("status") != "success":
            raise DeviceError(
                f"astrometry.net refused the upload: "
                f"{submission.get('errormessage') or submission.get('status')}")
        subid = submission["subid"]
        note(f"submitted as {subid}; waiting for a solution")

        # Two waits, because the service has two queues: one for the submission
        # to be turned into a job, and one for the job to be run.
        job_id = None
        while job_id is None:
            if stop():
                raise DeviceError("plate solve aborted")
            if time.monotonic() > deadline:
                raise DeviceError(
                    f"astrometry.net did not start the job within "
                    f"{timeout / 60:.0f} minutes")
            time.sleep(POLL_SECONDS)
            status = _get(f"{base}submissions/{subid}", min(60.0, left()))
            jobs = [j for j in (status.get("jobs") or []) if j]
            if jobs:
                job_id = jobs[0]

        while True:
            if stop():
                raise DeviceError("plate solve aborted")
            if time.monotonic() > deadline:
                raise DeviceError(
                    f"astrometry.net did not finish within "
                    f"{timeout / 60:.0f} minutes")
            job = _get(f"{base}jobs/{job_id}", min(60.0, left()))
            state = job.get("status")
            if state == "success":
                break
            if state == "failure":
                raise DeviceError("astrometry.net could not solve the frame")
            note(f"astrometry.net job {job_id}: {state or 'queued'}")
            time.sleep(POLL_SECONDS)

        calibration = _get(f"{base}jobs/{job_id}/calibration", min(60.0, left()))
        if "ra" not in calibration:
            raise DeviceError("astrometry.net returned no calibration")
        return {**calibration, "jobId": job_id, "submissionId": subid}

    except urllib.error.HTTPError as exc:
        raise DeviceError(f"astrometry.net returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise DeviceError(f"could not reach astrometry.net: {exc.reason}") from exc
    except (ValueError, KeyError) as exc:
        raise DeviceError(f"astrometry.net said something unexpected: {exc}") from exc
