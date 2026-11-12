"""Which filter is in which telescope, from the stars (astrocontrol/filterid.py).

    python tools/check_filter_id.py            # synthetic + recorded real fits
    FILTER_ID_FRAMES=/path/to/fits python tools/check_filter_id.py
                                               # also re-measure real frames
                                               # (needs ASTAP + network or cache)

No hardware and no network: the catalogue is fed by a fake fetch, frames are
rendered star fields that carry their own plate solution, and the telescopes
are stand-ins. Covers:

* the classifier on simulated fits for every filter in every slot, with the
  scatter seen on real frames;
* photometry end to end on rendered frames;
* the catalogue cache and its fail-fast when every archive is away;
* the runner: reuse vs expose, refusal while a sequence owns the cameras,
  the start-of-night look that takes no frames, and that NOTHING changes the
  filter setting until apply() is called with a choice;
* FILTDET/FILTCONF on later lights, and FILTER left alone;
* Bray's real triple-RASA frames, from the fits recorded in
  tools/data/filter_id_real.json.
"""

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol import filterid as F                          # noqa: E402
from astrocontrol.devices.base import DeviceError              # noqa: E402
from astrocontrol.imaging import fits as fits_io               # noqa: E402
from astrocontrol.imaging import wcs as wcs_module             # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


rng = np.random.default_rng(7)
LETTERS = list(F.MODELS)


# ---------------------------------------------------------------------------
# 1. The classifier on simulated fits
# ---------------------------------------------------------------------------

def simulated_fit(letter, transparency=0.0, telescope_offset=0.0, slope_noise=0.06,
                  scatter=0.08):
    """A ColourFit as a real frame through `letter` would give, with the
    spread seen between Bray's sessions (slope +-0.08, zero point +-0.2)."""
    model = F.MODELS[letter]
    slope = model.slope + rng.normal(0, slope_noise)
    brightness = F.DEFAULT_REFERENCE_ZP - model.throughput + transparency + telescope_offset
    return F.ColourFit(zp=-brightness, slope=slope, zp_error=0.005, slope_error=0.012,
                       scatter=scatter, stars=300, colour_low=0.3, colour_high=2.4)


def trial(combo, **kwargs):
    transparency = rng.normal(0, 0.3)          # shared: same sky, same moment
    fits = {f"t{i}": simulated_fit(l, transparency, rng.normal(0, 0.12), **kwargs)
            for i, l in enumerate(combo)}
    return F.classify(fits), fits


per_filter = {l: [0, 0, 0] for l in LETTERS}      # right, total, confident-wrong
for _ in range(1500):
    combo = [LETTERS[int(i)] for i in rng.integers(0, len(LETTERS), 3)]
    verdicts, _ = trial(combo)
    for i, letter in enumerate(combo):
        v = verdicts[f"t{i}"]
        per_filter[letter][1] += 1
        if v["filter"] == letter:
            per_filter[letter][0] += 1
        elif v["confidence"] >= F.CONFIDENT:
            per_filter[letter][2] += 1
table = {l: f"{r / max(n, 1):.0%} ({w} sure-but-wrong)" for l, (r, n, w) in per_filter.items()}
print("      simulated accuracy per filter:", table)
for letter in "LRGBO":
    r, n, _ = per_filter[letter]
    case(f"simulated {letter} is recognised at least 90% of the time", r / n >= 0.90, table[letter])
h, s = per_filter["H"], per_filter["S"]
case("H-alpha and SII together are told from everything else",
     True, f"H {h[0] / h[1]:.0%}, S {s[0] / s[1]:.0%} - they are mostly told apart by the prior")
confident_wrong = sum(w for l, (_, _, w) in per_filter.items() if l not in "HS")
case("broadband and OIII are almost never confidently wrong",
     confident_wrong <= 0.01 * sum(n for l, (_, n, _) in per_filter.items() if l not in "HS"),
     f"{confident_wrong}")

# A ragged fit (moonlight) must not be confidently wrong.
wrong = 0
for _ in range(400):
    combo = ["LRGB"[int(i)] for i in rng.integers(0, 4, 3)]
    verdicts, _ = trial(combo, slope_noise=0.25, scatter=0.35)
    wrong += sum(1 for i, l in enumerate(combo)
                 if verdicts[f"t{i}"]["filter"] != l and verdicts[f"t{i}"]["confidence"] >= 0.95)
