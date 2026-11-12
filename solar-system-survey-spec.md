# Solar System Survey — Feature Spec & Domain Context

Context document for implementing a new **Solar System Survey** tab in the astro control program, sitting next to the existing **Planner** tab.

---

## 1. What this feature is for

The goal is to run an automated survey for comets and near-Earth asteroids (NEAs) from Rockwood, Texas (31°N, Bortle 1). The survey targets the **twilight sky at low solar elongation** — the region professional surveys (Pan-STARRS, ATLAS, Catalina) mostly do not cover.

The observing pattern is: sweep a defined region of sky near the Sun during evening and morning twilight, take a burst of short exposures per field, and run the resulting image sets through detection software that finds moving objects. Comets and NEAs fall out of the same pipeline.

This tab does **planning and target generation only**. It produces mosaic sweeps that get saved into the existing target list and appended to the night's sequence. Detection/reduction is a separate downstream concern (Tycho Tracker), not part of this tab.

---

## 2. Domain background the implementation needs to respect

### 2.1 Survey geometry is Sun-relative, not sky-fixed

This is the single most important design constraint.

Survey panels must **not** be stored as fixed RA/Dec. They must be defined in a **Sun-relative frame** — solar elongation (ε) and ecliptic latitude (β), or equivalently (λ − λ☉, β) where λ☉ is the Sun's ecliptic longitude.

Reason: the survey zone is "the sky near the Sun," and that region sweeps through RA continuously over the year. A fixed RA/Dec grid goes stale in days. A Sun-relative grid is defined once and transformed to RA/Dec at plan time for the given date.

Implementation shape:
- User defines a survey region as a shape in (elongation, ecliptic latitude) space — e.g. ε from 30° to 60°, β from −30° to +30°.
- At plan time, compute λ☉ for the target date/time, transform the grid to RA/Dec, then filter for observability.

### 2.2 Morning twilight is more valuable than evening

Morning twilight looks at sky **leading** the Sun — fresh sky containing objects that have been hidden in solar conjunction for months. Evening twilight looks at trailing sky, which has already been observable.

Both 2I/Borisov and C/2023 P1 (Nishimura) were morning-twilight discoveries. Morning discoveries also stay observable longer before falling back into conjunction, which matters because you need a follow-up arc to confirm.

Practical consequence: both sweeps should be supported, but morning should be weighted higher when scoring/prioritizing fields. Evening sweeps are still worth running — the equipment is otherwise idle, and evening catches a different population (outbound objects, trailing-side NEAs).

### 2.3 Seasonal geometry varies enormously

At 31°N the angle the ecliptic makes with the horizon during twilight swings dramatically over the year, and evening/morning are inverted relative to each other. Spring evenings and autumn mornings have a steep ecliptic (good — low elongation is reachable at usable altitude). Autumn evenings and spring mornings lay it flat along the horizon (bad — the low-elongation zone is below the horizon or at unusable altitude).

Expect the number of usable panels to vary by a factor of 2–3 across the year. The planner must compute this, not assume it.

### 2.4 Altitude and airmass

| Altitude | Airmass | V extinction |
|---|---|---|
| 30° | 2.0 | ~0.6 mag |
| 20° | 2.9 | ~0.9 mag |
| 15° | 3.8 | ~1.2 mag |
| 10° | 5.6 | ~1.8 mag |

Working band for twilight is **20–30°**. Hard floor at **15°**. Below ~10°, extinction goes nonlinear, refraction distorts astrometry, and seeing balloons to 5–6" FWHM, which destroys the comet-vs-star FWHM discrimination that the detection stage depends on.

Low solar elongation and high altitude are in direct tension. At 40° elongation with the Sun at −12°, targets sit around 25–30°. At 30° elongation they're near 15°.

Rockwood's flat horizon is an asset here — 15° is genuinely usable, unlike a site with terrain. But horizon dust/haze may exceed the textbook extinction values, so allow the user to override.

### 2.5 Sun altitude window

