# Solar System Survey — Addendum

Supplements `solar-system-survey-spec.md`. Where the two conflict, **this document wins** — it corrects several parameters in the original.

---

## 1. The big change: this is two surveys, not one

The original spec described a single twilight survey at 30–60° solar elongation with a 15° altitude floor. That was calibrated for deep NEA detection and is **wrong for the comet case**.

There are two distinct observing modes with opposite parameters. Both must be supported, and mode should be a **property of a sweep segment**, not a global setting.

### Mode A — Deep NEA survey

| Parameter | Value |
|---|---|
| Target magnitude | ~20 |
| Solar elongation | Unconstrained (dark-hour sky) |
| Altitude floor | 15° hard, 20–30° working band |
| Exposure | 30 s × 36 frames per panel |
| Binning | 2×2 |
| Dither | Mandatory |
| Reduction | Synthetic tracking (Tycho Tracker), GPU required |
| Cadence | Every clear night, all year |
| Payoff | Steady — a few NEA discoveries per year |

Rationale for the strict altitude floor: at magnitude 20 there is no extinction margin. Dropping from 30° to 15° altitude costs ~0.6 mag, which is most of what synthetic tracking bought. Seeing degradation at low altitude also destroys the FWHM-based comet/asteroid discrimination.

### Mode B — Shallow twilight comet sweep

| Parameter | Value |
|---|---|
| Target magnitude | 11–13 |
| Solar elongation | **20–45°** (the 23–30° band is the real gap) |
| Altitude floor | **5°** |
| Exposure | 5–15 s × ~15 frames per panel |
| Binning | 2×2 |
| Dither | Yes |
| Reduction | Simple frame differencing — synthetic tracking not required at this brightness |
| Cadence | ~20 min at each twilight, only when geometry permits (see §3) |
| Payoff | Rare — but this is the only path to a bright, photogenic comet |

Rationale for the 5° floor: at magnitude 11 you have ~9 magnitudes of margin. Extinction at 5° altitude is ~2.5 mag — easily absorbed. Rockwood's flat West Texas horizon makes 5° genuinely usable where most sites cannot.

### Reference case that sets Mode B's parameters

Comet C/2023 P1 (Nishimura), discovered 2023 Aug 11.76854 UT:
- **Solar elongation: 23°**
- **Altitude: 5–8°** above the eastern horizon
- Pre-dawn (morning) twilight
- Equipment: Canon 6D + 200 mm f/3 lens (66 mm aperture)
- Three 30-second exposures
- Magnitude ~11 at discovery; peaked near magnitude 4

Professional surveys missed it precisely because it was so low and so deep in twilight glow. Note the aperture: 66 mm. Mode B is not aperture-limited, it is sky- and geometry-limited.

**Verification note:** the 23° elongation figure came from a user-supplied summary whose citations looked mismatched. It is consistent with everything else known about the discovery, but it is the load-bearing parameter for Mode B — worth confirming against the actual MPEC before hard-coding.

---

## 2. Night structure — one routine, Sun-altitude-driven mode switching

Both modes belong in a **single nightly routine** that switches mode as the Sun's altitude changes. They cannot be merged into one tiled sweep, because they want opposite geometry at the same clock time and the Mode B window is the one that cannot be recovered if missed.

Segment boundaries (evening; morning is the mirror image and is higher priority):

| Sun altitude | Segment | Notes |
|---|---|---|
| −5° | Startup | Cool cameras, open, unpark, autofocus, plate solve |
| −6° to −12° | **Mode B evening sweep** (~20 min) | Low elongation, 5–15° altitude, short subs |
| −12° to −15° | Transition | Panels at 20–30°, elongation 40–60°, longer subs, refocus |
| below −15° | **Mode A deep survey** | Full-night NEA work |
| −15° to −12° (morning) | Morning deep twilight | Highest-value block of the night |
| −12° to −6° (morning) | **Mode B morning sweep** (~20 min) | The Nishimura window |
| −6° (morning) | Shutdown | Sky flats, park, warm, update coverage map |

### Instrument assignment, not just time slots

Mode B is cheap and does not need the whole fleet. The scheduler should support **running both modes simultaneously on different instruments** — e.g. the VSD100 on the Mode B sweep while the RASAs and FSQs work Mode A panels above.

This means a sweep segment carries an instrument subset, and the scheduler assigns instruments to modes rather than only panels to times. Check whether the existing sequencer already has conditional or time-gated blocks before building a parallel mechanism.

---

## 3. Seasonal geometry — the constraint that governs Mode B

**This is the most important addition in this document.**

Mode B is only geometrically possible for part of the year. On the wrong dates it is not a matter of scheduling cleverness — the target zone is physically below the horizon.

### Why

The comet zone sits ~23° from the Sun *along the ecliptic*. Whether that 23° translates to altitude depends on the **angle the ecliptic makes with the horizon** during twilight.

- **Steep ecliptic** (near vertical): 23° along the ecliptic is ~23° *above* the Sun. Sun at −15°, target at +8°. Observable.
- **Flat ecliptic** (near horizontal): the same 23° is mostly lateral. Sun at −15°, target at −12°. Below the horizon. Unobservable at any aperture.

The ecliptic is fixed at 23.4° to the celestial equator, and the horizon's relation to the equator is fixed by latitude — but the Sun slides along the ecliptic over the year, so the portion of the ecliptic near the Sun presents a different angle to the horizon each month. At 31°N the pre-dawn ecliptic ranges from roughly 70° from horizontal down to about 20°.

Morning and evening are inverted: when morning is steep, evening is flat.