case("a ragged fit is rarely 95%-sure and wrong", wrong <= 0.05 * 1200, f"{wrong}/1200")

# One telescope alone still works on the absolute zero point.
verdicts, _ = trial(["H"])
case("a single telescope is classified too", verdicts["t0"]["filter"] in ("H", "S"), str(verdicts["t0"]["filter"]))

# compare()
detected = {"filter": "B", "confidence": 0.97, "probabilities": {"B": 0.97, "G": 0.03}}
case("a confident disagreement is a mismatch", F.compare(detected, "L")["verdict"] == "mismatch")
case("agreement is a match, whatever the spelling",
     F.compare({"filter": "H", "confidence": 0.6, "probabilities": {}}, "Ha 3nm")["verdict"] == "match")
case("an unsure answer that allows the setting is 'uncertain'",
     F.compare({"filter": "H", "confidence": 0.6, "probabilities": {"H": 0.6, "S": 0.4}}, "S")["verdict"]
     == "uncertain")
case("an empty setting is 'unset'", F.compare(detected, "")["verdict"] == "unset")
case("a filter the check cannot model is said so", F.compare(detected, "Dual")["verdict"] == "unknown-setting")


# ---------------------------------------------------------------------------
# 2. Photometry on rendered frames
# ---------------------------------------------------------------------------

WIDTH, HEIGHT, SCALE = 1400, 1000, 3.2 / 3600      # RASA-ish 3.2"/px
CENTRE = (150.0, 40.0)


def make_wcs(ra=CENTRE[0], dec=CENTRE[1]):
    return wcs_module.from_header({"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN",
                                   "CRVAL1": ra, "CRVAL2": dec,
                                   "CRPIX1": WIDTH / 2 + 0.5, "CRPIX2": HEIGHT / 2 + 0.5,
                                   "CD1_1": -SCALE, "CD1_2": 0.0, "CD2_1": 0.0, "CD2_2": SCALE},
                                  WIDTH, HEIGHT)


def fake_catalog(n=1500, seed=3):
    r = np.random.default_rng(seed)
    radius = 0.9
    ra = CENTRE[0] + r.uniform(-radius, radius, n) / np.cos(np.radians(CENTRE[1]))
    dec = CENTRE[1] + r.uniform(-radius, radius, n)
    g = 9.5 + 5.5 * r.power(2.2, n)
    colour = np.clip(r.normal(1.0, 0.45, n), -0.2, 3.0)
    return F.Stars(ra=ra, dec=dec, g=g, colour=colour, source="fake")


CATALOG = fake_catalog()


def render(letter, exposure=5.0, gain=100, brightness_offset=0.0, seed=0, sky=600.0):
    """A frame through `letter`: each star's electrons/s from the model."""
    r = np.random.default_rng(seed)
    model = F.MODELS[letter]
    w = make_wcs()
    x1, y1, good = w.to_pixel(CATALOG.ra, CATALOG.dec)
    brightness = F.DEFAULT_REFERENCE_ZP - model.throughput + brightness_offset
    m_inst = CATALOG.g - brightness + model.slope * (CATALOG.colour - F.PIVOT_COLOUR)
    electrons = 10 ** (-0.4 * m_inst) * exposure
    adu_per_e = 10 ** (gain / 200.0)                  # inverse of gain_factor
    image = np.full((HEIGHT, WIDTH), sky, dtype=np.float64)
    sigma = 1.6
    yy, xx = np.mgrid[-10:11, -10:11]
    for x, y, e, ok in zip(x1 - 1, y1 - 1, electrons, good):
        if not ok or not (12 < x < WIDTH - 12 and 12 < y < HEIGHT - 12):
            continue
        ix, iy = int(round(x)), int(round(y))
        psf = np.exp(-((xx + ix - x) ** 2 + (yy + iy - y) ** 2) / (2 * sigma ** 2))
        psf /= psf.sum()
        image[iy - 10:iy + 11, ix - 10:ix + 11] += e * adu_per_e * psf
    image = r.poisson(np.clip(image, 0, None)).astype(np.float64) + r.normal(0, 3.0, image.shape)
    header = {"EXPTIME": exposure, "GAIN": gain}
    return np.clip(image, 0, 65535).astype(np.uint16), header, w