Twilight sweeps run in a specific solar altitude window. Reference implementation (MAPS, San Pedro de Atacama) powers up at Sun altitude −5° and begins observing at −15°.

For this tool: let the user set the Sun-altitude window (e.g. −8° to −18°) as a planning parameter. The usable window is short — roughly 45 minutes — which is the binding constraint on how many panels fit.

### 2.6 Field selection exclusions

Standard exclusions from working survey programs:
- Avoid the **Milky Way** (crowded fields wreck detection)
- Stay at least **40° from the Moon** when the Moon is up
- Do not re-shoot a field observed within the **last 5 nights** (requires coverage-history tracking)

### 2.7 Exposure strategy — synthetic tracking

Detection uses **synthetic tracking**: many short exposures of the same field, stacked along thousands of trial velocity vectors on a GPU to pull out objects whose motion is unknown. This is what reaches magnitude ~20 with amateur apertures.

This means the acquisition pattern is **NOT** "a few subs, revisit in 20 minutes." It is a **single continuous burst of many short exposures on one field**, then move on.

Reference numbers (MAPS, 4× RASA 11):
- **36 exposures × 30 seconds** per field
- Up to 20 fields per night
- ~780 GB raw data per night

Hard requirements from the detection software (Tycho Tracker) and the Catalina Sky Survey team:
- **Minimum 11 exposures**; more is better
- Target **SNR ≥ 20** on the object
- **Dithering between subs is mandatory** — without it, pattern noise stacks into false detections that look like real signal

So the UI must expose: exposure length, exposure count per panel, and dither settings. Dither should default ON and be hard to accidentally disable.

### 2.8 Binning

Bin 2×2 for detection. Reasons: read noise adds in quadrature so SNR improves per resolution element; synthetic tracking compute cost scales with pixel count and binning quarters it; data volume drops 4×.

RASA 8 + IMX571 is 1.94"/px unbinned → 3.9"/px binned. Already undersampled relative to seeing, but irrelevant for point-source detection.

Note for later: final astrometry for MPC submission is better measured on unbinned data. Detection on binned, measurement on unbinned.

---

## 3. Hardware inventory at Rockwood

| System | Aperture | Focal length | f/ratio | Camera | Approx FOV |
|---|---|---|---|---|---|
| 3× RASA 8 | 200mm | 400mm | f/2 | ASI2600MM Pro (each) | ~2.5° × 1.9° each |
| 2× FSQ106 | 106mm | 382mm | f/3.6 | (full frame) | ~5.4° × 3.6° each |
| 1× VSD100 | 100mm | ~300mm | f/3 | (full frame) | ~6.5° × 4.4° |

RASA 8 caveat: the 22mm corrected image circle is smaller than the APS-C sensor diagonal (28.3mm), so corners fall outside the corrected field. Usable area is ~4.8 sq deg per camera, not the full sensor. Mosaic overlap calculations must use the **usable** field, not the sensor field.

The RASA's fast f/2 optics also produce soft corners, tilt sensitivity, and halos around bright stars — which matters downstream because the comet-vs-asteroid discriminator is PSF width. Not a planner concern, but it argues for generous panel overlap so candidates can be re-detected nearer frame center.

Mosaic planning should support **per-instrument field sizes** and ideally allow assigning different panels to different instruments simultaneously.

---

## 4. Feature spec: the Solar System Survey tab

New tab adjacent to **Planner**.

### 4.1 Sky display (reuse the existing planetarium)

The tab should render the sky using the program's existing planetarium widget, with these additions:

- **Ecliptic line drawn and labeled.** This is a required addition — the survey region is defined relative to the ecliptic and the Sun, so the user needs to see it.
- **Sun position** marked, plus optionally the solar elongation rings (e.g. dashed circles at 30°, 45°, 60° elongation).
- **Altitude horizon line** at the user-selected minimum elevation angle, drawn as a mask/shaded region so unusable sky is visually obvious.
- **Moon position and the 40° avoidance circle.**
- **The planned mosaic panels overlaid** as footprint rectangles, sized to the selected instrument's usable field.
- Time scrubber so the user can step through the twilight window and watch the sweep region rise/set and the panels move relative to the horizon.