| Season | Morning twilight | Evening twilight |
|---|---|---|
| Autumn (Sep–Nov) | **Steep — best window** | Flat |
| Winter | Moderate | Moderate |
| Spring (Feb–Apr) | Flat | **Steep** |
| Summer | Moderate | Moderate |

This is the same geometry that governs Mercury visibility — Mercury is never more than ~20° from the Sun and is "well placed" in spring evenings and autumn mornings. The comet zone is the Mercury zone.

Nishimura's mid-August pre-dawn discovery sits at the start of the steep morning season.

### Required UI features

1. **Per-night viability readout.** For the selected date, compute and display: *the minimum solar elongation reachable at ≥5° altitude with the Sun at ≤−12°.* This single number tells the user whether tonight's Mode B sweep is worth running at all.

2. **Seasonal viability calendar.** A year view highlighting the date ranges where Mode B geometry works, for both morning and evening. This is arguably the most useful single feature in the tab — it tells the user which ~20 mornings a year not to miss.

3. **Automatic fallback.** When Mode B is not viable for a given night, the planner should say so plainly and allocate the window to deep twilight panels instead of generating unreachable targets.

---

## 4. Morning is more valuable than evening

Morning twilight views sky **leading** the Sun — fresh sky holding objects hidden in solar conjunction for months. Evening views trailing sky, already observable in prior weeks.

Both 2I/Borisov and Nishimura were morning-twilight discoveries. Morning discoveries also remain observable longer before returning to conjunction, which matters because a confirmation arc is required.

Weight morning higher in field scoring. Still run evening — the equipment is otherwise idle and evening catches a different population (outbound objects, trailing-side NEAs) — but prioritize evening candidates harder for **same-night confirmation**, because an evening object may not have a tomorrow.

---

## 5. Instrument assignment recommendations

Revised from the original spec now that the two modes are distinguished.

**VSD100 (f/3, ~31 sq deg) — first call for Mode B.**
Largest field owned; at 23° elongation the accessible sky is a thin sliver near the horizon, so covering it in few pointings matters more than depth. Clean unobstructed optics give trustworthy PSFs, which matters because the low-altitude discriminator is "does this look non-stellar." At magnitude 11–13 the 100 mm aperture is ample — Nishimura used 66 mm.

It should **not** be exclusively dedicated: the Mode B window is ~20 min twice a night and seasonal. Assign it to Mode A whenever Mode B geometry is not viable.

**Mount check:** at 5° altitude on a fast sweep, the mount needs quick slews, reliable low-altitude pointing, and no meridian-flip interruptions. German equatorials are specifically discouraged for continuous scanning.

**3× RASA 8 + 2× FSQ106 — Mode A deep panels**, each on its own field. See original spec §3 for field sizes and the RASA 8's 22 mm image-circle caveat.

---

## 6. Coadding — do not do it for detection

Question considered: point multiple systems at the same field and coadd?

**No, for the six-system case.** Étendue is conserved either way; coadding just trades area for depth at a fixed rate. Three systems coadded gives √3 = 1.73× SNR = 0.6 mag deeper, but covers ⅓ the area. Since object counts go roughly as N ∝ 10^0.5m, 0.6 mag yields ~2× more objects per square degree against 3× the square degrees. **Area wins.**

Coadding also introduces resampling across differing pixel scales (correlating noise and softening PSFs — bad for FWHM discrimination), bandpass mismatches, and a requirement that all mounts slew and expose in lockstep.

**One exception worth testing empirically:** if the single-system limiting magnitude in twilight falls short of the findable population entirely, area is worthless and depth is the only thing that helps. Measure actual limiting magnitude on a real twilight field before finalizing.

---

## 7. Adjacent opportunity — the hosted fleet (out of scope for this tab)

Rockwood hosts hundreds of customer widefield systems. Their normal imaging output — dozens to hundreds of dithered subs on one field per night — is already exactly the input synthetic tracking wants (Tycho needs 11+ frames; dithering is mandatory per the Catalina team). No change to customer behavior would be needed, only a pipeline watching data land.

Not a coadd, and not part of this tab. Noted because it affects the longer-term architecture:

- **As a confirmation network** this fleet is excellent: the survey flags a candidate, a hosted long-focal-length rig confirms it in minutes because the position is already known. Confirming faint candidates on a second, larger telescope before submitting is standard practice.
- **Constraints:** field selection is aesthetic not strategic (everyone shoots the Milky Way, which is excluded from survey use); GPU cost at that scale is substantial; customer data rights require explicit opt-in; many hosted rigs are too long-focal-length to contribute meaningful étendue.
- **Suggested first step:** prototype the archival pipeline on existing owned data and check whether it recovers known NEAs from ordinary imaging runs.

---

## 8. Corrections to the original spec — quick list

| Original spec said | Corrected to |
|---|---|
| Single survey mode | Two modes (A/B) with separate parameter sets |
| Elongation 30–60° | Mode A: unconstrained. Mode B: 20–45° |
| Altitude floor 15° global | Mode A: 15°. **Mode B: 5°** |
| Fixed exposure strategy | Per-mode: 30 s × 36 (A) vs 5–15 s × ~15 (B) |
| Synthetic tracking everywhere | Mode A only; Mode B uses frame differencing |
| Horizon mask at 15° | Must render usefully to 5°; add a **site horizon profile** for real obstructions |
| Seasonal variation mentioned in passing | **Governing constraint for Mode B** — needs viability readout + calendar |

Additional: extinction below 10° altitude is unreliable in textbook form, and West Texas horizon dust will make it worse. Allow user override of the extinction model and plan to measure it empirically.
