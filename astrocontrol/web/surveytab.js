/* The Solar System Survey tab: twilight sweeps for comets and NEAs.
 *
 * The chart is drawn in altitude and azimuth rather than as a star chart,
 * because everything this tab has to weigh up is horizon-relative: how far the
 * Sun is down, how low the fields are, where the Moon is, and which panels are
 * about to set. The planetarium answers a different question, and squeezing
 * this one into it would answer neither well.
 *
 * Everything drawn comes from the server's own plan, so the picture cannot
 * disagree with the panels that get saved.
 */
'use strict';

(function () {
  const app = window.astro;
  const $ = app.$;
  const DEG = Math.PI / 180;

  const sv = {
    plan: null,        // the last /api/survey/plan result
    context: null,     // /api/survey: windows, field, settings
    modes: null,       // the two surveys and their parameters
    viability: null,   // how close to the Sun tonight can reach
    season: null,      // a year of that, for the calendar
    fraction: 0,       // where the time scrubber sits, 0..1
    // A sweep of both sides is two runs hours apart, so the chart draws one at
    // a time: plotting dusk panels against dawn's horizon is nonsense.
    showWindow: null,
    // Draw the whole grid as footprints, not just the scheduled handful: it is
    // the only way to see whether the tiling actually overlaps.
    showGrid: true,
    zoom: 1,
    panX: 0,
    panY: 0,
    loading: false,
    saved: null,
  };

  /* --------------------------------------------------------------- helpers */

  const clock = (ts) => (!ts ? '—'
    : new Date(ts * 1000).toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }));

  function duration(seconds) {
    if (!seconds || seconds <= 0) return '0m';
    const m = Math.round(seconds / 60);
    return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
      : `${m}m`;
  }

  const number = (id, fallback) => {
    const value = Number($(id).value);
    return Number.isFinite(value) ? value : fallback;
  };

  /** Everything the controls are asking for, as the API expects it. */
  function settings() {
    const beta = Math.abs(number('svBeta', 30));
    return {
      elongationMin: number('svElongMin', 30),
      elongationMax: number('svElongMax', 60),
      betaMin: -beta,
      betaMax: beta,
      side: $('svSide').value,
      sunHigh: number('svSunHigh', -8),
      sunLow: number('svSunLow', -18),
      minAltitude: number('svMinAlt', 20),
      moonAvoidance: number('svMoon', 40),
      moonScaleByPhase: $('svMoonScale').checked,
      galacticAvoidance: number('svGalactic', 10),
      revisitNights: number('svRevisit', 5),
      exposure: number('svExposure', 30),
      exposureCount: Math.round(number('svCount', 36)),
      binning: Math.round(Number($('svBinning').value) || 2),
      dither: $('svDither').checked,
      ditherPixels: number('svDitherPixels', 3),
      overlap: Math.max(0, Math.min(0.89, number('svOverlap', 8) / 100)),
    };
  }

  function fillControls(values) {
    if (!values) return;
    const set = (id, value) => { if (value !== undefined && value !== null) $(id).value = value; };
    set('svElongMin', values.elongationMin);
    set('svElongMax', values.elongationMax);
    set('svBeta', Math.abs(values.betaMax ?? 30));
    set('svSide', values.side);
    set('svSunHigh', values.sunHigh);
    set('svSunLow', values.sunLow);
    set('svMinAlt', values.minAltitude);
    set('svMoon', values.moonAvoidance);
    set('svGalactic', values.galacticAvoidance);
    set('svRevisit', values.revisitNights);
    set('svExposure', values.exposure);
    set('svCount', values.exposureCount);
    set('svBinning', values.binning);
    set('svDitherPixels', values.ditherPixels);
    set('svOverlap', Math.round((values.overlap ?? 0.08) * 100));
    $('svMoonScale').checked = values.moonScaleByPhase !== false;
    $('svDither').checked = values.dither !== false;
  }

  /* ------------------------------------------------------------ warnings */

  /* The two numbers that decide whether a survey is worth running, and the two
     the operator is most likely to get wrong. Said plainly rather than buried. */
  function updateNotes() {
    const values = settings();

    const altitude = values.minAltitude;
    const airmass = altitude > 0
      ? 1 / Math.sin((altitude + 244 / (165 + 47 * altitude ** 1.1)) * DEG) : null;
    let note = airmass ? `Airmass ${airmass.toFixed(1)} at ${altitude}°.` : '';
    if (altitude < 15) {
      note += ' Below 15° extinction runs away, refraction distorts astrometry'
        + ' and seeing balloons — which destroys the comet-versus-star width'
        + ' test the detection stage depends on.';
    } else if (altitude < 20) {
      note += ' Under the 20–30° working band; usable at a flat site, but the'
        + ' extinction is real.';
    }
    const altNote = $('svAltNote');
    altNote.textContent = note;
    altNote.classList.toggle('warn-text', altitude < 15);

    const count = values.exposureCount;
    let acq = `${count} × ${values.exposure}s = `
      + `${duration(count * values.exposure)} of open shutter per field.`;
    if (count < 11) {
      acq += ' Detection needs at least 11 exposures — below that synthetic'
        + ' tracking has nothing to stack.';
    }
    if (!values.dither) {
      acq += ' Dithering is off: pattern noise will stack into false detections'
        + ' that look exactly like real moving objects.';
    }
    const acqNote = $('svAcqNote');
    acqNote.textContent = acq;
    acqNote.classList.toggle('warn-text', count < 11 || !values.dither);
  }

  /* ------------------------------------------------------------- the chart */

  /* Altitude/azimuth, north at the top, the horizon as the outer circle. The
     zenith is the centre, so "low in the sky" reads as "near the edge", which
     is the thing being judged. */
  function project(altitude, azimuth, cx, cy, radius) {
    const r = radius * (90 - altitude) / 90;
    const a = (azimuth - 90) * DEG;      // north up, east to the right
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  }

  /* Pixels per degree of arc. Only true near the centre — the projection
     stretches towards the horizon — so it is used for indicative circles
     (elongation rings, the Moon's exclusion) rather than for measurement. */
  const perDegreeScale = (radius) => radius / 90;

  /* ---------------------------------------------------- time, in the browser */

  /* The chart is redrawn as the scrubber moves, so alt/az has to be worked out
     here rather than only at the moment the server judged the plan. The angle
     the ecliptic makes with the horizon swings right through the twilight
     window — that swing is the whole seasonal story, and watching it move is
     more convincing than a number. */

  const julianOf = (timestamp) => timestamp / 86400.0 + 2440587.5;

  function lstDegrees(timestamp, longitude) {
    const jd = julianOf(timestamp);
    const t = (jd - 2451545.0) / 36525.0;
    const gmst = 280.46061837 + 360.98564736629 * (jd - 2451545.0)
      + 0.000387933 * t * t - (t * t * t) / 38710000.0;
    return ((gmst + longitude) % 360 + 360) % 360;
  }

  /** RA in hours, Dec in degrees, to altitude and azimuth at a moment. */
  function altAz(raHours, decDeg, timestamp, site) {
    const hourAngle = (lstDegrees(timestamp, site.longitude) - raHours * 15) * DEG;
    const dec = decDeg * DEG;
    const lat = site.latitude * DEG;
    const sinAlt = Math.sin(dec) * Math.sin(lat)
      + Math.cos(dec) * Math.cos(lat) * Math.cos(hourAngle);
    const altitude = Math.asin(Math.max(-1, Math.min(1, sinAlt)));
    const cosAlt = Math.cos(altitude);
    if (cosAlt < 1e-9) return [altitude / DEG, 0];
    const sinAz = -Math.cos(dec) * Math.sin(hourAngle) / cosAlt;
    const cosAz = (Math.sin(dec) - sinAlt * Math.sin(lat)) / (cosAlt * Math.cos(lat));
    return [altitude / DEG, ((Math.atan2(sinAz, cosAz) / DEG) % 360 + 360) % 360];
  }

  /** The moment the chart is drawing: wherever the scrubber sits. */
  function chartMoment() {
    const plan = sv.plan;
    const window_ = plan && (plan.windows || {})[currentWindow()];
    if (!window_) return null;
    return window_.start + (window_.end - window_.start) * sv.fraction;
  }

  /** Alt/az for something with RA and Dec, at the scrubbed time. */
  function placeOf(thing, moment) {
    const site = sv.plan && sv.plan.site;
    if (!site || moment === null || thing.ra === undefined
        || thing.ra === null) {
      return [thing.altitude, thing.azimuth];
    }
    return altAz(thing.ra, thing.dec, moment, site);
  }

  /* Where north and east point, in pixels, at one spot on the chart.
     The alt/az projection turns and stretches as you move away from the
     zenith, so a camera footprint cannot be drawn as an upright box — north is
     a different screen direction at every panel. Measured numerically by
     stepping a little north and a little east and seeing where they land,
     which cannot be got subtly wrong the way a closed form can. */
  function skyBasis(panel, moment, cx, cy, radius) {
    if (panel.ra === undefined || panel.ra === null) return null;
    const step = 0.35;                       // degrees
    const [alt, az] = placeOf(panel, moment);
    const centre = project(alt, az, cx, cy, radius);

    const dec = Math.max(-89, Math.min(89, panel.dec));
    const northAt = placeOf({ ra: panel.ra, dec: dec + step }, moment);
    const eastAt = placeOf(
      { ra: panel.ra + step / 15 / Math.cos(dec * DEG), dec }, moment);
    const pn = project(northAt[0], northAt[1], cx, cy, radius);
    const pe = project(eastAt[0], eastAt[1], cx, cy, radius);
    return {
      centre,
      north: [(pn[0] - centre[0]) / step, (pn[1] - centre[1]) / step],
      east: [(pe[0] - centre[0]) / step, (pe[1] - centre[1]) / step],
    };
  }

  /* The four corners of a camera field at a sky position angle.
     Position angle is measured from north through east and describes where the
     camera's up axis points, so its axes in (east, north) are
     up = (sin PA, cos PA) and right = (-cos PA, sin PA) — the same convention
     the mosaic planner uses, so the two agree about what an angle means. */
  function footprint(basis, positionAngle, widthDeg, heightDeg) {
    const pa = positionAngle * DEG;
    const cos = Math.cos(pa);
    const sin = Math.sin(pa);
    const axis = (e, n) => [basis.east[0] * e + basis.north[0] * n,
      basis.east[1] * e + basis.north[1] * n];
    const up = axis(sin, cos);
    const right = axis(-cos, sin);
    const hw = widthDeg / 2;
    const hh = heightDeg / 2;
    return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => [
      basis.centre[0] + right[0] * sx * hw + up[0] * sy * hh,
      basis.centre[1] + right[1] * sx * hw + up[1] * sy * hh,
    ]);
  }

  /** How far the ecliptic is from horizontal near the Sun, right now. */
  function eclipticTiltAt(sun, moment) {
    const site = sv.plan && sv.plan.site;
    if (!site || !sun || moment === null) return null;
    const trace = (sv.plan.ecliptic || {})[currentWindow()] || [];
    if (trace.length < 3) return null;
    // The two trace points either side of the Sun's own longitude.
    const sorted = [...trace].sort(
      (a, b) => Math.abs(a.dLambda) - Math.abs(b.dLambda));
    const near = sorted.slice(0, 2);
    if (near.length < 2) return null;
    const [a1, z1] = placeOf(near[0], moment);
    const [a2, z2] = placeOf(near[1], moment);
    const rise = a2 - a1;
    let across = ((z2 - z1 + 180) % 360) - 180;
    across *= Math.cos(((a1 + a2) / 2) * DEG);
    if (!rise && !across) return null;
    return Math.atan2(Math.abs(rise), Math.abs(across)) / DEG;
  }

  /** Which twilight the chart is drawing. */
  function currentWindow() {
    const windows = Object.keys((sv.plan && sv.plan.windows) || {});
    if (!windows.length) return null;
    return windows.includes(sv.showWindow) ? sv.showWindow : windows[0];
  }

  function drawChart() {
    const canvas = $('svChart');
    if (!canvas || canvas.offsetParent === null) return;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 600;
    const height = canvas.clientHeight || 480;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#070a10';
    ctx.fillRect(0, 0, width, height);

    // Zoom and pan about the middle of the view. The field footprints are a
    // couple of degrees across on a 180-degree chart, so at 1x they are
    // specks — being able to get in close is the only way to see whether the
    // panels actually overlap.
    const cx = width / 2 + sv.panX;
    const cy = height / 2 + sv.panY;
    const radius = (Math.min(width, height) / 2 - 26) * sv.zoom;

    // Horizon, and the altitude rings.
    ctx.strokeStyle = '#232936';
    ctx.lineWidth = 1;
    for (const altitude of [0, 30, 60]) {
      ctx.beginPath();
      ctx.arc(cx, cy, radius * (90 - altitude) / 90, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.fillStyle = '#4a5263';
    ctx.font = '11px ui-monospace, monospace';
    ctx.textAlign = 'center';
    for (const [label, azimuth] of [['N', 0], ['E', 90], ['S', 180], ['W', 270]]) {
      const [x, y] = project(-4, azimuth, cx, cy, radius);
      ctx.fillText(label, x, y + 4);
    }

    const plan = sv.plan;
    if (!plan || !plan.panels) {
      ctx.fillStyle = '#5c6577';
      ctx.textAlign = 'center';
      ctx.fillText('Plan a sweep to see it here', cx, cy);
      return;
    }

    const values = settings();
    // The unusable sky below the working altitude, shaded so it is obvious.
    ctx.fillStyle = 'rgba(217, 79, 61, 0.10)';
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.arc(cx, cy, radius * (90 - values.minAltitude) / 90, 0, Math.PI * 2, true);
    ctx.fill();
    ctx.strokeStyle = 'rgba(217, 79, 61, 0.5)';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.arc(cx, cy, radius * (90 - values.minAltitude) / 90, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);

    const which = currentWindow();
    const moon = (plan.moon || {})[which];
    const sun = (plan.sun || {})[which];
    const ecliptic = (plan.ecliptic || {})[which] || [];

    // The ecliptic. The survey region is defined against it, so it has to be
    // visible — and at 31 degrees its angle to the horizon is the single thing
    // that decides whether a twilight sweep is worth running tonight.
    const moment = chartMoment();
    if (ecliptic.length) {
      ctx.strokeStyle = 'rgba(232, 176, 84, 0.55)';
      ctx.lineWidth = 1.4;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      let drawing = false;
      for (const point of ecliptic) {
        const [altitude, azimuth] = placeOf(point, moment);
        if (altitude < -6) { drawing = false; continue; }
        const [x, y] = project(altitude, azimuth, cx, cy, radius);
        if (!drawing) { ctx.moveTo(x, y); drawing = true; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // The Sun, below the horizon, and rings at the elongation limits.
    if (sun) {
      const [sunAlt, sunAz] = placeOf(sun, moment);
      const [sx, sy] = project(sunAlt, sunAz, cx, cy, radius);
      ctx.strokeStyle = 'rgba(232, 176, 84, 0.30)';
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      for (const ring of [values.elongationMin, values.elongationMax]) {
        ctx.beginPath();
        ctx.arc(sx, sy, ring * perDegreeScale(radius), 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.fillStyle = '#e8b054';
      ctx.beginPath();
      ctx.arc(sx, sy, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#8b7340';
      ctx.textAlign = 'center';
      ctx.fillText('Sun', sx, sy + 16);
    }

    // The Moon and the circle it rules out, when it is up.
    if (moon && moon.up) {
      const [moonAlt, moonAz] = placeOf(moon, moment);
      const [mx, my] = project(moonAlt, moonAz, cx, cy, radius);
      ctx.strokeStyle = 'rgba(217, 79, 61, 0.55)';
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.arc(mx, my, moon.avoidanceRadius * perDegreeScale(radius), 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#c9cddb';
      ctx.beginPath();
      ctx.arc(mx, my, 4.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#8b94a7';
      ctx.textAlign = 'center';
      ctx.fillText('Moon', mx, my + 16);
    }

    // Every candidate for *this* twilight, so the rejected ones show why the
    // sweep is the shape it is. Candidates from the other side of the Sun
    // belong to a different sky hours away and are not drawn here.
    // The grid itself, as footprints rather than dots.
    //
    // This is the thing worth looking at: the scheduled panels are only the
    // handful the clock had room for, picked by score from all over the
    // region, so they are nowhere near each other and tell you nothing about
    // whether the tiling overlaps. The grid does.
    const here = (plan.candidates || []).filter((p) => p.window === which);
    const drawFootprints = here.length <= 900 && sv.showGrid;
    const fw = plan.field.width || 2;
    const fh = plan.field.height || 2;

    for (const panel of here) {
      const [altitude, azimuth] = placeOf(panel, moment);
      if (altitude < -2) continue;
      if (drawFootprints) {
        const basis = skyBasis(panel, moment, cx, cy, radius);
        if (!basis) continue;
        const corners = footprint(basis, panel.rotation || 0, fw, fh);
        ctx.beginPath();
        corners.forEach(([px, py], index) => {
          if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        });
        ctx.closePath();
        // Faint fills stack where panels overlap, so the overlap is visible as
        // a brighter seam rather than having to be taken on trust.
        ctx.fillStyle = panel.usable ? 'rgba(91, 141, 217, 0.10)'
          : 'rgba(120, 128, 148, 0.05)';
        ctx.fill();
        ctx.strokeStyle = panel.usable ? 'rgba(91, 141, 217, 0.30)'
          : 'rgba(120, 128, 148, 0.16)';
        ctx.lineWidth = 0.7;
        ctx.stroke();
      } else {
        const [x, y] = project(altitude, azimuth, cx, cy, radius);
        ctx.fillStyle = panel.usable ? 'rgba(91, 141, 217, 0.22)'
          : 'rgba(120, 128, 148, 0.13)';
        ctx.beginPath();
        ctx.arc(x, y, 2.2, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // The panels that will actually be shot, drawn as the camera will really
    // sit: turned to the angle that makes the grid abut, and skewed by the
    // projection the same way the sky is. An upright box would look tidy and
    // tell you nothing about whether the panels meet.
    const fixedAngle = (plan.rotation && !plan.rotation.hasRotator)
      ? plan.rotation.fixedAngle : null;
    ctx.font = '10px ui-monospace, monospace';
    for (const panel of plan.panels.filter((p) => p.window === which)) {
      const basis = skyBasis(panel, moment, cx, cy, radius);
      if (!basis) continue;
      // Without a rotator the camera cannot take the angle the grid wants, so
      // draw where it will actually point rather than where it should.
      const angle = fixedAngle !== null && fixedAngle !== undefined
        ? fixedAngle : (panel.rotation || 0);
      const corners = footprint(basis, angle, plan.field.width || 2,
                                plan.field.height || 2);
      ctx.beginPath();
      corners.forEach(([px, py], index) => {
        if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.closePath();
      ctx.strokeStyle = '#4bb87a';
      ctx.lineWidth = 1.4;
      ctx.stroke();
      ctx.fillStyle = 'rgba(75, 184, 122, 0.16)';
      ctx.fill();
      ctx.fillStyle = '#cfe8d8';
      ctx.textAlign = 'center';
      ctx.fillText(String(panel.index), basis.centre[0], basis.centre[1] + 3);
    }

    // A legend, because four colours with no key is a puzzle not a chart.
    ctx.textAlign = 'left';
    ctx.font = '11px ui-monospace, monospace';
    const inThis = (list) => list.filter((p) => p.window === which).length;
    const items = [
      ['#4bb87a', `${inThis(plan.panels)} fit tonight's ${which} window`],
      ['rgba(91, 141, 217, 0.6)',
        `${inThis((plan.candidates || []).filter((p) => p.usable))} in the grid`],
      ['rgba(120, 128, 148, 0.6)',
        `${inThis((plan.candidates || []).filter((p) => !p.usable))} ruled out`],
      ['rgba(217, 79, 61, 0.6)', `below ${values.minAltitude}°`],
    ];
    items.forEach(([colour, label], index) => {
      const y = 16 + index * 15;
      ctx.fillStyle = colour;
      ctx.fillRect(12, y - 8, 9, 9);
      ctx.fillStyle = '#8b94a7';
      ctx.fillText(label, 26, y);
    });

    // The ecliptic's angle to the horizon, recomputed for wherever the
    // scrubber is: it swings measurably across a single twilight window, which
    // is worth seeing rather than being surprised by.
    const tilt = eclipticTiltAt(sun, moment);
    if (tilt !== null) {
      ctx.fillStyle = tilt >= 55 ? '#4bb87a' : tilt >= 35 ? '#e8b054' : '#d94f3d';
      ctx.fillText(`ecliptic ${tilt.toFixed(0)}° from horizontal`, 12, height - 28);
    }
    if (moon) {
      ctx.fillStyle = '#8b94a7';
      ctx.fillText(
        `Moon ${(moon.illumination * 100).toFixed(0)}% lit, `
        + `${moon.elongation.toFixed(0)}° from the Sun, `
        + (moon.up ? `${moon.altitude.toFixed(0)}° up, avoid ${moon.avoidanceRadius}°`
          : 'below the horizon'),
        12, height - 12);
    }
  }

  /* ------------------------------------------------------------- rendering */

  /** With both sides planned, let the chart show one twilight at a time. */
  function renderWindowPicker() {
    const picker = $('svShowWindow');
    const windows = Object.keys((sv.plan && sv.plan.windows) || {});
    picker.hidden = windows.length < 2;
    if (picker.hidden) return;
    const chosen = currentWindow();
    if (picker.dataset.built !== windows.join(',')) {
      picker.innerHTML = '';
      for (const which of windows) {
        picker.appendChild(new Option(`${which} twilight`, which));
      }
      picker.dataset.built = windows.join(',');
    }
    picker.value = chosen;
  }

  function render() {
    const plan = sv.plan;
    renderWindowPicker();
    drawChart();
    if (!plan) return;

    const counts = plan.counts;
    setText('svFields', String(counts.scheduled));
    setText('svPerField', duration(plan.secondsPerPanel));
    const capacity = Object.values(plan.capacity || {});
    setText('svCapacity', capacity.length ? capacity.join(' / ') : '—');
    setText('svSummary', plan.summary || summarise(plan));

    // The honest warnings: the twilight window is short and it binds hard.
    const warnings = [];
    if (!Object.keys(plan.windows || {}).length) {
      warnings.push('No twilight window tonight for that Sun altitude range.');
    }
    if (counts.usable > counts.scheduled) {
      warnings.push(
        `${counts.usable} fields are observable but only ${counts.scheduled} fit `
        + `the window — each takes ${duration(plan.secondsPerPanel)}. Fewer `
        + 'exposures per field, or a shorter exposure, buys more sky.');
    }
    if (!counts.usable && counts.considered) {
      warnings.push('Nothing in the region is observable: check the minimum '
        + 'altitude and the Moon.');
    }
    // Whether the panels really abut, which is a property of the rig rather
    // than of the plan.
    const rot = plan.rotation || {};
    if (rot.required && rot.hasRotator) {
      warnings.push(
        `The grid runs along the ecliptic, so the camera turns between `
        + `${rot.min.toFixed(0)}° and ${rot.max.toFixed(0)}° across the sweep `
        + `(${rot.spread.toFixed(0)}° of rotation).`);
    } else if (rot.required && rot.worstMismatch !== undefined) {
      const line = `No rotator: the camera is fixed at `
        + `${(rot.fixedAngle || 0).toFixed(0)}° but the grid wants up to `
        + `${rot.worstMismatch.toFixed(0)}° away from that.`;
      warnings.push(rot.gaps
        ? line + ' At that angle the panels no longer overlap — raise the '
          + 'overlap or accept gaps between fields.'
        : line + ` The overlap that survives is about `
          + `${Math.round((rot.effectiveOverlap || 0) * 100)}%.`);
    }
    const warn = $('svWarn');
    warn.hidden = !warnings.length;
    warn.textContent = warnings.join('  ');

    $('btnSvSave').disabled = !counts.scheduled;
    if (!$('svName').value) {
      const day = $('svDate').value || new Date().toISOString().slice(0, 10);
      const side = $('svSide').value;
      $('svName').value = `${side[0].toUpperCase()}${side.slice(1)} Sweep ${day}`;
    }

    renderTable(plan);
    renderScrubber(plan);
  }

  const setText = (id, value) => { const n = $(id); if (n) n.textContent = value; };

  function summarise(plan) {
    const windows = Object.entries(plan.windows || {})
      .map(([which, w]) => `${which} ${w.minutes.toFixed(0)} min`).join(', ');
    return `${plan.counts.scheduled} of ${plan.counts.usable} usable`
      + (windows ? `; ${windows}` : '');
  }

  function renderTable(plan) {
    const host = $('svTable');
    if (!plan.panels.length) {
      host.innerHTML = '<p class="muted small">No fields scheduled.</p>';
      return;
    }
    const windows = Object.keys(plan.windows || {});
    // Grouped by twilight: evening and morning are runs hours apart, and one
    // list running from dusk straight into dawn reads as a mistake.
    const rows = plan.panels.map((p, i, all) => {
      const first = i === 0 || all[i - 1].window !== p.window;
      const header = (first && windows.length > 1)
        ? `<tr class="survey-group"><td colspan="9">${p.window} twilight</td></tr>`
        : '';
      return header + `
      <tr>
        <td class="mono">${p.index}</td>
        <td class="mono">${clock(p.startAt)}</td>
        <td class="mono">${p.elongation.toFixed(1)}°</td>
        <td class="mono">${p.beta >= 0 ? '+' : ''}${p.beta.toFixed(1)}°</td>
        <td class="mono">${p.altitude.toFixed(1)}°</td>
        <td class="mono">${p.airmass ? p.airmass.toFixed(2) : '—'}</td>
        <td class="mono">${p.moonDistance !== undefined ? `${p.moonDistance.toFixed(0)}°` : '—'}</td>
        <td class="mono">${p.settingRate > 0 ? '↓' : '↑'}${Math.abs(p.settingRate).toFixed(2)}</td>
        <td class="mono muted">${p.cell}</td>
      </tr>`;
    }).join('');
    host.innerHTML = `
      <table class="survey-fields">
        <thead><tr>
          <th>#</th><th>Start</th><th>Elong</th><th>Ecl lat</th><th>Alt</th>
          <th>Airmass</th><th>Moon</th><th>°/min</th><th>Cell</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  function renderScrubber(plan) {
    const which = currentWindow();
    const window_ = (plan.windows || {})[which];
    if (!window_) {
      setText('svClock', '—');
      setText('svSunAlt', '—');
      setText('svWindowLabel', 'no twilight window');
      return;
    }
    const at = window_.start + (window_.end - window_.start) * sv.fraction;
    setText('svClock', clock(at));
    setText('svWindowLabel',
      `${which} twilight ${clock(window_.start)}–${clock(window_.end)} `
      + `(${window_.minutes.toFixed(0)} min, Sun ${window_.sunHigh}° to ${window_.sunLow}°)`);
    // The Sun's altitude across the window is linear enough to interpolate.
    const alt = window_.sunHigh + (window_.sunLow - window_.sunHigh) * sv.fraction;
    setText('svSunAlt', `Sun ${alt.toFixed(1)}°`);
  }

  /* ------------------------------------------------------- viability & year */

  /* The one number that decides whether a comet sweep is worth running: how
     close to the Sun tonight's twilight actually reaches along the ecliptic.
     On the flat months the answer is "not close enough", and that is geometry,
     not scheduling. */
  async function loadViability() {
    const floor = number('svMinAlt', 5);
    try {
      const date = $('svDate').value;
      sv.viability = await app.api(
        `/api/survey/viability?floor=${floor}`
        + (date ? `&date=${date}` : ''));
    } catch (error) {
      setText('svViabilityText', error.message);
      return;
    }
    const parts = [];
    for (const which of ['morning', 'evening']) {
      const v = sv.viability[which];
      if (!v) continue;
      parts.push(v.viable
        ? `${which} reaches ${v.elongation.toFixed(0)}° at ${v.altitude.toFixed(0)}° `
          + `(ecliptic ${v.eclipticTilt.toFixed(0)}° from horizontal)`
        : `${which}: not reachable (ecliptic ${(v.eclipticTilt || 0).toFixed(0)}°)`);
    }
    setText('svViabilityText', parts.join('   ·   ') || '—');

    // Under 30° is the interesting band; under 25° is the gap the big surveys
    // leave. Say so, since it is the whole point of the exercise.
    const best = ['morning', 'evening']
      .map((w) => sv.viability[w])
      .filter((v) => v && v.viable)
      .map((v) => v.elongation);
    const node = $('svViability');
    node.classList.toggle('good', best.length > 0 && Math.min(...best) <= 30);
    node.classList.toggle('poor', !best.length || Math.min(...best) > 40);
  }

  async function loadSeason() {
    const wrap = $('svSeasonWrap');
    if (!wrap.hidden && sv.season) { wrap.hidden = true; return; }
    wrap.hidden = false;
    setText('svSeasonNote', 'working out the year…');
    try {
      const floor = number('svMinAlt', 5);
      const year = ($('svDate').value || '').slice(0, 4);
      sv.season = await app.api(`/api/survey/season?floor=${floor}`
        + (year ? `&year=${year}` : ''));
    } catch (error) {
      setText('svSeasonNote', error.message);
      return;
    }
    setText('svSeasonNote',
      'Darker is closer to the Sun and better. On the pale months the comet '
      + 'zone is below the horizon — not a scheduling problem, geometry.');
    drawSeason();
  }

  function drawSeason() {
    const canvas = $('svSeason');
    if (!canvas || !sv.season || canvas.offsetParent === null) return;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 800;
    const height = canvas.clientHeight || 92;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const days = sv.season.days || [];
    if (!days.length) return;
    const pad = { left: 54, right: 8, top: 6, bottom: 16 };
    const w = (width - pad.left - pad.right) / days.length;
    const rowHeight = (height - pad.top - pad.bottom) / 2;

    ['morning', 'evening'].forEach((which, row) => {
      const y = pad.top + row * rowHeight;
      days.forEach((day, index) => {
        const entry = day[which] || {};
        const x = pad.left + index * w;
        if (!entry.viable || entry.elongation === null) {
          ctx.fillStyle = '#151a23';
        } else {
          // 15 deg (excellent) to 45 deg (marginal).
          const t = Math.max(0, Math.min(1, (entry.elongation - 15) / 30));
          const light = Math.round(40 + t * 130);
          ctx.fillStyle = `rgb(${Math.round(75 + t * 60)}, ${185 - t * 60}, ${light})`;
        }
        ctx.fillRect(x, y + 1, Math.max(1, w - 0.5), rowHeight - 3);
      });
      ctx.fillStyle = '#8b94a7';
      ctx.font = '11px ui-monospace, monospace';
      ctx.textAlign = 'right';
      ctx.fillText(which, pad.left - 6, y + rowHeight / 2 + 4);
    });

    ctx.textAlign = 'center';
    ctx.fillStyle = '#5c6577';
    for (let month = 0; month < 12; month += 1) {
      const index = days.findIndex((d) => Number(d.date.slice(5, 7)) === month + 1);
      if (index < 0) continue;
      const x = pad.left + index * w;
      ctx.fillText('JFMAMJJASOND'[month], x + w / 2, height - 4);
    }
  }

  /* -------------------------------------------------------------- actions */

  /** Fill the controls from one of the two survey modes. */
  function applyMode(name) {
    const mode = (sv.modes || {})[name];
    if (!mode) return;
    fillControls({ ...mode, betaMax: mode.betaMax });
    updateNotes();
    loadViability();
  }

  async function loadContext() {
    try {
      const date = $('svDate').value;
      sv.context = await app.api('/api/survey' + (date ? `?date=${date}` : ''));
      fillControls(sv.context.settings);
      setText('svCoverageCount', `${sv.context.coverage.cells} cells`);
      const field = sv.context.field;
      if (field && field.width) {
        setText('svWindowLabel',
          `tiling at ${field.width.toFixed(2)}° × ${field.height.toFixed(2)}°`
          + (field.limitedBy ? `, limited by ${field.limitedBy}` : ''));
      }
      updateNotes();
    } catch (error) {
      console.error(error);
    }
  }

  async function planSweep() {
    if (sv.loading) return;
    sv.loading = true;
    $('btnSvPlan').disabled = true;
    $('btnSvPlan').textContent = 'Planning…';
    try {
      const body = { ...settings(), date: $('svDate').value || undefined };
      sv.plan = await app.api('/api/survey/plan', 'POST', body);
      sv.saved = null;
      render();
    } catch (error) {
      app.toast(error.message, 'error');
      setText('svSummary', error.message);
    } finally {
      sv.loading = false;
      $('btnSvPlan').disabled = false;
      $('btnSvPlan').textContent = 'Plan sweep';
    }
  }

  async function saveSweep() {
    if (!sv.plan || !sv.plan.panels.length) return;
    const name = ($('svName').value || '').trim();
    if (!name) { app.toast('Give the sweep a name', 'error'); return; }
    try {
      const result = await app.api('/api/survey/save', 'POST', {
        name,
        date: $('svDate').value || undefined,
        position: $('svPosition').value,
        settings: settings(),
      });
      sv.saved = result;
      app.toast(`Saved ${result.target.panels.length} fields to the plan`, 'success');
      setText('svSummary', result.summary);
    } catch (error) {
      app.toast(error.message, 'error');
    }
  }

  /* --------------------------------------------------------------- wiring */

  /* Zoom and pan, so the field footprints can actually be inspected: at full
     sky they are a couple of degrees on a 180-degree chart. */
  function bindChartView() {
    const canvas = $('svChart');
    const setZoom = (value, ax, ay) => {
      const next = Math.max(1, Math.min(24, value));
      if (next === sv.zoom) return;
      // Keep whatever is under the cursor under the cursor.
      if (ax !== undefined) {
        const rect = canvas.getBoundingClientRect();
        const midX = rect.width / 2;
        const midY = rect.height / 2;
        const scale = next / sv.zoom;
        sv.panX = ax - midX - (ax - midX - sv.panX) * scale;
        sv.panY = ay - midY - (ay - midY - sv.panY) * scale;
      }
      sv.zoom = next;
      if (sv.zoom === 1) { sv.panX = 0; sv.panY = 0; }
      setText('svZoomLabel', `${sv.zoom.toFixed(1)}×`);
      drawChart();
    };

    canvas.addEventListener('wheel', (event) => {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      setZoom(sv.zoom * (event.deltaY < 0 ? 1.15 : 1 / 1.15),
        event.clientX - rect.left, event.clientY - rect.top);
    }, { passive: false });

    let dragging = null;
    canvas.addEventListener('mousedown', (event) => {
      dragging = { x: event.clientX, y: event.clientY,
        panX: sv.panX, panY: sv.panY };
      canvas.style.cursor = 'grabbing';
    });
    window.addEventListener('mousemove', (event) => {
      if (!dragging) return;
      sv.panX = dragging.panX + (event.clientX - dragging.x);
      sv.panY = dragging.panY + (event.clientY - dragging.y);
      drawChart();
    });
    window.addEventListener('mouseup', () => {
      dragging = null;
      canvas.style.cursor = '';
    });
    canvas.addEventListener('dblclick', () => setZoom(sv.zoom * 1.6));

    $('btnSvZoomIn').addEventListener('click', () => setZoom(sv.zoom * 1.4));
    $('btnSvZoomOut').addEventListener('click', () => setZoom(sv.zoom / 1.4));
    $('btnSvZoomReset').addEventListener('click', () => {
      sv.panX = 0; sv.panY = 0; setZoom(1);
      setText('svZoomLabel', '1.0×');
      drawChart();
    });
    $('svShowWindow').addEventListener('change', () => {
      sv.showWindow = $('svShowWindow').value;
      if (sv.plan) { drawChart(); renderScrubber(sv.plan); }
    });
  }

  /** Let the geometry settle the parameters, then plan straight away. */
  async function optimiseSweep() {
    const button = $('btnSvOptimise');
    button.disabled = true;
    button.textContent = 'Working it out…';
    try {
      // Honour the Side selector: with morning or evening picked this finds
      // the best sweep on *that* side. Leave it on Both to let the geometry
      // choose the half as well.
      const side = $('svSide').value;
      const result = await app.api('/api/survey/optimise', 'POST', {
        date: $('svDate').value || undefined,
        side: (side === 'morning' || side === 'evening') ? side : undefined,
      });
      fillControls(result.settings);
      $('svMode').value = result.mode;
      $('svSide').value = result.side;

      const advice = $('svAdvice');
      advice.hidden = false;
      const alternatives = (result.alternatives || [])
        .map((a) => `${a.which} ${a.mode}`
          + (a.elongation ? ` (${a.elongation.toFixed(0)}°)` : ''))
        .join(', ');
      advice.innerHTML =
        `<b>${result.reasons[0]}</b>`
        + result.reasons.slice(1).map((r) => `<span>${r}</span>`).join('')
        + (alternatives ? `<span class="muted">Also possible: ${alternatives}`
          + '</span>' : '');
      updateNotes();
      await planSweep();
    } catch (error) {
      app.toast(error.message, 'error');
      const advice = $('svAdvice');
      advice.hidden = false;
      advice.textContent = error.message;
    } finally {
      button.disabled = false;
      button.textContent = 'Best for tonight';
    }
  }

  function bind() {
    bindChartView();
    $('btnSvOptimise').addEventListener('click', optimiseSweep);
    $('svShowGrid').addEventListener('change', () => {
      sv.showGrid = $('svShowGrid').checked;
      drawChart();
    });
    $('svMode').addEventListener('change', () => applyMode($('svMode').value));
    $('btnSvSeason').addEventListener('click', loadSeason);
    $('btnSvPlan').addEventListener('click', planSweep);
    $('btnSvSave').addEventListener('click', saveSweep);
    $('btnSvTonight').addEventListener('click', () => {
      $('svDate').value = '';
      loadContext().then(planSweep);
    });
    $('svDate').addEventListener('change', () => {
      loadContext();
      loadViability();
      if (sv.season) { sv.season = null; $('svSeasonWrap').hidden = true; }
    });
    $('svTime').addEventListener('input', () => {
      sv.fraction = Number($('svTime').value) / 100;
      // Redraw, not just relabel: the point of the scrubber is watching the
      // fields and the ecliptic move against the horizon through the window.
      if (sv.plan) { renderScrubber(sv.plan); drawChart(); }
    });
    $('btnSvClearCoverage').addEventListener('click', async () => {
      const ok = await app.confirmAction(
        'Clear the survey coverage history? The "not shot in the last N nights" '
        + 'filter will have nothing to work from until fields are shot again.',
        { title: 'Clear coverage', confirmLabel: 'Clear', danger: true });
      if (!ok) return;
      await app.send('/api/survey/coverage', 'DELETE', null, 'Coverage cleared');
      loadContext();
    });

    // Warnings track the controls as they are typed, not only on plan.
    for (const id of ['svMinAlt', 'svCount', 'svExposure', 'svDither']) {
      $(id).addEventListener('input', updateNotes);
      $(id).addEventListener('change', updateNotes);
    }
    // The reachable elongation depends on the altitude floor, so it has to
    // follow that control rather than only the date.
    $('svMinAlt').addEventListener('change', loadViability);
    window.addEventListener('resize', () => {
      if (sv.plan) drawChart();
      if (sv.season) drawSeason();
    });
  }

  function init() {
    bind();
    updateNotes();
    app.api('/api/survey/modes')
      .then((result) => { sv.modes = result.modes; })
      .catch(() => {});
    app.onStatus((status, tab) => {
      if (tab === 'survey') {
        if (!sv.context) { loadContext(); loadViability(); }
        drawChart();
      }
    });
  }

  document.addEventListener('DOMContentLoaded', init);
}());