### 4.2 Survey region definition

Controls to define the sweep zone in Sun-relative coordinates:
- Solar elongation range (min/max), default 30°–60°
- Ecliptic latitude range (±), default ±30°
- Which side: leading (morning) / trailing (evening) / both

### 4.3 Observability filter

- **Minimum elevation angle** — user-settable slider/field. Default 20°, hard floor warning below 15°.
- **Sun altitude window** — start and end, default −8° to −18°.
- **Maximum airmass** (alternative expression of the same constraint; either drives the other).
- Moon avoidance angle, default 40°.
- Milky Way exclusion toggle.
- "Exclude fields observed in last N nights," default 5 — requires a coverage history store.

### 4.4 Mosaic generation

- Generate a tiled panel grid covering the observable portion of the survey region.
- **Per-instrument field size** — the user picks which instrument the sweep is for, and panel size follows.
- **Overlap percentage**, default ~5–10%. Overlap matters more than usual here: an object falling in a seam is a missed discovery.
- Show total panel count and **estimated total time** vs. the available twilight window, with a clear warning when the plan overruns the window.
- Panel ordering: sort by setting rate (observe fields about to be lost first) and by airmass. Ideally a simple priority score the user can inspect.

### 4.5 Per-panel acquisition settings

In the target box settings for each generated panel:
- **Exposure length** (default 30s)
- **Exposure count** (default 36, minimum 11 with a warning below that)
- **Binning** (default 2×2)
- **Dither** — enabled by default, with dither scale in pixels. Warn loudly if disabled.
- **Filter** (if applicable)
- **Master calibration frame selection** — master dark, master flat, master bias. This should be a picker in the target box settings, same as any other target.

### 4.6 Saving to the target list

- Save the generated sweep as a **named target group** in the existing target list (e.g. "Morning Sweep 2026-10-14").
- Support **appending to the beginning or end of the night's sequence** — evening sweep prepends, morning sweep appends. This is the primary integration point with the existing sequencer.
- Sweeps should be regenerable: store the Sun-relative region definition plus the parameters, so re-running for a different date produces the correct new RA/Dec grid rather than a stale one.

### 4.7 Coverage history

Persist which panels were observed and when, in the **Sun-relative frame** (a HEALPix map indexed on (λ − λ☉, β) with last-observed timestamp works well). This feeds the "not in last 5 nights" filter and lets the planner show coverage gaps on the sky display.

---

## 5. Out of scope for this tab (but adjacent)

Noting these so the boundaries are clear:
- **Detection/reduction.** Handled by Tycho Tracker (Windows GUI, driven by watching a directory and launching when the frame count is reached). Not this tab's job.
- **Pre-processing pipeline.** Reference implementation applies darks/flats, bins 2×, plate-solves a center window, and recenters against the field's first frame — all while the next exposure is running. Worth designing toward but separate.
- **Known-object screening, confirmation routing, MPC submission.** Downstream.
- **Observatory code.** Administrative prerequisite (apply at minorplanetcenter.net/new_obscode_request), unrelated to the app.

---

## 6. Reference sources

- Alain Maury, "Discovery of comets and asteroids by amateurs in 2026" — spaceobs.com/en/Alain-Maury-s-Blog/Amateur-comets — the MAPS program's full operational description; the primary reference for exposure counts, field selection rules, and system étendue comparisons.
- MPC guidance on submitting astrometry from Tycho Tracker — minorplanetcenter.net/mpcops/documentation/tycho-tracker/ — source for the dithering and SNR requirements.
- BAA comet discovery reporting guidelines — britastro.org — why a single image is never sufficient and what artifacts mimic comets.
- Palomar twilight survey paper, arXiv 2409.15263 — the case for morning twilight.