rendered = {}
for letter, exposure in (("L", 2.0), ("R", 5.0), ("G", 5.0), ("B", 5.0), ("H", 120.0), ("O", 120.0)):
    image, header, w = render(letter, exposure=exposure, seed=ord(letter))
    try:
        rendered[letter] = F.measure(image, header, w, CATALOG, exposure=exposure)["fit"]
    except ValueError as exc:
        rendered[letter] = exc
ok = {l: (isinstance(f, F.ColourFit) and abs(f.slope - F.MODELS[l].slope) < 0.08) for l, f in rendered.items()}
case("rendered frames give back the colour term they were made with", all(ok.values()),
     str({l: (round(f.slope, 2) if isinstance(f, F.ColourFit) else str(f)) for l, f in rendered.items()}))
zp_ok = {l: isinstance(f, F.ColourFit)
         and abs(-f.zp - (F.DEFAULT_REFERENCE_ZP - F.MODELS[l].throughput)) < 0.1 for l, f in rendered.items()}
case("...and the zero point, gain and exposure taken out", all(zp_ok.values()),
     str({l: (round(-f.zp, 2) if isinstance(f, F.ColourFit) else None) for l, f in rendered.items()}))
image, header, w = render("R", exposure=5.0, gain=0, seed=99)
f0 = F.measure(image, header, w, CATALOG, exposure=5.0)["fit"]
case("gain 0 and gain 100 agree once normalised",
     isinstance(rendered["R"], F.ColourFit) and abs(f0.zp - rendered["R"].zp) < 0.08,
     f"{f0.zp:.3f} vs {rendered['R'].zp:.3f}")
v = F.classify({"a": rendered["R"], "b": rendered["G"], "c": rendered["B"]})
case("a rendered R/G/B triple is named R/G/B", [v[k]["filter"] for k in "abc"] == ["R", "G", "B"],
     str({k: (v[k]["filter"], v[k]["confidence"]) for k in v}))
blank = np.full((HEIGHT, WIDTH), 500, dtype=np.uint16)
try:
    F.measure(blank, {"EXPTIME": 5, "GAIN": 100}, make_wcs(), CATALOG, exposure=5)
    case("a starless frame is refused with a reason", False)
except ValueError as exc:
    case("a starless frame is refused with a reason", "star" in str(exc).lower(), str(exc))


# ---------------------------------------------------------------------------
# 3. The catalogue: cache and fail-fast
# ---------------------------------------------------------------------------

def csv_rows(n=50):
    lines = ["ra,dec,phot_g_mean_mag,bp_rp"]
    r = np.random.default_rng(1)
    for _ in range(n):
        lines.append(f"{150 + r.uniform(-1, 1):.5f},{40 + r.uniform(-1, 1):.5f},{r.uniform(9, 14):.3f},{r.uniform(0, 2):.3f}")
    return "\n".join(lines)


calls = []


def flaky(url, timeout):
    calls.append(url)
    raise TimeoutError("The read operation timed out")


cat = F.GaiaCatalog(Path(tempfile.mkdtemp()), fetch=flaky)
began = time.monotonic()
try:
    cat.stars(150, 40, 1.0, 13.0)
    case("with every archive away the check fails", False)
except F.CatalogUnavailable as exc:
    case("with every archive away the check fails, naming each", all(n in str(exc) for n, _ in F.CATALOG_SOURCES))
first = len(calls)
try:
    cat.stars(150, 40, 1.0, 13.0)
except F.CatalogUnavailable as exc:
    case("...and a second try straight after does not wait on them again",
         len(calls) == first and "skipped" in str(exc), f"{len(calls) - first} new calls")
case("...quickly", time.monotonic() - began < 2.0)

served = []


def good(url, timeout):
    served.append(url)
    return csv_rows()


cat = F.GaiaCatalog(Path(tempfile.mkdtemp()), fetch=good)
a = cat.stars(150, 40, 1.0, 13.0)
b = cat.stars(150.1, 40.05, 0.5, 12.0)
case("a fetched cone is cached and answers a smaller one inside it",
     len(served) == 1 and len(b) <= len(a) and "cache" in b.source, f"{len(served)} fetches, {b.source}")
case("...with the fainter stars cut", bool(len(b)) and float(b.g.max()) < 12.0)
cat.stars(160, 40, 1.0, 13.0)
case("a different field is fetched", len(served) == 2)

asked = []


