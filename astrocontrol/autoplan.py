"""Deciding what to shoot, not just when.

`schedule.arrange` answers "which target, in what order, for how long".  This
answers the question underneath it: given that a target has ninety minutes of
sky tonight, *what should go in those ninety minutes* — which filters, how long
a sub, and how many.

Three things decide that, and all three are things a person weighs up by hand
every clear night:

**The Moon.**  Not whether it is up — a five per cent crescent a hundred degrees
away is nothing, and that is most of the Moon's time in the sky — but whether it
is up, lit, and near enough to matter.  A bright Moon does not stop a night; it
changes what the night is for.  Narrowband barely notices it, because a 3 nm
filter throws away almost all of the scattered sunlight along with almost all of
the sky.  Broadband notices enormously.  So a bright Moon moves a target onto
whatever narrowband it carries, and if it carries none, shortens the broadband
subs instead: skyglow fills the well, and the fix for a bright sky is more
shorter frames, not fewer longer ones.

**What the target already has.**  Two hours of luminance and ten minutes of blue
is not half a picture, it is a luminance frame with a colour problem.  So the
allocation is not shared out by ratio, it is shared out by *deficit* against a
ratio — whichever channel is furthest behind gets tonight.  Over a season that
converges on the ratio without anyone tracking it.

**The goal.**  A target with a goal in hours is not given more than it needs to
reach it, so the last night on a target is the short one it should be.

Everything here is a default that the operator then edits.  The arranger writes
an allocation into the plan; it does not shoot anything, and every number it
picks is visible and changeable on the plan before the night starts.
"""

from __future__ import annotations

from typing import Any

from . import filters, schedule

#: Filters that see the whole visible band, and therefore see the Moon. One
#: letter each - every spelling is folded to it before it gets here.
BROADBAND = ("L", "R", "G", "B")

#: Filters narrow enough that scattered moonlight is mostly rejected with it.
NARROWBAND = ("H", "S", "O")

#: A dual-band filter is narrowband for the purposes of the Moon but produces a
#: colour image on its own, so it is never mixed into a ratio with anything.
DUAL = ("Dual", "Duo", "L-eXtreme", "L-eNhance", "L-Ultimate", "L-Quad")

#: What the allocation aims at when nothing has been shot yet.  Luminance
#: carries the detail and is where the signal-to-noise is won, so it gets twice
#: any one colour channel; the three colours are equal because they are only
#: ever used together.
BROADBAND_RATIO = {"L": 2.0, "R": 1.0, "G": 1.0, "B": 1.0}

#: Narrowband is shared equally by default.  The Hubble palette wants more H
#: than S in practice, but that is a processing preference rather than a fact
#: about the sky, and guessing it wrong wastes a whole night of a rare filter.
NARROWBAND_RATIO = {"H": 1.0, "O": 1.0, "S": 1.0}


def classify(name: str) -> str:
    """Which sort of filter this is, for the purposes of the Moon."""
    cleaned = filters.canonical(name)
    if cleaned in NARROWBAND:
        return "narrowband"
    if cleaned.lower() in {d.lower() for d in DUAL}:
        return "dual"
    if cleaned in BROADBAND:
        return "broadband"
    # Anything unrecognised is treated as broadband, which is the cautious
    # answer: it may be affected by the Moon, and saying so costs a shorter sub
    # where guessing narrowband would cost a blown-out night.
    return "broadband"