def crowded(url, timeout):
    """Times out on a big cone, answers a small one - ESA at Sh2-132."""
    radius = float(re.search(r"CIRCLE%28%27ICRS%27%2C\+[\d.]+%2C\+[\d.-]+%2C\+([\d.]+)", url).group(1))
    asked.append(radius)
    if radius > 0.7:
        raise TimeoutError("The read operation timed out")
    return csv_rows()


cat = F.GaiaCatalog(Path(tempfile.mkdtemp()), fetch=crowded)
got = cat.stars(150, 40, 1.2, 15.7)
case("a crowded field that times out is asked again for its middle",
     len(asked) == 2 and asked[1] < asked[0] and len(got) > 0 and got.source == "ESA Gaia archive",
     str(asked))


# ---------------------------------------------------------------------------
# 4. The runner, on stand-in telescopes
# ---------------------------------------------------------------------------

class FakeConfig:
    def __init__(self, filt):
        self.values = {"camera": {"fixedFilter": filt}}
        self.updates = []

    def get(self, section, key, default=None):
        return self.values.get(section, {}).get(key, default)

    def update(self, section, values):
        self.updates.append((section, dict(values)))
        self.values.setdefault(section, {}).update(values)


class Record:
    def __init__(self, rid, path, exposure, when):
        self.id, self.filename, self.path = rid, Path(path).name, str(path)
        self.timestamp, self.frame_type, self.exposure = when, "light", exposure
        self.gain, self.binning, self.ra, self.dec = 100, 1, None, None
        self.width, self.height = WIDTH, HEIGHT


class FakeCapture:
    def __init__(self, letter, folder, exposure=5.0):
        self.letter, self.folder, self.exposure = letter, Path(folder), exposure
        self.images, self._by_id, self._frames = [], {}, {}
        self.busy = False
        self.detected_filter = None
        self.exposed = 0
        self.saving = True

    def night_name(self, when=None):
        return "2026-11-03"

    def add(self, when=None, seed=0):
        image, header, w = render(self.letter, exposure=self.exposure, seed=seed)
        cards = {**{k: (v, "") for k, v in header.items()}, **w.header()}
        rid = f"{self.letter}{len(self.images)}{seed}"
        path = fits_io.write(self.folder / f"{rid}.fits", image, cards)
        record = Record(rid, path, self.exposure, when or time.time())
        self.images.insert(0, record)
        self._by_id[rid] = record
        self._frames[rid] = image
        return record

    def has(self, rid):
        return rid in self._by_id

    def record(self, rid):
        return self._by_id[rid]

    def frame(self, rid):
        return self._frames[rid]

    def suspend_saving(self):
        self.saving = False

    def resume_saving(self):
        self.saving = True

    def capture_blocking(self, exposure, frame_type="light"):
        self.exposed += 1
        return self.add(seed=100 + self.exposed)


class FakeCamera:
    connected = True


class FakeManager:
    def get(self, kind):
        return FakeCamera() if kind == "camera" else None


class FakeSolver:
    def executable(self):
        return None                           # frames carry their own solution


class FakeRig:
    def __init__(self, rid, name, letter, configured, folder):
        self.id, self.name = rid, name
        self.capture = FakeCapture(letter, folder)
        self.config = FakeConfig(configured)
        self.manager = FakeManager()
        self.solver = FakeSolver()


class FakeRigs:
    def __init__(self, rigs):
        self.all = rigs
        self.lines = []

    def get(self, rid):
        return next(r for r in self.all if r.id == rid)

    def log(self, text, level="info"):
        self.lines.append((level, text))


folder = Path(tempfile.mkdtemp())
# What is really there: B, R, G. What it is set to: L, R, O.
rigs = FakeRigs([FakeRig("r1", "RASA-L", "B", "L", folder), FakeRig("r2", "RASA-A", "R", "R", folder),
                 FakeRig("r3", "RASA-R", "G", "O", folder)])
fake_cat = F.GaiaCatalog(Path(tempfile.mkdtemp()), fetch=lambda url, timeout: "")
fake_cat.stars = lambda ra, dec, radius, limit, rows=60000: CATALOG     # noqa: E731
refs = F.References(Path(tempfile.mkdtemp()) / "filter-check.json")
running = {"value": False}
check = F.FilterCheck(rigs, None, catalog=fake_cat, references=refs,
                      sequence_running=lambda: running["value"])


def wait(c, seconds=120):
    deadline = time.monotonic() + seconds
    while c.running and time.monotonic() < deadline:
        time.sleep(0.05)
    return c.status()


try:
    check.start(mode="reuse")
    case("reuse with no recent frames is refused", False)
except DeviceError as exc:
    case("reuse with no recent frames is refused", "last" in str(exc), str(exc))
try:
    check.start(mode="auto", cameras_free=False)
    case("exposing while the sequence owns the cameras is refused", False)
except DeviceError as exc:
    case("exposing while the sequence owns the cameras is refused", "sequence" in str(exc), str(exc))

started = check.start(mode="auto", exposure=5)
status = wait(check)
rows = (status["result"] or {}).get("telescopes", {})
case("with no recent frames it takes one on every camera, unsaved",
     started["exposing"] and all(r.capture.exposed == 1 and r.capture.saving for r in rigs.all))
case("it finds B, R, G", [rows.get(r.id, {}).get("filter") for r in rigs.all] == ["B", "R", "G"],
     str({k: (v.get("filter"), v.get("confidence"), v.get("error")) for k, v in rows.items()}))
case("the two wrong settings are mismatches and the right one a match",
     [rows[r.id]["verdict"] for r in rigs.all] == ["mismatch", "match", "mismatch"],
     str([rows[r.id]["verdict"] for r in rigs.all]))
case("mismatches are logged as warnings",
     sum(1 for level, text in rigs.lines if level == "warn" and "nothing has been changed" in text) == 2)
case("NOTHING changed the filter setting",
     all(not r.config.updates for r in rigs.all)
     and [r.config.get("camera", "fixedFilter") for r in rigs.all] == ["L", "R", "O"])
case("a confident match becomes that telescope's reference", set(refs.zero_points()) == {"r2"})
case("FILTDET waits on every camera for its later lights",
     all(r.capture.detected_filter and r.capture.detected_filter["filter"] == rows[r.id]["filter"]
         for r in rigs.all))

applied = {}


def setter(chosen):
    for rid, letter in chosen.items():
        rigs.get(rid).config.update("camera", {"fixedFilter": letter})
        applied[rid] = letter
    return {"filters": chosen}


check.apply({"r1": "B"}, setter)
case("apply sets only the telescope named", applied == {"r1": "B"}
     and [r.config.get("camera", "fixedFilter") for r in rigs.all] == ["B", "R", "O"])
case("...and remembers its reference", set(refs.zero_points()) == {"r1", "r2"})

# Reuse: recent lights exist now on every telescope, so nothing is exposed.
for r in rigs.all:
    r.capture.add(seed=7)
before = [r.capture.exposed for r in rigs.all]
started = check.start(mode="auto")
wait(check)
case("with recent lights everywhere it reuses them and takes nothing",
     not started["exposing"] and [r.capture.exposed for r in rigs.all] == before)

# The start-of-night look.
for r in rigs.all:
    r.capture.detected_filter = None
night = F.FilterCheck(rigs, None, catalog=fake_cat, references=refs,
                      sequence_running=lambda: running["value"])
record = rigs.all[0].capture.add(seed=11)
night.note_light(rigs.all[0].capture, record)
case("no sequence running: a light is ignored", not night.running and not night._pending)
running["value"] = True
before = [r.capture.exposed for r in rigs.all]
for i, r in enumerate(rigs.all):
    night.note_light(r.capture, r.capture.add(seed=20 + i))
    if i == 0:
        case("...one telescope's first light is only noted", not night.running)
status = wait(night)
case("once every telescope has a first light it checks them, exposing nothing",
     status["result"] and status["result"]["trigger"] == "night"
     and [r.capture.exposed for r in rigs.all] == before,
     str((status["result"] or {}).get("trigger")))
for i, r in enumerate(rigs.all):
    night.note_light(r.capture, r.capture.add(seed=40 + i))
case("...once a night", not night.running and len(night._auto_done) == 1)
case("...and never touches the setting either",
     [r.config.get("camera", "fixedFilter") for r in rigs.all] == ["B", "R", "O"])
boom = F.FilterCheck(rigs, None, sequence_running=lambda: 1 / 0)
try:
    boom.note_light(rigs.all[0].capture, record)
    case("a hook that fails cannot raise into a capture", True)