def conditions(moon: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """What sort of night this is, in one dict, with the reason in words."""
    illumination = float(moon.get("illumination") or 0.0)
    up_minutes = float(moon.get("upMinutes") or 0.0)
    threshold = float(settings.get("narrowbandAboveIllumination") or 0.4)

    bright = illumination >= threshold and up_minutes > 0
    if up_minutes <= 0:
        summary = (f"The Moon is down all night ({illumination * 100:.0f}% lit) "
                   "— a broadband night.")
    elif bright:
        summary = (f"The Moon is {illumination * 100:.0f}% lit and up for "
                   f"{up_minutes / 60:.1f}h — a narrowband night where a target "
                   "has the filters for it.")
    else:
        summary = (f"The Moon is {illumination * 100:.0f}% lit and up for "
                   f"{up_minutes / 60:.1f}h — dim enough for broadband.")
    return {
        "illumination": illumination,
        "moonUpMinutes": up_minutes,
        "bright": bright,
        "summary": summary,
    }


def _exposure_for(name: str, sky: dict[str, Any],
                  settings: dict[str, Any]) -> float:
    """How long one sub through this filter should be tonight.

    The per-filter table decides it wherever it has an answer; the three by
    class are the fallback, so a wheel carrying something the table has never
    heard of still gets a sensible length rather than a default that suits
    nothing.
    """
    kind = classify(name)
    name = filters.canonical(name)
    table = settings.get("filterExposures")
    named = None
    if isinstance(table, dict):
        value = filters.canonical_keys(table).get(name)
        if value is not None and float(value) > 0:
            named = float(value)

    if kind in ("narrowband", "dual"):
        # Scattered moonlight is mostly outside the passband, so the Moon does
        # not shorten these. What limits them is the mount and the guiding.
        return named if named is not None else float(
            settings.get("narrowbandExposure") or 600.0)

    if named is not None:
        base = named
    elif name == "L":
        base = float(settings.get("luminanceExposure") or 120.0)
    else:
        base = float(settings.get("broadbandExposure") or 180.0)

    if sky.get("bright") and settings.get("shortenUnderMoon", True):
        # A bright sky fills the well sooner, and the answer to that is more
        # shorter frames rather than fewer longer ones: the sky background is
        # what is being swamped, and it swamps at the same rate whatever the
        # sub length, so shorter subs simply keep them out of saturation.
        base = max(float(settings.get("minExposure") or 30.0), base / 2.0)
    return round(base, 1)


def _deficits(ratio: dict[str, float],
              collected: dict[str, float]) -> dict[str, float]:
    """How far behind each filter is against the ratio being aimed at.

    The pace is set by whichever channel is furthest *ahead* relative to its
    weight; every other channel's deficit is what it would take to catch up.
    A target with nothing shot has no deficits at all, and falls back to the
    plain ratio — which is the right answer for a first night.
    """
    pace = 0.0
    for name, weight in ratio.items():
        if weight > 0:
            pace = max(pace, collected.get(name, 0.0) / weight)
    if pace <= 0:
        return {}
    deficits = {name: max(0.0, pace * weight - collected.get(name, 0.0))
                for name, weight in ratio.items()}
    return deficits if any(v > 0 for v in deficits.values()) else {}


def _share(ratio: dict[str, float], collected: dict[str, float],
           seconds: float) -> dict[str, float]:
    """Split `seconds` between filters, catching up the ones behind first."""
    deficits = _deficits(ratio, collected)
    if deficits:
        owed = sum(deficits.values())
        if owed >= seconds:
            # Not even enough to level the channels up: spend it all on that.
            return {name: seconds * value / owed
                    for name, value in deficits.items() if value > 0}
        # Level them up, then share what is left by the ratio.
        left = seconds - owed
        weight = sum(ratio.values()) or 1.0
        return {name: deficits.get(name, 0.0) + left * ratio[name] / weight
                for name in ratio}

    weight = sum(ratio.values()) or 1.0
    return {name: seconds * value / weight for name, value in ratio.items()}


def choose(target: dict[str, Any], available_seconds: float,
           available_filters: list[str], moon: dict[str, Any],
           sky: dict[str, Any], overheads: dict[str, float],
           settings: dict[str, Any], goal_hours: float = 0.0,
           moon_separation: float | None = None) -> dict[str, Any]:
    """What this target should shoot tonight, and why.

    Returns an allocation the plan can store, plus the reasoning in plain words
    — because an allocation that arrives without a reason is one the operator
    has to check by hand, which is most of the work it was meant to save.
    """
    notes: list[str] = []
    carried = [f for f in available_filters if f]
    if not carried:
        return {"filters": [], "notes": ["no filters are configured for this "
                                         "telescope, so nothing could be chosen"]}

    # -- how much time there is to spend --------------------------------
    seconds = max(0.0, float(available_seconds))
    collected = dict((target.get("integration") or {}).get("byFilter") or {})
    if goal_hours > 0:
        done = float((target.get("integration") or {}).get("seconds") or 0.0)
        remaining = goal_hours * 3600.0 - done
        if remaining <= 0:
            return {"filters": [],
                    "notes": [f"already past its {goal_hours:g}-hour goal"]}
        if remaining < seconds:
            seconds = remaining
            notes.append(f"trimmed to the {remaining / 3600:.1f}h left of its "
                         f"{goal_hours:g}-hour goal")
    if seconds <= 0:
        return {"filters": [], "notes": ["no time in the night for it"]}

    # -- which filters ---------------------------------------------------
    narrow = [f for f in carried if classify(f) == "narrowband"]
    dual = [f for f in carried if classify(f) == "dual"]
    broad = [f for f in carried if classify(f) == "broadband"]

    far_enough = (moon_separation is not None
                  and moon_separation >= float(settings.get("moonSafeDegrees") or 90.0))

    # Whether the Moon matters *to this target*, which is not the same as
    # whether the night is a bright one. A full Moon a hundred and twenty
    # degrees away is someone else's problem: it neither moves this target onto
    # narrowband nor shortens its subs, and a sky dict that said otherwise
    # would quietly halve every exposure on the far side of the sky.
    here = dict(sky)
    here["bright"] = bool(sky.get("bright")) and not far_enough

    if sky.get("bright") and far_enough:
        chosen, ratio = broad or narrow, BROADBAND_RATIO
        notes.append(f"the Moon is bright but {moon_separation:.0f}° away, which "
                     "is far enough for broadband at full length")
    elif sky.get("bright") and narrow:
        chosen, ratio = narrow, NARROWBAND_RATIO
        notes.append(f"narrowband, because the Moon is "
                     f"{sky['illumination'] * 100:.0f}% lit and up")
    elif sky.get("bright") and dual:
        chosen, ratio = dual[:1], {dual[0]: 1.0}
        notes.append(f"the dual-band filter, because the Moon is "
                     f"{sky['illumination'] * 100:.0f}% lit and there is no "
                     "narrowband set")
    elif sky.get("bright"):
        chosen, ratio = broad, BROADBAND_RATIO
        notes.append("broadband under a bright Moon, because there is no "
                     "narrowband in the wheel — the subs are shortened to suit")
    else:
        chosen, ratio = broad or narrow or dual, BROADBAND_RATIO
        if broad:
            notes.append("broadband, with the Moon down or dim")
        else:
            notes.append("the only filters this telescope carries")

    if not chosen:
        return {"filters": [], "notes": ["no usable filter for tonight's sky"]}

    # A ratio only means anything for the filters actually in the wheel.
    ratio = {name: ratio.get(name, 1.0) for name in chosen}

    # -- what is left for frames after the rig has taken its cut ----------
    #
    # A slot is wall-clock time, and the shutter does not get all of it. The
    # slew and centre at the start, a filter change per channel, and the focus
    # sweeps the night will trigger all come out first — otherwise the arranger
    # hands out more frames than the night holds and the plan immediately marks
    # them as over budget, which is a confusing way to discover that the
    # arranger and the budget disagree.
    spent = float(overheads.get("perPanel", 90.0) or 0.0)
    spent += len(chosen) * float(overheads.get("filterChange", 20.0) or 0.0)
    usable = max(0.0, seconds - spent)
    run = float(overheads.get("focusRun", 0.0) or 0.0)
    if run > 0 and usable > 0:
        every = float(overheads.get("focusEvery", 0.0) or 0.0)
        usable = max(0.0, (usable - run)
                     / (1.0 + (run / (every * 60.0) if every > 0 else 0.0)))
    if usable <= 0:
        return {"filters": [],
                "notes": notes + ["the slot is taken up by slewing, changing "
                                  "filters and focusing before a frame fits"]}
    if usable < seconds * 0.9:
        notes.append(f"{(seconds - usable) / 60:.0f} min of it goes on slewing, "
                     "filter changes and focusing")

    # -- how long each sub, and how many ---------------------------------
    exposures = {name: _exposure_for(name, here, settings) for name in chosen}
    shares = _share(ratio, collected, usable)
    behind = [name for name in chosen
              if _deficits(ratio, collected).get(name, 0.0) > 0]
    if behind:
        notes.append("weighted towards " + ", ".join(behind)
                     + ", which are behind the others")

    minimum = max(1, int(settings.get("minFramesPerFilter") or 3))
    allocation: list[dict[str, Any]] = []
    for name in chosen:
        exposure = exposures[name]
        cost = schedule.frame_seconds(exposure, overheads)
        count = int(shares.get(name, 0.0) // cost)
        if count <= 0:
            continue
        if count < minimum:
            # A handful of frames of one channel is not worth the filter change
            # it costs; the time is better given to the channels that can carry
            # a real count.
            continue
        allocation.append({"name": name, "exposure": exposure, "count": count})

    if not allocation:
        # Not enough time for a useful set of anything. Give the whole slot to
        # the single most deserving filter rather than returning nothing.
        name = max(chosen, key=lambda f: shares.get(f, 0.0))
        exposure = exposures[name]
        count = int(usable // schedule.frame_seconds(exposure, overheads))
        if count <= 0:
            return {"filters": [], "notes": notes + ["too little time for even "
                                                     "one frame"]}
        allocation = [{"name": name, "exposure": exposure, "count": count}]
        notes.append(f"only enough time for one filter, so all of it goes to {name}")

    # Fill the slot, then make sure it really fits.
    #
    # Both steps ask `schedule.plan_seconds` rather than doing arithmetic of
    # their own, because that is the function the plan's own ceilings enforce.
    # Anything that estimates the same thing a second way ends up disagreeing
    # with it by a minute or so, and the operator is shown an allocation the
    # planner immediately marks as over budget — which looks like a fault and
    # is really two pieces of arithmetic that were never told to agree.
    def cost() -> float:
        return schedule.plan_seconds(allocation, 1, overheads)

    def share_of(item: dict[str, Any]) -> float:
        spent = schedule.frame_seconds(item["exposure"], overheads) * item["count"]
        return spent / max(1.0, shares.get(item["name"], 1.0))

    # Rounding down to whole frames, and dropping any filter that could not
    # reach a useful count, leaves time on the table — on a short window it can
    # be most of it. So frames are added one at a time to whichever filter is
    # furthest behind its share, until the next one would not fit.
    added = 0
    while added < 2000:
        candidate = min(allocation, key=share_of)
        candidate["count"] += 1
        if cost() > seconds:
            candidate["count"] -= 1
            break
        added += 1
    if added:
        notes.append(f"{added} more frame{'' if added == 1 else 's'} to fill the slot")

    # And trim, for the case where the first pass overshot: whichever filter is
    # furthest *ahead* of its share gives a frame back.
    trimmed = 0
    while cost() > seconds and trimmed < 2000:
        candidate = max((i for i in allocation if i["count"] > 0),
                        key=share_of, default=None)
        if candidate is None:
            break
        candidate["count"] -= 1
        trimmed += 1
    allocation = [item for item in allocation if item["count"] > 0]
    if not allocation:
        return {"filters": [], "notes": notes + ["too little time for even one frame"]}

    total = schedule.plan_seconds(allocation, 1, overheads)
    notes.append(f"{sum(i['count'] for i in allocation)} frames, "
                 f"{total / 3600:.1f}h of the {seconds / 3600:.1f}h slot "
                 "including everything but the exposures")
    return {"filters": allocation, "notes": notes}