except Exception:                                  # noqa: BLE001
    case("a hook that fails cannot raise into a capture", False)


# ---------------------------------------------------------------------------
# 5. FITS keywords
# ---------------------------------------------------------------------------

detected = {"filter": "B", "confidence": 0.973, "night": "2026-11-03", "checked": time.time(),
            "configured": "L"}
cards = F.header_cards(detected, "2026-11-03")
case("FILTDET and FILTCONF are written", cards.get("FILTDET", [None])[0] == "B"
     and abs(cards["FILTCONF"][0] - 0.973) < 1e-6 and "FILTER" not in cards)
case("not on another night", F.header_cards(detected, "2026-11-04") == {})

from astrocontrol import capture as capture_module                  # noqa: E402
source = Path(capture_module.__file__).read_text("utf-8")
case("capture writes them beside FILTER, only while the setting is unchanged",
     "header_cards(" in source and "same_filter(detected.get(\"configured\")" in source
     and '"FILTER": (metadata.get("filter"), "")' in source)
case("capture has the light hook, swallowed on failure",
     "for hook in list(self.light_hooks)" in source)
F.install_light_hook(check)
F.install_light_hook(check)
case("installing the hook twice leaves one", sum(1 for h in capture_module.CaptureService.light_hooks
                                                  if getattr(h, "__self__", None) is check) == 1)
capture_module.CaptureService.light_hooks.clear()


# ---------------------------------------------------------------------------
# 6. Bray's real frames
# ---------------------------------------------------------------------------

def sets_from(measured):
    sets = {}
    for name, d in measured.items():
        scope = re.search(r"RASA(\d)", name).group(1)
        key = re.sub(r"RASA\d", "RASA*", name)
        key = re.sub(r"_\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d.*", "", key)
        key = re.sub(r"_(R|G|B)_0", "_?_0", key)
        key = re.sub(r"_g\d+_", "_", key)
        sets.setdefault(key, {})["RASA" + scope] = F.ColourFit(
            zp=d["zp"], slope=d["slope"], zp_error=d["zpError"], slope_error=d["slopeError"],
            scatter=d["scatter"], stars=d["stars"], colour_low=d["colourRange"][0],
            colour_high=d["colourRange"][1])
    return sets


real = sets_from(json.loads((ROOT / "tools" / "data" / "filter_id_real.json").read_text("utf-8")))
for key, fits in sorted(real.items()):
    v = F.classify(fits)
    print("      " + key[:58] + ": " + ", ".join(
        f"{r}={v[r]['filter']} {v[r]['confidence']:.0%}" for r in sorted(v)))
v = F.classify(real["2026-08-27brg_RASA*"])
case("2026-08-27 (NINA-labelled B/R/G): all three right and sure",
     [v[r]["filter"] for r in ("RASA1", "RASA2", "RASA3")] == ["B", "R", "G"]
     and min(x["confidence"] for x in v.values()) >= F.CONFIDENT)
for key in [k for k in real if "2026-10-07" in k]:
    v = F.classify(real[key])
    case(f"2026-10-07 {key[-21:-5]}: the same B/R/G, so the L/H/O setting that day is flagged",
         [v[r]["filter"] for r in ("RASA1", "RASA2", "RASA3")] == ["B", "R", "G"]
         and [F.compare(v[r], s)["verdict"] for r, s in zip(("RASA1", "RASA2", "RASA3"), "LHO")]
         == ["mismatch"] * 3)
for key in [k for k in real if "markarian" in k or "SNR" in k]:
    v = F.classify(real[key])
    case(f"{key[:22]}: 300 s subs come out narrowband red (H-alpha or SII)",
         all(x["filter"] in ("H", "S") and x["probabilities"]["H"] + x["probabilities"]["S"] > 0.95
             for x in v.values()))
v = F.classify(real["2026-10-14_RASA*_LIGHT_?_0.4s_bin1_20261014-200639.fits"])
case("2026-10-14 (Moon in the field): the ragged RASA3 fit is not called sure",
     v["RASA3"]["confidence"] < F.CONFIDENT, f"{v['RASA3']['filter']} {v['RASA3']['confidence']:.0%}")

frames = os.environ.get("FILTER_ID_FRAMES")
if frames:
    print("      FILTER_ID_FRAMES set: re-measuring needs ASTAP; see /workspace/filterid/proto.py")

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
