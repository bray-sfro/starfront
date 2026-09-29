/* Starfront imaging plan.
 *
 * One box per target, each showing the altitude it will reach between sunset
 * and sunrise and how much of that night is actually usable. Frame counts are
 * capped by that window: for a mosaic the cap accounts for every tile, since
 * twenty frames a panel on a four-panel mosaic is eighty frames of sky time.
 *
 * The plan writes itself to disk on every change and is read back at startup.
 */
'use strict';

(function () {
  const app = window.astro;
  const $ = app.$;

  const sched = {
    data: null,          // the whole /api/plan payload
    loading: false,
    dirty: new Map(),    // "entryId:rigId" -> pending save timer
    // Which telescope's allocation each entry is showing. Per entry, so
    // switching to the second scope on one target does not switch it on all.
    rigTab: new Map(),
    // The last Auto-arrange result, kept so its reasoning stays on screen
    // while the allocations it wrote are looked over.
    arranged: null,
    // Saved sequences, the night the plan on screen was worked out for, and
    // the night this page last saw by its own clock.
    sequences: [],
    night: null,
    clientNight: null,
  };

  const HOUR = 3600;

  /* ------------------------------------------------------------ formatting */

  const clock = (ts) => (!ts ? '--:--'
    : new Date(ts * 1000).toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', hour12: false,
    }));

  function duration(seconds) {
    if (!seconds || seconds <= 0) return '0m';
    const hours = Math.floor(seconds / HOUR);
    const minutes = Math.round((seconds % HOUR) / 60);
    return hours ? `${hours}h ${String(minutes).padStart(2, '0')}m` : `${minutes}m`;
  }

  /* ---------------------------------------------------- the composite night */

  /* Distinct at a glance and distinct in the dark. Deliberately not a rainbow:
     these sit over a near-black plot under red light, and hues that differ only
     in saturation vanish. */
  const TRACK_COLOURS = ['#5b8dd9', '#4bb87a', '#d9a03d', '#c07ad9', '#4bc0c8',
    '#d97a7a', '#8fd94b', '#d94fa0'];

  /* One picture of the whole night.
   *
   * Every target's altitude curve on one pair of axes, the Moon drawn as a
   * filled shape underneath them, and a lane along the foot saying which target
   * the telescope is on at each moment. The two questions it answers are the
   * ones a stack of per-target graphs cannot: is the Moon going to be sitting
   * on top of the thing I am shooting, and how much of the night is nobody
   * using. */
  function drawNight(canvas, data) {
    const night = data.night;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 760;
    const height = canvas.clientHeight || 230;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!night) return;

    const t0 = night.sunset || night.windowStart;
    const t1 = night.sunrise || night.windowEnd;
    if (!t1 || t1 <= t0) return;

    const laneHeight = 26;
    const pad = { left: 32, right: 10, top: 10, bottom: 18 };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom - laneHeight - 6;
    const laneTop = pad.top + plotHeight + 6;

    const x = (t) => pad.left + (t - t0) / (t1 - t0) * plotWidth;
    const y = (alt) => pad.top + (90 - Math.max(0, Math.min(90, alt))) / 90 * plotHeight;

    // Twilight: three bands, darkest in the middle, so how much real dark the
    // night has is a thing you see rather than a number you read.
    ctx.fillStyle = '#161b26';
    ctx.fillRect(pad.left, pad.top, plotWidth, plotHeight);
    for (const [from, to, shade] of [
      [night.duskCivil, night.dawnCivil, '#111622'],
      [night.duskNautical, night.dawnNautical, '#0c101a'],
      [night.duskAstronomical, night.dawnAstronomical, '#070910'],
    ]) {
      if (!from || !to || to <= from) continue;
      ctx.fillStyle = shade;
      ctx.fillRect(x(from), pad.top, x(to) - x(from), plotHeight);
    }

    // The Moon, as a filled shape under its own curve. Filled rather than a
    // line because it is not a target to compare against the others — it is a
    // condition sitting over the whole night, and it should read as weather.
    const moon = data.moon || {};
    const curve = (moon.curve || []).filter((p) => p.t >= t0 && p.t <= t1);
    if (curve.length) {
      const lit = Math.max(0.08, Number(moon.illumination) || 0);
      ctx.beginPath();
      ctx.moveTo(x(curve[0].t), y(0));
      for (const point of curve) ctx.lineTo(x(point.t), y(Math.max(0, point.alt)));
      ctx.lineTo(x(curve[curve.length - 1].t), y(0));
      ctx.closePath();
      ctx.fillStyle = `rgba(232, 226, 196, ${0.05 + lit * 0.17})`;
      ctx.fill();
      ctx.beginPath();
      let drawing = false;
      for (const point of curve) {
        if (point.alt < 0) { drawing = false; continue; }
        const px = x(point.t);
        const py = y(point.alt);
        if (drawing) ctx.lineTo(px, py); else { ctx.moveTo(px, py); drawing = true; }
      }
      ctx.strokeStyle = `rgba(232, 226, 196, ${0.3 + lit * 0.5})`;
      ctx.lineWidth = 1.4;
      ctx.setLineDash([5, 3]);
      ctx.stroke();
      ctx.setLineDash([]);

      if (moon.peakTime && moon.maxAltitude > 2) {
        ctx.fillStyle = `rgba(240, 236, 214, ${0.45 + lit * 0.5})`;
        ctx.font = '10px ui-monospace, monospace';
        ctx.textAlign = 'center';
        ctx.fillText(`☾ ${Math.round(lit * 100)}%`,
          x(moon.peakTime), Math.max(pad.top + 10, y(moon.maxAltitude) - 6));
      }
    }

    // Altitude grid and the floor below which nothing is shot.
    ctx.strokeStyle = '#232936';
    ctx.lineWidth = 1;
    ctx.font = '9px ui-monospace, monospace';
    ctx.textAlign = 'right';
    for (const altitude of [30, 60, 90]) {
      const yy = Math.round(y(altitude)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(pad.left, yy);
      ctx.lineTo(width - pad.right, yy);
      ctx.stroke();
      ctx.fillStyle = '#5c6577';
      ctx.fillText(`${altitude}°`, pad.left - 4, yy + 3);
    }
    ctx.strokeStyle = 'rgba(217, 79, 61, 0.45)';
    ctx.setLineDash([4, 3]);
    const floor = Math.round(y(data.minAltitude)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.left, floor);
    ctx.lineTo(width - pad.right, floor);
    ctx.stroke();
    ctx.setLineDash([]);

    // Every target's curve, in the colour its lane below will use.
    const shooting = (data.entries || []).filter(
      (e) => e.kind !== 'calibration' && e.curve && e.curve.length);
    shooting.forEach((entry, index) => {
      const skipped = (entry.options || {}).enabled === false;
      ctx.beginPath();
      let drawing = false;
      for (const point of entry.curve) {
        if (point.t < t0 || point.t > t1) continue;
        if (point.alt < 0) { drawing = false; continue; }
        const px = x(point.t);
        const py = y(point.alt);
        if (drawing) ctx.lineTo(px, py); else { ctx.moveTo(px, py); drawing = true; }
      }
      ctx.strokeStyle = TRACK_COLOURS[index % TRACK_COLOURS.length];
      ctx.globalAlpha = skipped ? 0.22 : 0.9;
      ctx.lineWidth = skipped ? 1 : 1.6;
      // Dotted, not dashed: the Moon is already the dashed line on this plot,
      // and two dashed greys are one too many to tell apart at a glance.
      if (skipped) ctx.setLineDash([1, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
    });

    // Hour ticks along the foot of the lane.
    ctx.textAlign = 'center';
    ctx.fillStyle = '#5c6577';
    const firstHour = Math.ceil(t0 / HOUR) * HOUR;
    for (let t = firstHour; t <= t1; t += HOUR) {
      const px = x(t);
      ctx.fillText(clock(t), px, height - 5);
      ctx.strokeStyle = '#1c2330';
      ctx.beginPath();
      ctx.moveTo(Math.round(px) + 0.5, pad.top);
      ctx.lineTo(Math.round(px) + 0.5, pad.top + plotHeight);
      ctx.stroke();
    }

    drawUtilisation(ctx, { data, shooting, x, laneTop, laneHeight, pad,
      plotWidth, t0, t1, night });

    // Now, if the night is in progress.
    const now = Date.now() / 1000;
    if (now > t0 && now < t1) {
      ctx.strokeStyle = '#4bb87a';
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.moveTo(Math.round(x(now)) + 0.5, pad.top);
      ctx.lineTo(Math.round(x(now)) + 0.5, laneTop + laneHeight);
      ctx.stroke();
    }
  }

  /* The lane along the foot: what the telescope is on, and when it is on
     nothing. The gaps are the point — an evening with two targets and three
     hours of nobody using the sky is a night you would want to fill. */
  function drawUtilisation(ctx, geom) {
    const { data, shooting, x, laneTop, laneHeight, pad, plotWidth, t0, t1,
      night } = geom;

    ctx.fillStyle = '#0d1017';
    ctx.fillRect(pad.left, laneTop, plotWidth, laneHeight);

    // Only the dark hours count as usable, so idle time is measured against
    // those rather than against sunset-to-sunrise.
    const dusk = night.duskAstronomical || t0;
    const dawn = night.dawnAstronomical || t1;
    ctx.fillStyle = '#151a24';
    ctx.fillRect(x(dusk), laneTop, Math.max(0, x(dawn) - x(dusk)), laneHeight);

    ctx.font = '9.5px ui-monospace, monospace';
    ctx.textBaseline = 'middle';
    const slots = [];
    shooting.forEach((entry, index) => {
      if (!entry.startAt && !entry.endAt) return;
      if ((entry.options || {}).enabled === false) return;
      const from = Math.max(t0, entry.startAt || dusk);
      const to = Math.min(t1, entry.endAt || dawn);
      if (to <= from) return;
      slots.push({ from, to, name: entry.name,
        colour: TRACK_COLOURS[index % TRACK_COLOURS.length] });
    });
    slots.sort((a, b) => a.from - b.from);

    for (const slot of slots) {
      const left = x(slot.from);
      const wide = Math.max(2, x(slot.to) - left);
      ctx.fillStyle = slot.colour;
      ctx.globalAlpha = 0.82;
      ctx.fillRect(left, laneTop + 3, wide, laneHeight - 6);
      ctx.globalAlpha = 1;
      // The name only when it fits; a clipped label is worse than none.
      const label = slot.name;
      if (ctx.measureText(label).width + 10 < wide) {
        ctx.fillStyle = '#0a0c11';
        ctx.textAlign = 'center';
        ctx.fillText(label, left + wide / 2, laneTop + laneHeight / 2);
      }
    }

    // The gaps, hatched, with the biggest one given its length in words.
    let cursor = dusk;
    let idle = 0;
    let longest = null;
    for (const slot of [...slots, { from: dawn, to: dawn }]) {
      if (slot.from > cursor) {
        const gap = { from: cursor, to: Math.min(slot.from, dawn) };
        if (gap.to > gap.from) {
          idle += gap.to - gap.from;
          if (!longest || (gap.to - gap.from) > (longest.to - longest.from)) {
            longest = gap;
          }
          const left = x(gap.from);
          const wide = x(gap.to) - left;
          ctx.strokeStyle = 'rgba(123, 134, 152, 0.4)';
          ctx.lineWidth = 1;
          for (let px = left; px < left + wide; px += 5) {
            ctx.beginPath();
            ctx.moveTo(px, laneTop + laneHeight - 3);
            ctx.lineTo(Math.min(px + 7, left + wide), laneTop + 3);
            ctx.stroke();
          }
        }
      }
      cursor = Math.max(cursor, slot.to);
    }
    if (longest && (longest.to - longest.from) > 900) {
      const left = x(longest.from);
      const wide = x(longest.to) - left;
      const label = `${duration(longest.to - longest.from)} idle`;
      if (ctx.measureText(label).width + 8 < wide) {
        ctx.fillStyle = '#7b8698';
        ctx.textAlign = 'center';
        ctx.fillText(label, left + wide / 2, laneTop + laneHeight / 2);
      }
    }
    ctx.textBaseline = 'alphabetic';

    geom.data.idleSeconds = idle;
  }

  /** Who is who on the composite, and how much of the dark is being used. */
  function fillNightLegend(data) {
    const host = $('nightLegend');
    if (!host) return;
    host.innerHTML = '';
    const night = data.night;
    if (!night) return;

    const shooting = (data.entries || []).filter(
      (e) => e.kind !== 'calibration' && e.curve && e.curve.length);
    shooting.forEach((entry, index) => {
      const skipped = (entry.options || {}).enabled === false;
      const chip = document.createElement('span');
      chip.className = `night-key${skipped ? ' skipped' : ''}`;
      const swatch = document.createElement('i');
      swatch.style.background = TRACK_COLOURS[index % TRACK_COLOURS.length];
      chip.appendChild(swatch);
      chip.appendChild(document.createTextNode(entry.name));
      const moon = entry.moon || {};
      chip.title = moon.closest !== null && moon.closest !== undefined
        ? `The Moon comes within ${moon.closest}° of it tonight`
        : '';
      host.appendChild(chip);
    });

    const moon = data.moon || {};
    const sky = data.sky || {};
    const summary = document.createElement('span');
    summary.className = 'night-key moon';
    summary.innerHTML = '<i class="moon-swatch"></i>';
    summary.appendChild(document.createTextNode(
      moon.upMinutes ? `Moon ${Math.round((moon.illumination || 0) * 100)}%, `
        + `up ${duration(moon.upMinutes * 60)}`
        : `Moon ${Math.round((moon.illumination || 0) * 100)}%, down all night`));
    summary.title = sky.summary || '';
    host.appendChild(summary);

    const dark = (night.darkMinutes || 0) * 60;
    const idle = Math.max(0, data.idleSeconds || 0);
    const used = Math.max(0, dark - idle);
    const usage = document.createElement('span');
    usage.className = `night-key usage${idle > dark * 0.25 ? ' poor' : ''}`;
    usage.textContent = dark
      ? `${duration(used)} of ${duration(dark)} dark used`
        + (idle > 60 ? ` · ${duration(idle)} idle` : ' · fully booked')
      : 'no astronomical dark tonight';
    host.appendChild(usage);
  }

  /* What Auto-arrange decided, in the words it decided it in.
   *
   * Shown until the next arrange or a reload. An allocation that arrives
   * without a reason is one the operator has to check by hand, which is most
   * of the work the arranger was meant to save — and the reasons are short
   * because the rules are. */
  function fillArrangeReport(data) {
    const host = $('arrangeReport');
    if (!host) return;
    const result = sched.arranged;
    if (!result || !result.chooseExposures) { host.hidden = true; return; }
    host.hidden = false;
    host.innerHTML = '';

    const head = document.createElement('div');
    head.className = 'arrange-head';
    head.innerHTML = '<b>Auto-arrange</b> <span class="sky"></span>'
      + '<span class="spacer"></span>'
      + '<button class="btn small ghost" id="btnArrangeDismiss">Dismiss</button>';
    head.querySelector('.sky').textContent = (result.sky || {}).summary || '';
    host.appendChild(head);

    const names = new Map((data.entries || []).map((e) => [e.id, e.name]));
    for (const item of result.chosen || []) {
      const row = document.createElement('div');
      row.className = 'arrange-row';
      const shot = (item.filters || [])
        .map((f) => `${f.name} ${f.count}×${f.exposure}s`).join('  ');
      row.innerHTML = '<b class="who"></b><span class="what mono"></span>'
        + '<span class="why muted"></span>';
      row.querySelector('.who').textContent = names.get(item.id) || item.name;
      row.querySelector('.what').textContent = shot || 'nothing';
      row.querySelector('.why').textContent = (item.notes || []).join(' · ');
      row.classList.toggle('empty', !shot);
      host.appendChild(row);
    }
    for (const item of result.unplaced || []) {
      const row = document.createElement('div');
      row.className = 'arrange-row empty';
      row.innerHTML = '<b class="who"></b><span class="what mono">no slot</span>'
        + '<span class="why muted"></span>';
      row.querySelector('.who').textContent = item.name;
      row.querySelector('.why').textContent = item.reason;
      host.appendChild(row);
    }

    $('btnArrangeDismiss').addEventListener('click', () => {
      sched.arranged = null;
      host.hidden = true;
    });
  }

  /* -------------------------------------------------------- the night graph */

  /** Altitude from sunset to sunrise, with the twilight and usable bands. */
  function drawTransit(canvas, entry, night, minAltitude) {
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 460;
    const height = canvas.clientHeight || 120;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const pad = { left: 30, right: 8, top: 8, bottom: 16 };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom;
    const t0 = entry.curveStart;
    const t1 = entry.curveEnd;
    if (!t1 || t1 <= t0) return;

    const x = (t) => pad.left + (t - t0) / (t1 - t0) * plotWidth;
    const y = (alt) => pad.top + (90 - Math.max(0, Math.min(90, alt))) / 90 * plotHeight;

    // Sky background: darkest between astronomical dusk and dawn.
    ctx.fillStyle = '#12161f';
    ctx.fillRect(pad.left, pad.top, plotWidth, plotHeight);
    if (night.duskAstronomical && night.dawnAstronomical) {
      ctx.fillStyle = '#080a10';
      ctx.fillRect(x(night.duskAstronomical), pad.top,
        x(night.dawnAstronomical) - x(night.duskAstronomical), plotHeight);
    }

    // The window this target is actually shootable in.
    ctx.fillStyle = 'rgba(75, 184, 122, 0.16)';
    for (const interval of entry.window.intervals || []) {
      ctx.fillRect(x(interval.start), pad.top,
        Math.max(1, x(interval.end) - x(interval.start)), plotHeight);
    }

    // Altitude gridlines, and the limit below which we will not shoot.
    ctx.strokeStyle = '#232936';
    ctx.lineWidth = 1;
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = '#5c6577';
    ctx.textAlign = 'right';
    for (const altitude of [30, 60, 90]) {
      const yy = Math.round(y(altitude)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(pad.left, yy);
      ctx.lineTo(width - pad.right, yy);
      ctx.stroke();
      ctx.fillText(`${altitude}°`, pad.left - 4, yy + 3);
    }
    ctx.strokeStyle = 'rgba(217, 79, 61, 0.55)';
    ctx.setLineDash([4, 3]);
    const limit = Math.round(y(minAltitude)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.left, limit);
    ctx.lineTo(width - pad.right, limit);
    ctx.stroke();
    ctx.setLineDash([]);

    // Hour ticks.
    ctx.textAlign = 'center';
    ctx.fillStyle = '#5c6577';
    const firstHour = Math.ceil(t0 / HOUR) * HOUR;
    for (let t = firstHour; t <= t1; t += 2 * HOUR) {
      ctx.fillText(clock(t), x(t), height - 4);
    }

    // The curve itself.
    ctx.beginPath();
    let started = false;
    for (const point of entry.curve) {
      const px = x(point.t);
      const py = y(point.alt);
      if (point.alt < 0) { started = false; continue; }
      if (started) ctx.lineTo(px, py); else { ctx.moveTo(px, py); started = true; }
    }
    ctx.strokeStyle = '#5b8dd9';
    ctx.lineWidth = 1.8;
    ctx.stroke();

    // When this target actually runs.
    //
    // Drawn by dimming everything *outside* the slot rather than tinting what
    // is inside it. A tint over a graph that already has a green window band
    // and a blue curve is one more colour to disentangle; taking the light out
    // of the hours the telescope is elsewhere leaves the slot as the only lit
    // part of the picture, which is what it is.
    drawSlot(ctx, entry, { x, pad, plotWidth, plotHeight, t0, t1 });

    // Transit marker.
    if (entry.window.transitTime && entry.window.maxAltitude > 0) {
      const tx = x(entry.window.transitTime);
      const ty = y(entry.window.maxAltitude);
      ctx.fillStyle = '#8fb6ef';
      ctx.beginPath();
      ctx.arc(tx, ty, 2.6, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  /* The start and end of a target's slot, made impossible to miss.
   *
   * These are the two numbers the graph exists to let you set, and as a pair of
   * hairlines they were the faintest thing on it. Now the hours the telescope
   * spends elsewhere are dimmed out, each edge carries a labelled flag, and the
   * run between them is underlined along the foot of the plot. */
  function drawSlot(ctx, entry, geom) {
    const { x, pad, plotWidth, plotHeight, t0, t1 } = geom;
    const start = entry.startAt;
    const end = entry.endAt;
    if (!start && !end) return;

    const from = x(Math.max(t0, start || t0));
    const to = x(Math.min(t1, end || t1));
    const top = pad.top;
    const bottom = pad.top + plotHeight;

    // Dim the hours outside the slot.
    ctx.fillStyle = 'rgba(8, 10, 16, 0.62)';
    if (start && from > pad.left) ctx.fillRect(pad.left, top, from - pad.left, plotHeight);
    if (end && to < pad.left + plotWidth) {
      ctx.fillRect(to, top, pad.left + plotWidth - to, plotHeight);
    }

    // The run itself, underlined along the foot.
    ctx.fillStyle = 'rgba(126, 231, 165, 0.85)';
    ctx.fillRect(from, bottom - 3, Math.max(2, to - from), 3);

    ctx.font = '9px ui-monospace, monospace';
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = '#7ee7a5';

    for (const [at, present, side] of [[from, start, 'start'], [to, end, 'end']]) {
      if (!present) continue;
      const px = Math.round(at) + 0.5;
      ctx.beginPath();
      ctx.moveTo(px, top);
      ctx.lineTo(px, bottom);
      ctx.stroke();

      // A flag that points into the slot, so which side is which is readable
      // without reading the clock underneath it.
      const dir = side === 'start' ? 1 : -1;
      ctx.fillStyle = '#7ee7a5';
      ctx.beginPath();
      ctx.moveTo(px, top);
      ctx.lineTo(px + dir * 9, top + 4.5);
      ctx.lineTo(px, top + 9);
      ctx.closePath();
      ctx.fill();

      const label = clock(present);
      const wide = ctx.measureText(label).width + 6;
      // Kept inside the plot at either end, so a slot that starts at sunset
      // does not print its time off the edge of the canvas.
      let boxX = side === 'start' ? px + 11 : px - 11 - wide;
      boxX = Math.max(pad.left + 1, Math.min(boxX, pad.left + plotWidth - wide - 1));
      ctx.fillStyle = 'rgba(217, 79, 61, 0.92)';
      ctx.fillRect(boxX, top + 0.5, wide, 11);
      ctx.fillStyle = '#12151c';
      ctx.textAlign = 'left';
      ctx.fillText(label, boxX + 3, top + 9);
    }
    ctx.textAlign = 'center';
  }

  /* ------------------------------------------------------------ tonight */

  /** What this target will actually get through before its window closes.
   *
   *  The allocation is what was asked for. On a mosaic bigger than one night
   *  the two are very different numbers, and only one of them is a plan: a
   *  forty-panel collaboration chunk with five hours of sky is forty-five hours
   *  of work, and reporting only the ask is how somebody finds out in the
   *  morning that thirty-six panels were never started.
   *
   *  Shown on the closed box, because that is the box people look at.
   */
  function buildTonight(entry, data) {
    const perRig = entry.tonight || {};
    const master = data.masterRig;
    // The master's forecast is the one the box speaks for; a rig with filters
    // of its own is called out in the expanded detail, not here.
    const plan = perRig[master] || perRig[Object.keys(perRig)[0]];
    if (!plan) return null;

    const line = document.createElement('div');
    line.className = 'plan-tonight small';

    if (plan.nothing) {
      // Distinguish the two ways of getting nothing. "You planned nothing" and
      // "there is no room for what you planned" want opposite actions.
      const anything = Object.values(entry.allocations || {})
        .some((rows) => (rows || []).some((row) => row.count > 0));
      line.classList.add('none');
      line.innerHTML = anything
        ? '<b>Tonight</b> nothing fits — the window is shorter than one panel'
        : '<b>Tonight</b> nothing planned';
      return line;
    }

    const frames = Object.entries(plan.frames)
      .map(([name, count]) => `${count}× ${name}`).join(', ');

    const panels = plan.totalPanels > 1
      ? `<b class="mono">${plan.panels.join(', ')}</b>`
        + ` <span class="muted">of ${plan.totalPanels}</span>`
      : '';

    line.innerHTML = '<b>Tonight</b> '
      + `<span class="mono">${frames}</span>`
      + `   <span class="muted">·</span>   ${duration(plan.seconds)}`
      + (panels ? `   <span class="muted">·</span>   panel${
        plan.panels.length === 1 ? '' : 's'} ${panels}` : '')
      + (plan.partial
        ? `   <span class="muted">· panel ${plan.partial.panel} is cut short at `
          + `${plan.partial.frames} frame${plan.partial.frames === 1 ? '' : 's'}`
          + '</span>'
        : '');

    // How long on each of them. "Panels 1, 2, 3" says where the telescope
    // goes; this says how long it stays, which is the other half of the
    // question and the one that decides whether a panel is worth starting.
    if (plan.totalPanels > 1 && plan.complete) {
      const cut = (plan.partial && plan.partial.seconds) || 0;
      const each = (plan.seconds - cut) / plan.complete;
      line.innerHTML += `<div class="muted">About ${duration(each)} on each`
        + (cut
          ? `, and ${duration(cut)} on panel ${plan.partial.panel} before the `
            + 'window closes' : '')
        + '.</div>';
    }

    if (!plan.fits && plan.totalPanels > 1) {
      const left = plan.totalPanels - plan.complete;
      line.innerHTML += `<div class="muted">${left} panel${left === 1 ? '' : 's'}`
        + ' will not be started tonight. It carries over: the panels already'
        + ' shot are logged, and the capture order picks up where the sky'
        + ' leaves it.</div>';
    }
    return line;
  }

  /* ------------------------------------------------------- budget maths */

  /* Mirrors schedule.plan_seconds / schedule.max_count so the inputs can show a
     ceiling without a round trip. The server clamps on save and its answer
     wins; this only needs to be close enough to guide typing. */

  const frameSeconds = (exposure, overheads) => exposure + overheads.perFrame;

  /** What autofocus costs a stretch of imaging. Mirrors schedule.focus_seconds. */
  function focusSeconds(shooting, filtersUsed, overheads) {
    const run = Number(overheads.focusRun) || 0;
    if (run <= 0 || shooting <= 0) return 0;
    const every = Number(overheads.focusEvery) || 0;
    const byClock = every > 0 ? shooting / (every * 60) : 0;
    const byFilter = overheads.focusOnFilter === false ? 0 : Math.max(0, filtersUsed);
    return run * Math.max(1, Math.max(byClock, byFilter));
  }

  function planSeconds(filters, panels, overheads) {
    let perPanel = 0;
    let used = 0;
    for (const filter of filters) {
      if (!filter.count) continue;
      used += 1;
      perPanel += filter.count * frameSeconds(filter.exposure, overheads);
    }
    if (!perPanel) return 0;
    const total = panels * (perPanel + used * overheads.filterChange + overheads.perPanel);
    return total + focusSeconds(total, used, overheads);
  }

  /* Asked of planSeconds rather than worked out separately — mirrors
     schedule.max_count, and for the same reason: the filter change a new
     channel adds and the focus sweeps the whole allocation shares are not
     per-frame costs, and any arithmetic that treats them as such disagrees
     with the budget it is meant to enforce. */
  function maxCount(exposure, panels, available, others, overheads) {
    if (available <= 0 || frameSeconds(exposure, overheads) <= 0) return 0;
    const rest = others.filter((row) => row.count > 0);
    // The name is unused by planSeconds — it counts rows, not names.
    const fits = (count) =>
      planSeconds([...rest, { name: 'probe', exposure, count }],
        panels, overheads) <= available;
    if (!fits(1)) return 0;
    let high = 1;
    while (high < 100000 && fits(high * 2)) high *= 2;
    let low = high;
    high *= 2;
    while (low < high) {
      const middle = Math.ceil((low + high) / 2);
      if (fits(middle)) low = middle; else high = middle - 1;
    }
    return low;
  }

  /* ------------------------------------------------------------- rendering */

  function render() {
    const host = $('schedList');
    const data = sched.data;
    if (!data) { host.innerHTML = '<p class="muted small">Loading…</p>'; return; }

    if (!data.night) {
      host.innerHTML = '<p class="muted small">The observing site is not set. '
        + 'Open <b>Site &amp; Optics</b> and enter your latitude and longitude, '
        + 'or connect a mount that reports them.</p>';
      $('schedNight').textContent = '—';
      return;
    }

    const night = data.night;
    $('schedNight').textContent = [
      `sunset ${clock(night.sunset)}`,
      `dark ${clock(night.duskAstronomical)} – ${clock(night.dawnAstronomical)}`,
      `sunrise ${clock(night.sunrise)}`,
      `${duration(night.darkMinutes * 60)} of astronomical dark`,
    ].join('   ·   ')
      + (night.sunAlwaysUp ? '   ·   the sun does not set tonight' : '')
      + (night.neverAstronomicallyDark ? '   ·   never fully dark tonight' : '');

    const options = $('schedAddTarget');
    const planned = new Set(data.plan.entries.map((e) => e.targetId));
    const previous = options.value;
    options.innerHTML = '';
    let addable = 0;
    for (const target of sched.targets || []) {
      if (planned.has(target.id)) continue;
      addable += 1;
      options.appendChild(new Option(
        `${target.name}${target.type === 'mosaic'
          ? ` (${target.rows}×${target.columns})`
          : target.type === 'survey'
            ? ` (sweep, ${(target.panels || []).length} fields)`
            : target.type === 'allsky' ? ' (all-sky survey)' : ''}`, target.id));
    }
    if (!addable) options.appendChild(new Option('no unplanned targets', ''));
    if (previous) options.value = previous;
    $('btnSchedAdd').disabled = !addable;

    if (!data.entries.length) {
      host.innerHTML = '<p class="muted small">Nothing planned yet. Save a framing '
        + 'in the <b>Planner</b>, then add it here — or build a recipe in '
        + '<b>Calibrate</b> and add it as a task, for darks at the end of the '
        + 'night.</p>';
      $('schedSummary').textContent = '—';
      return;
    }

    // Drawn before the boxes so the legend's idle figure, which the drawing
    // works out, is there for the summary line underneath.
    setTimeout(() => {
      drawNight($('nightGraph'), data);
      fillNightLegend(data);
      fillArrangeReport(data);
    }, 0);

    host.innerHTML = '';
    let totalSeconds = 0;
    let totalFrames = 0;
    for (const entry of data.entries) {
      totalSeconds += entry.usedSeconds;
      totalFrames += entry.frames;
      const box = buildBox(entry, data);
      addRunBadge(box, entry, data);
      host.appendChild(box);
    }
    showSummary(data, totalSeconds, totalFrames);
  }

  /* Where the run has got to, on the plan itself.
   *
   * The plan is the list of what the night is meant to do, so it is where
   * "which of these has actually happened" belongs — reading that off a banner
   * that only ever names the current target means counting backwards from the
   * log to find out whether the first three targets ran. */
  function runPosition(entry, data) {
    const sequence = (app.state.status && app.state.status.sequence) || {};
    if (!sequence.running) return null;
    const order = (data.entries || []).map((one) => one.id);
    const here = order.indexOf(entry.id);
    const now = order.indexOf(sequence.entryId);
    if (here < 0 || now < 0) return null;
    if (here === now) {
      return sequence.paused ? { label: 'paused', tone: 'paused' }
        : sequence.recovery ? { label: 'recovering', tone: 'alert' }
          : { label: sequence.state || 'running', tone: 'running' };
    }
    if (here < now) return { label: 'done', tone: 'done' };
    if (here === now + 1) return { label: 'next', tone: 'next' };
    return { label: 'queued', tone: 'queued' };
  }

  function addRunBadge(box, entry, data) {
    // A skipped target says so on the box itself, whether or not anything is
    // running: a plan you have to open three boxes to read is not a plan.
    const position = (entry.options || {}).enabled === false
      ? { label: 'skipped', tone: 'skipped' }
      : runPosition(entry, data);
    if (!position) return;
    const head = box.querySelector('.plan-head');
    if (!head) return;
    const badge = document.createElement('span');
    badge.className = `run-badge ${position.tone}`;
    badge.textContent = position.label;
    head.insertBefore(badge, head.querySelector('.remove'));
    box.classList.add(`run-${position.tone}`);
  }

  /** The whole plan has to fit the night too: the telescope points at one
   *  target at a time, so per-target windows overlapping is not enough. */
  function showSummary(data, totalSeconds, totalFrames) {
    const dark = (data.night && data.night.darkMinutes * 60) || 0;
    const node = $('schedSummary');
    const calibration = data.entries.filter((e) => e.kind === 'calibration');
    const shooting = data.entries.length - calibration.length;
    const parts = [
      `${shooting} target${shooting === 1 ? '' : 's'}`,
    ];
    if (calibration.length) {
      parts.push(`${calibration.length} calibration `
        + `task${calibration.length === 1 ? '' : 's'}`);
    }
    parts.push(`${totalFrames} frame${totalFrames === 1 ? '' : 's'}`,
      `${duration(totalSeconds)} of exposures`);
    if (dark) parts.push(`${duration(dark)} of dark`);
    node.className = '';
    if (dark && totalSeconds > dark) {
      parts.push(`OVER by ${duration(totalSeconds - dark)} — the rig can only `
        + 'shoot one target at a time');
      node.className = 'over-committed';
    }
    node.textContent = parts.join('  ·  ');
  }

  /** Turn a wall-clock time into a moment in *this* night.
   *
   *  A night straddles midnight, so "04:30" typed against a plan for the 14th
   *  means the small hours of the 15th. The night's own sunset is the anchor. */
  function nightTimestamp(night, hours, minutes) {
    const anchor = new Date(((night && night.sunset) || Date.now() / 1000) * 1000);
    const at = new Date(anchor);
    at.setHours(hours, minutes, 0, 0);
    if (hours < 12 && anchor.getHours() >= 12) at.setDate(at.getDate() + 1);
    return Math.round(at.getTime() / 1000);
  }

  /** A calibration task in the plan.
   *
   *  It has no window and no altitude curve — nothing about the sky says when
   *  to take darks — so where a target shows a transit graph this shows what it
   *  will shoot, and the time is typed rather than clicked off a curve. */
  function buildCalibrationBox(entry, data) {
    const box = document.createElement('section');
    box.className = 'plan-box plan-calibration';
    box.draggable = true;
    box.dataset.entryId = entry.id;

    const running = (app.state.status && app.state.status.sequence) || {};
    if (running.running && running.entryId === entry.id) box.classList.add('active');

    const head = document.createElement('div');
    head.className = 'plan-head';
    head.innerHTML = '<div class="name"><span class="grip">⠿</span><span></span></div>'
      + '<div class="kind"></div>'
      + '<button class="btn small ghost danger remove">Remove</button>';
    head.querySelector('.name span:last-child').textContent = entry.name;
    head.querySelector('.kind').textContent = entry.missing
      ? 'the recipe this task used has been deleted'
      : `calibration · ${entry.frames} frames on ${entry.telescope}`;
    head.querySelector('.kind').classList.toggle('warn', !!entry.missing);
    head.querySelector('.remove').addEventListener('click', async () => {
      const ok = await app.confirmAction(`Remove ${entry.name} from the plan?`,
        { title: 'Remove task', confirmLabel: 'Remove', danger: true });
      if (!ok) return;
      await app.api(`/api/plan/entries/${entry.id}`, 'DELETE').catch(() => {});
      load();
    });
    box.appendChild(head);

    const list = document.createElement('div');
    list.className = 'plan-cal-sets small mono';
    for (const spec of entry.sets || []) {
      const line = document.createElement('div');
      const bits = [`${spec.count}×`, spec.frameType];
      if (spec.frameType === 'flat' && spec.autoExposure) bits.push('(measured)');
      else if (spec.frameType === 'darkflat' && spec.followsFlat) {
        bits.push('(matching the flats)');
      } else if (spec.exposure) bits.push(`${spec.exposure}s`);
      if (spec.allFilters) bits.push('every filter');
      else if (spec.filter) bits.push(spec.filter);
      if (spec.binning > 1) bits.push(`bin${spec.binning}`);
      line.textContent = bits.join(' ');
      const cost = document.createElement('span');
      cost.className = 'muted';
      cost.textContent = `   ${duration(spec.seconds)}`;
      line.appendChild(cost);
      list.appendChild(line);
    }
    if (!(entry.sets || []).length) {
      list.innerHTML = '<span class="muted">no sets in this recipe</span>';
    }
    box.appendChild(list);

    const note = document.createElement('div');
    note.className = 'plan-window mono small';
    note.textContent = `about ${duration(entry.usedSeconds)}`
      + '   ·   runs where it sits in this list, or at the time set below';
    box.appendChild(note);

    // Typed rather than clicked: there is no curve to click on.
    const times = document.createElement('div');
    times.className = 'plan-slot small';
    const label = document.createElement('span');
    label.className = 'muted';
    label.textContent = 'Start at';
    times.appendChild(label);

    const clockInput = document.createElement('input');
    clockInput.type = 'time';
    if (entry.startAt) {
      const when = new Date(entry.startAt * 1000);
      clockInput.value = `${String(when.getHours()).padStart(2, '0')}:`
        + `${String(when.getMinutes()).padStart(2, '0')}`;
    }
    clockInput.addEventListener('change', async () => {
      const raw = clockInput.value;
      const body = raw
        ? { startAt: nightTimestamp(data.night, Number(raw.slice(0, 2)),
          Number(raw.slice(3, 5))) }
        : { clearStart: true };
      try {
        await app.api(`/api/plan/entries/${entry.id}/times`, 'POST', body);
      } catch (error) { app.toast(error.message, 'error'); return; }
      load();
    });
    times.appendChild(clockInput);

    const state = document.createElement('span');
    state.className = 'mono muted';
    state.textContent = entry.startAt
      ? `waits until ${clock(entry.startAt)}`
      : 'no wait — runs as soon as the task before it finishes';
    times.appendChild(state);
    box.appendChild(times);

    return box;
  }

  /** An all-sky survey in the plan.
   *
   *  It is never finished and it has no window of its own — something in the
   *  grid is always up — so instead of a transit curve it shows how many fields
   *  fit in the slot it has been given, and how far the whole thing has got. */
  function buildAllSkyBox(entry, data) {
    const box = document.createElement('section');
    box.className = 'plan-box plan-allsky';
    box.draggable = true;
    box.dataset.entryId = entry.id;

    const running = (app.state.status && app.state.status.sequence) || {};
    if (running.running && running.entryId === entry.id) box.classList.add('active');

    const head = document.createElement('div');
    head.className = 'plan-head';
    head.innerHTML = '<div class="name"><span class="grip">⠿</span><span></span></div>'
      + '<div class="kind"></div>'
      + '<button class="btn small ghost danger remove">Remove</button>';
    head.querySelector('.name span:last-child').textContent = entry.name;
    const s = entry.summary || {};
    head.querySelector('.kind').textContent =
      `all-sky · ${(s.complete || 0).toLocaleString()} of `
      + `${(s.reachable || 0).toLocaleString()} fields done`;
    head.querySelector('.remove').addEventListener('click', async () => {
      const ok = await app.confirmAction(`Remove ${entry.name} from the plan? `
        + 'The survey and everything shot for it are kept.',
      { title: 'Remove from plan', confirmLabel: 'Remove', danger: true });
      if (!ok) return;
      await app.api(`/api/plan/entries/${entry.id}`, 'DELETE').catch(() => {});
      load();
    });
    box.appendChild(head);

    const bar = document.createElement('div');
    bar.className = 'as-progress';
    bar.style.margin = '8px 0 0';
    bar.innerHTML = '<div class="as-bar"><div class="as-bar-fill"></div></div>'
      + '<div class="as-stats"></div>';
    bar.querySelector('.as-bar-fill').style.width =
      `${((s.fraction || 0) * 100).toFixed(2)}%`;
    bar.querySelector('.as-stats').textContent =
      `${((s.fraction || 0) * 100).toFixed(2)}%   ·   `
      + `${(s.started || 0).toLocaleString()} part done   ·   `
      + `about ${duration(s.remainingSeconds || 0)} of clear sky left`;
    box.appendChild(bar);

    const line = document.createElement('div');
    line.className = 'plan-window mono small';
    const goal = (entry.goal || [])
      .map((g) => `${g.count}×${g.exposure}s ${g.name || 'unfiltered'}`).join(', ');
    line.textContent = [
      `${entry.fieldsTonight} field(s) in this slot`,
      goal || 'no filters set',
      entry.flips === 0 ? 'no meridian flips' : '',
      entry.telescopes > 1 ? `${entry.telescopes} telescopes` : '',
      entry.detail,
    ].filter(Boolean).join('   ·   ');
    line.classList.toggle('unusable', !entry.fieldsTonight);
    box.appendChild(line);

    // The first handful of fields, so the slot is not a black box.
    if ((entry.tonight || []).length) {
      const strip = document.createElement('div');
      strip.className = 'plan-cal-sets small mono';
      strip.textContent = entry.tonight.slice(0, 6)
        .map((f) => `${f.id} ${clock(f.startAt)} ${f.altitude.toFixed(0)}°`)
        .join('    ')
        + (entry.tonight.length > 6
          ? `    …${entry.fieldsTonight - 6} more` : '');
      box.appendChild(strip);
    }

    box.appendChild(buildSlot(entry, 'it will use whatever is left of the night'));
    return box;
  }

  function buildBox(entry, data) {
    if (entry.kind === 'calibration') return buildCalibrationBox(entry, data);
    if (entry.kind === 'allsky') return buildAllSkyBox(entry, data);

    const box = document.createElement('section');
    box.className = 'plan-box';
    box.draggable = true;
    box.dataset.entryId = entry.id;
    const window_ = entry.window;
    const unusable = !window_.intervals.length;

    const running = (app.state.status && app.state.status.sequence) || {};
    if (running.running && running.entryId === entry.id) box.classList.add('active');

    const mosaic = entry.panels > 1;
    const head = document.createElement('div');
    head.className = 'plan-head';
    // The total collected sits next to the name, in the heading, because it is
    // the number you scan the plan for: "how much of this do I have?" is asked
    // far more often than anything else on the box.
    head.innerHTML = '<div class="name"><span class="grip">⠿</span>'
      + '<span class="target-name"></span><b class="plan-total"></b></div>'
      + '<div class="kind"></div>'
      + '<button class="btn small ghost danger remove">Remove</button>';
    head.querySelector('.target-name').textContent = entry.name;
    fillTotal(head.querySelector('.plan-total'), entry);
    // A survey sweep has many panels but no rows and columns — it is a
    // scattered selection of fields, not a grid — so it needs its own label
    // rather than "undefined×undefined mosaic".
    // A panel selection is said here as well as inside the detail: a mosaic set
    // to shoot one panel looks exactly like one set to shoot all of them until
    // you open it, and that is not something to discover in the morning.
    const picked = ((entry.options || {}).panels || []);
    head.querySelector('.kind').textContent = entry.target.type === 'survey'
      ? `survey sweep · ${entry.panels} fields`
      : mosaic
        ? `${entry.target.rows}×${entry.target.columns} mosaic · `
          + (picked.length
            ? `panel${picked.length === 1 ? '' : 's'} `
              + [...picked].sort((a, b) => a - b).join(', ') + ' only'
            : `${entry.panels} panels`)
        : 'single frame';
    head.querySelector('.kind').classList.toggle('picked-panels',
      mosaic && picked.length > 0);
    head.querySelector('.remove').addEventListener('click', async () => {
      const ok = await app.confirmAction(`Remove ${entry.name} from the plan?`,
        { title: 'Remove target', confirmLabel: 'Remove', danger: true });
      if (!ok) return;
      await app.api(`/api/plan/entries/${entry.id}`, 'DELETE').catch(() => {});
      load();
    });
    box.appendChild(head);

    const graph = document.createElement('canvas');
    graph.className = 'plan-graph';
    box.appendChild(graph);

    const window_line = document.createElement('div');
    window_line.className = `plan-window mono small${unusable ? ' unusable' : ''}`;
    window_line.textContent = unusable
      ? (window_.reason || 'not observable tonight')
      : `${clock(window_.rises)} – ${clock(window_.sets)}`
        + `   ·   ${duration(window_.longestMinutes * 60)} usable`
        + `   ·   peaks ${window_.maxAltitude}° at ${clock(window_.transitTime)}`
        + (mosaic ? `   ·   ${duration(entry.perPanelSeconds)} per panel` : '');
    box.appendChild(window_line);

    if (mosaic && entry.tileOrder && entry.tileOrder.order.length) {
      const order = document.createElement('div');
      order.className = 'plan-order small';
      order.innerHTML = '<span class="muted">Capture order</span> '
        + `<b class="mono">${entry.tileOrder.order.join(' → ')}</b>`
        + (entry.tileOrder.adjacent
          ? ' <span class="muted">· neighbours only, no gradient seam</span>'
          : ' <span class="warn">· not all steps are adjacent</span>')
        + (entry.tileOrder.note
          ? ` <span class="warn">· ${entry.tileOrder.note}</span>` : '');
      box.appendChild(order);
    }

    const forecast = buildTonight(entry, data);
    if (forecast) box.appendChild(forecast);

    // A collaboration chunk shows its framing without being opened. It is a
    // piece of somebody else's sky that this telescope did not choose, and the
    // picture of what it is working against is the first thing anybody wants —
    // not something to find behind a double-click.
    if (entry.collab) {
      const framing = buildFraming(entry, data);
      framing.classList.add('framing-inline');
      box.appendChild(framing);
      setTimeout(() => showFramings(framing), 0);
    }

    box.appendChild(buildIntegration(entry));
    box.appendChild(buildSlot(entry));
    box.appendChild(buildFilters(entry, data, unusable));
    box.appendChild(buildDetail(entry, data));
    setTimeout(() => drawTransit(graph, entry, data.night, data.minAltitude), 0);

    // Double-click anywhere on the box that is not a control to open the night
    // report and this target's own settings. The box stays short by default —
    // a plan of eight targets is meant to be readable at a glance.
    box.addEventListener('dblclick', (event) => {
      if (event.target.closest('button, input, select, textarea, canvas, a')) return;
      toggleDetail(box, entry.id);
    });

    // Click the graph to pin when this target may run: left sets the start,
    // right sets the end. Both are cleared from the line underneath.
    graph.addEventListener('click', (event) => setTimeFromGraph(entry, graph, event, 'start'));
    graph.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      setTimeFromGraph(entry, graph, event, 'end');
    });
    return box;
  }

  function timeAt(entry, graph, event) {
    const rect = graph.getBoundingClientRect();
    const pad = { left: 30, right: 8 };
    const span = rect.width - pad.left - pad.right;
    const fraction = (event.clientX - rect.left - pad.left) / span;
    const clamped = Math.max(0, Math.min(1, fraction));
    return entry.curveStart + clamped * (entry.curveEnd - entry.curveStart);
  }

  async function setTimeFromGraph(entry, graph, event, which) {
    const at = Math.round(timeAt(entry, graph, event));
    const body = which === 'start' ? { startAt: at } : { endAt: at };
    try {
      await app.api(`/api/plan/entries/${entry.id}/times`, 'POST', body);
    } catch (error) { app.toast(error.message, 'error'); return; }
    load();
  }

  /* ------------------------------------------------------ saved sequences */

  /* A plan is a night's worth of decisions — which targets, how many frames of
   * what through which filter, each one's floor and Moon limit — and those are
   * worth more than one night. "The winter narrowband run" is something you
   * build once and want back in October.
   *
   * The times are deliberately not kept: they are absolute moments belonging to
   * the night they were worked out for, and restoring them in November would
   * pin every target to a slot that has been over for months. */
  async function loadSequences(select = '') {
    let payload;
    try {
      payload = await app.api('/api/sequences');
    } catch (error) { return; }
    sched.sequences = payload.sequences || [];
    const node = $('schedSequence');
    const previous = select || node.value;
    node.innerHTML = '';
    if (!sched.sequences.length) {
      node.appendChild(new Option('no saved sequences', ''));
    } else {
      node.appendChild(new Option('— saved sequences —', ''));
      for (const item of sched.sequences) {
        const when = new Date(item.saved * 1000).toLocaleDateString();
        node.appendChild(new Option(
          `${item.name} (${item.targets} target${item.targets === 1 ? '' : 's'}, ${when})`,
          item.id));
      }
    }
    if (previous) node.value = previous;
    const chosen = !!node.value;
    $('btnSeqLoad').disabled = !chosen;
    $('btnSeqDelete').disabled = !chosen;
  }

  function bindSequences() {
    $('schedSequence').addEventListener('change', () => {
      const chosen = !!$('schedSequence').value;
      $('btnSeqLoad').disabled = !chosen;
      $('btnSeqDelete').disabled = !chosen;
    });

    $('btnSeqSave').addEventListener('click', async () => {
      const current = sched.data && sched.data.plan ? sched.data.plan.name : '';
      const name = await app.askForText('Save this plan as', current || 'Tonight',
        { title: 'Save sequence', confirmLabel: 'Save' });
      if (!name) return;
      try {
        const result = await app.api('/api/sequences', 'POST', { name });
        await loadSequences(result.sequence.id);
        app.toast(`Saved '${result.sequence.name}'`, 'success');
      } catch (error) { app.toast(error.message, 'error'); }
    });

    $('btnSeqLoad').addEventListener('click', async () => {
      const id = $('schedSequence').value;
      if (!id) return;
      const item = (sched.sequences || []).find((s) => s.id === id);
      const ok = await app.confirmAction(
        `Replace tonight's plan with '${item ? item.name : id}'? `
        + 'Its targets and allocations come back; the times do not, because '
        + 'they belonged to the night it was saved for — run Auto-arrange after.',
        { title: 'Load sequence', confirmLabel: 'Load' });
      if (!ok) return;
      try {
        await app.api(`/api/sequences/${id}/load`, 'POST');
      } catch (error) { app.toast(error.message, 'error'); return; }
      sched.arranged = null;
      await load();
      app.toast('Loaded — now Auto-arrange to place it in tonight', 'success');
    });

    $('btnSeqDelete').addEventListener('click', async () => {
      const id = $('schedSequence').value;
      if (!id) return;
      const item = (sched.sequences || []).find((s) => s.id === id);
      const ok = await app.confirmAction(
        `Delete the saved sequence '${item ? item.name : id}'? `
        + "Tonight's plan is not touched.",
        { title: 'Delete sequence', confirmLabel: 'Delete', danger: true });
      if (!ok) return;
      try {
        await app.api(`/api/sequences/${id}`, 'DELETE');
      } catch (error) { app.toast(error.message, 'error'); return; }
      await loadSequences('');
    });
  }

  /* ------------------------------------------------------ the night rolls */

  /* Which night it is now, by the same noon cut the server uses: frames taken
   * after midnight belong to the evening that started the day before.
   *
   * The plan is worked out for tonight every time it is fetched, so it is
   * already right whenever the tab is opened. This is for the window left open
   * across the small hours — or across the following noon — where without it
   * the graphs would go on showing yesterday's sky until something else
   * happened to reload them. */
  function tonightName(now = new Date()) {
    const at = new Date(now);
    if (at.getHours() < 12) at.setDate(at.getDate() - 1);
    return `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, '0')}`
      + `-${String(at.getDate()).padStart(2, '0')}`;
  }

  /* Fires once when the clock crosses into a new night.
   *
   * Compared against the night *this page* last saw, not against the one the
   * server reported. Those normally agree, and when they do not — a clock a few
   * minutes out either way, either machine — keying off the server's answer
   * means the mismatch never resolves and the check fires on every status tick
   * for ever. The browser's own clock crossing noon is the event; it happens
   * once. */
  function checkNightRolled() {
    const now = tonightName();
    if (sched.clientNight === null) { sched.clientNight = now; return; }
    if (now === sched.clientNight) return;
    sched.clientNight = now;
    app.toast('A new night — the plan has been worked out again for tonight, '
      + 'with your start and end times kept at the same time of night',
      'info', 6000);
    load();
  }

  /* ------------------------------------------------ the expanded detail */

  /** How much has been collected on this target, in the heading. */
  function fillTotal(node, entry) {
    const totals = (entry.target && entry.target.integration) || {};
    const seconds = totals.seconds || 0;
    const goal = Number((entry.options || {}).goalHours || 0) * 3600;
    node.textContent = seconds ? duration(seconds) : '—';
    node.classList.toggle('none', !seconds);
    if (goal > 0) {
      const share = Math.min(1, seconds / goal);
      node.textContent = `${duration(seconds)} / ${duration(goal)}`;
      node.classList.toggle('met', share >= 1);
      node.title = share >= 1
        ? 'This target has reached its goal and will be skipped'
        : `${Math.round(share * 100)}% of its ${duration(goal)} goal`;
    } else {
      node.title = seconds
        ? `${totals.frames} frame${totals.frames === 1 ? '' : 's'} collected in total`
        : 'nothing shot on this target yet';
    }
  }

  /** Which boxes the operator has opened. Kept across a re-render. */
  const opened = new Set();

  function toggleDetail(box, entryId) {
    const detail = box.querySelector('.plan-detail');
    if (!detail) return;
    const show = detail.hidden;
    detail.hidden = !show;
    box.classList.toggle('expanded', show);
    if (show) opened.add(entryId); else opened.delete(entryId);
    // Only now does the framing canvas have a width to measure.
    if (show) showFramings(detail);
  }

  function buildDetail(entry, data) {
    const detail = document.createElement('div');
    detail.className = 'plan-detail';
    detail.hidden = !opened.has(entry.id);
    // Left open across a re-render: mark the box and draw the framing once the
    // detail is in the document and has a width.
    if (!detail.hidden) setTimeout(() => {
      const box = detail.closest('.plan-box');
      if (box) box.classList.add('expanded');
      showFramings(detail);
    }, 0);
    // A collaboration chunk already shows its framing on the closed box, so the
    // detail does not repeat it.
    if (!entry.collab) detail.appendChild(buildFraming(entry, data));
    detail.appendChild(buildNightReport(entry));
    detail.appendChild(buildOptions(entry, data));
    return detail;
  }

  /* ------------------------------------------------------- the framing */

  /* The framing this target was saved with, over a picture of the real sky.
   *
   * A target is a decision about where to point and which way up, and until now
   * that decision was three numbers on a plan. Here it is the thing itself: a
   * survey cutout of that piece of sky with the camera's field drawn on it, at
   * the angle it will actually be shot at — and for a mosaic, every panel.
   *
   * The rectangle comes from what was *saved*, not from the optics as they are
   * configured now. This is a record of the framing that was chosen, so a
   * telescope swapped since then must not silently redraw it. */

  const DEG = Math.PI / 180;

  function skyToOffset(ra0, dec0, ra, dec) {
    const r0 = ra0 * DEG, d0 = dec0 * DEG, r = ra * DEG, d = dec * DEG;
    const dRa = r - r0;
    const denominator = Math.sin(d0) * Math.sin(d)
      + Math.cos(d0) * Math.cos(d) * Math.cos(dRa);
    if (denominator <= 0) return null;
    return [
      Math.cos(d) * Math.sin(dRa) / denominator / DEG,
      (Math.sin(d) * Math.cos(d0) - Math.cos(d) * Math.sin(d0) * Math.cos(dRa))
        / denominator / DEG,
    ];
  }

  /** Which way north points at (ra, dec) in the tangent plane at (ra0, dec0). */
  function northAngle(ra0, dec0, ra, dec) {
    const epsilon = dec + 1e-4 <= 90 ? 1e-4 : -1e-4;
    const a = skyToOffset(ra0, dec0, ra, dec);
    const b = skyToOffset(ra0, dec0, ra, dec + epsilon);
    if (!a || !b) return 0;
    const dXi = epsilon > 0 ? b[0] - a[0] : a[0] - b[0];
    const dEta = epsilon > 0 ? b[1] - a[1] : a[1] - b[1];
    return Math.atan2(dXi, dEta) / DEG;
  }

  /** Every panel this target is made of, in degrees of RA and Dec. */
  function framingPanels(target) {
    const panels = target.panels || [];
    if (panels.length) {
      return panels.map((panel) => ({
        ra: panel.ra * 15.0, dec: panel.dec, index: panel.index,
        rotation: panel.rotation ?? target.rotation ?? 0,
      }));
    }
    return [{ ra: target.ra * 15.0, dec: target.dec, index: 0,
      rotation: target.rotation || 0 }];
  }

  function buildFraming(entry, data) {
    const wrap = document.createElement('div');
    wrap.className = 'plan-framing';
    const target = entry.target || {};

    const heading = document.createElement('h4');
    heading.textContent = 'Framing';
    wrap.appendChild(heading);

    // What it was framed with, if it was saved with one. A target from before
    // the field was recorded — or one typed in by coordinates — falls back to
    // what the camera covers now, and the caption says which it is showing,
    // because those two are only the same until you change a telescope.
    const saved = target.panelWidth > 0 && target.panelHeight > 0;
    const current = (data && data.field) || {};
    const field = saved
      ? { width: target.panelWidth, height: target.panelHeight }
      : { width: current.width || 0, height: current.height || 0 };
    const panels = framingPanels(target);
    // How much sky to fetch: the whole framing with a margin, so the field sits
    // in context rather than filling the frame edge to edge.
    const extent = target.extent || {};
    // The composite's extent is measured along the camera's axes; on the sky
    // it is turned by the target's angle, and what the picture has to hold is
    // its bounding box. At 268 degrees a 10 by 16 composite stands 16 tall.
    const turn = (Number(target.rotation) || 0) * DEG;
    const c = Math.abs(Math.cos(turn)), s = Math.abs(Math.sin(turn));
    const ew = Math.max(extent.width || 0, field.width || 0, 0.2);
    const ns = Math.max(extent.height || 0, field.height || 0, 0.2);
    const across = ew * c + ns * s;
    const down = ew * s + ns * c;
    // `fov` is the picture's *width*; the frame is 3:2, so a tall composite
    // needs a width one and a half times its height or it runs off the top and
    // bottom — which on a wide mosaic it did. The cap used to be 20 degrees,
    // which a 16 degree mosaic filled edge to edge with nothing to show it
    // against.
    const fov = Math.min(60, Math.max(0.1,
      Math.max(across, down * FRAME_W / FRAME_H) * 1.5));

    const stage = document.createElement('div');
    stage.className = 'framing-stage';
    const canvas = document.createElement('canvas');
    canvas.className = 'framing-canvas';
    stage.appendChild(canvas);
    const status = document.createElement('div');
    status.className = 'framing-status small muted';
    status.textContent = 'Fetching the sky…';
    stage.appendChild(status);
    wrap.appendChild(stage);

    const caption = document.createElement('p');
    caption.className = 'small muted';
    const bits = [];
    if (field.width) {
      bits.push(`${(field.width * 60).toFixed(1)}′ × ${(field.height * 60).toFixed(1)}′`
        + (saved ? ' as framed' : ' from the camera now'));
    }
    bits.push(`camera at ${Number(target.rotation || 0).toFixed(1)}°`
      + (target.align === 'fixed' && panels.length > 1 ? ' (no rotator: panels laid at the camera’s own angle)' : ''));
    if (panels.length > 1) {
      bits.push(`${target.rows}×${target.columns} mosaic, ${panels.length} panels`);
      bits.push(`${Math.round((target.overlap || 0) * 100)}% overlap`);
    }
    if (!field.width) {
      bits.push('no field size — set the focal length and sensor in '
        + 'Site & Optics to draw the frame');
    }
    caption.textContent = bits.join('  ·  ');
    wrap.appendChild(caption);

    // What the colours mean, as swatches rather than a sentence. Eight
    // overlapping rectangles in three colours are not something a caption
    // explains at a glance.
    if (panels.length > 1 || entry.collab) {
      const legend = document.createElement('div');
      legend.className = 'framing-legend small';
      const items = [];
      if (panels.length > 1) {
        items.push(['sw-green', entry.collab ? 'yours tonight' : 'shooting tonight']);
        items.push(['sw-blue', entry.collab ? 'the rest of the mosaic' : 'other panels']);
      }
      items.push(['sw-ring', 'being shot right now']);
      if (entry.collab && entry.collab.region) {
        const r = entry.collab.region;
        items.push(['sw-dash', `the collaboration’s region, ${Number(r.width).toFixed(1)}° × ${Number(r.height).toFixed(1)}° north-up`]);
      }
      for (const [cls, text] of items) {
        const item = document.createElement('span');
        item.className = 'legend-item';
        item.innerHTML = `<i class="sw ${cls}"></i>`;
        item.appendChild(document.createTextNode(text));
        legend.appendChild(item);
      }
      wrap.appendChild(legend);
      if (entry.collab && entry.collab.region && panels.length > 1) {
        const why = document.createElement('p');
        why.className = 'small muted';
        why.textContent = 'The panels are laid along your camera and stepped by whole '
          + 'frames, so they always cover a little more than the dashed region '
          + 'and, on a camera that cannot turn, sit at its angle rather than '
          + 'north-up. That is expected: the region is what the collaboration '
          + 'wants covered; the panels are how this telescope covers it.';
        wrap.appendChild(why);
      }
    }

    // Picking panels to re-shoot: click them on the picture.
    //
    // The mosaic is already drawn here with every panel numbered, so the
    // obvious thing to click is the panel itself. One panel came out under
    // cloud or with a satellite through it — click it, set the exposure and
    // count in the grid the way you would for any target, and the run shoots
    // only that. Nothing else about the target changes, so nothing has to be
    // put back afterwards except the selection.
    // A collaboration chunk is drawn from its *share*, and the share is not
    // clickable. The server dealt those panels and moves them as others join;
    // a stray click that dropped two of them would look exactly like the deal
    // having changed, and there would be no telling the two apart. The red
    // fill is the same, the numbers are the same — only the hand is kept off.
    const dealt = !!entry.collab;
    const chosen = new Set(dealt
      ? ((entry.collab || {}).share || [])
      : ((entry.options || {}).panels || []));
    let bar = null;
    if (panels.length > 1 && !dealt) {
      bar = document.createElement('div');
      bar.className = 'panel-pick small';
      wrap.appendChild(bar);
    }

    const saveChoice = async () => {
      // The collapsed heading says which panels are picked, and it is the only
      // part of this visible once the box is shut — so it is updated here
      // rather than waiting for the next full render, which may be a while and
      // would leave the box claiming twelve panels while it means two.
      const head = wrap.closest('.plan-box');
      const kind = head && head.querySelector('.kind');
      if (kind && panels.length > 1) {
        const list = [...chosen].sort((a, b) => a - b);
        kind.textContent = `${target.rows}×${target.columns} mosaic · `
          + (list.length
            ? `panel${list.length === 1 ? '' : 's'} ${list.join(', ')} only`
            : `${panels.length} panels`);
        kind.classList.toggle('picked-panels', list.length > 0);
      }
      try {
        await app.api(`/api/plan/entries/${entry.id}/options`, 'POST',
          { panels: [...chosen] });
      } catch (error) { app.toast(error.message, 'error'); }
      refreshTotals();
    };

    const drawBar = () => {
      if (!bar) return;
      bar.innerHTML = '';
      const label = document.createElement('span');
      label.textContent = chosen.size
        ? `Shooting panel${chosen.size === 1 ? '' : 's'} `
          + [...chosen].sort((a, b) => a - b).join(', ')
        : 'Click a panel to shoot only that one';
      label.className = chosen.size ? 'picked' : 'muted';
      bar.appendChild(label);
      if (chosen.size) {
        const clear = document.createElement('button');
        clear.className = 'btn small ghost';
        clear.textContent = 'All panels';
        clear.addEventListener('click', () => {
          chosen.clear();
          drawBar();
          if (typeof wrap.show === 'function') wrap.show(true);
          saveChoice();
        });
        bar.appendChild(clear);
      }
    };
    drawBar();

    // Nothing is fetched or drawn until the box is actually open.
    //
    // Two reasons, and the second is the one that bites. A plan of eight
    // targets should not pull eight survey cutouts off CDS the moment it
    // renders — and a canvas inside a hidden element has no width to measure,
    // so anything drawn then is drawn at a guessed size and stretched to the
    // real one when it appears, which turns a 3:2 sensor into whatever shape
    // the guess happened to be.
    // Tonight's panels: the ones the forecast says the run will reach before
    // the window closes, in the master's order. On a collaboration this is
    // the part of the dealt list that fits; on an ordinary mosaic, the part
    // of the whole (or of the picked panels) that fits.
    const forecast = (entry.tonight || {})[data && data.masterRig]
      || Object.values(entry.tonight || {})[0] || {};
    const tonight = new Set(forecast.panels || []);
    wrap.show = makeFraming(canvas, status, target, panels, field, fov, {
      chosen,
      tonight,
      // The panel the telescope is on right now, when the run is on this
      // entry. Read at paint time, so a repaint on a status tick sees the
      // current one.
      active: () => {
        const sequence = (app.state.status && app.state.status.sequence) || {};
        if (!sequence.running || sequence.entryId !== entry.id) return null;
        return sequence.panelIndex || 0;
      },
      // What the whole collaboration covers, drawn under the panels.
      outline: dealt ? ((entry.collab || {}).region || null) : null,
      // No hand on a dealt share: the fill and the numbers, and nothing to
      // click. See `dealt` above.
      onPick: dealt ? null : (index) => {
        if (chosen.has(index)) chosen.delete(index); else chosen.add(index);
        drawBar();
        wrap.show(true);
        saveChoice();
      },
    });
    return wrap;
  }

  /** Draw (or redraw) any framing panel that is on screen. */
  function showFramings(root) {
    (root || document).querySelectorAll('.plan-framing').forEach((wrap) => {
      if (typeof wrap.show === 'function') wrap.show();
    });
  }

  /* The cutout is asked for at this shape, and the canvas is made to match it
     exactly — so the picture fills the frame with no bands, and one scale in
     pixels per degree serves both axes. Get this wrong and the field rectangle
     is drawn at the wrong shape, which is worse than useless: the whole point
     of it is to show what the sensor covers. */
  const FRAME_W = 900;
  const FRAME_H = 600;

  function makeFraming(canvas, status, target, panels, field, fov, picking) {
    const ratio = window.devicePixelRatio || 1;
    let image = null;
    let fetched = false;
    let lastWidth = 0;
    // The panel lit as "shooting now" at the last paint, so a status tick
    // that moves the run on to the next panel repaints, and one that does
    // not costs nothing.
    let lastActive = null;
    // Where each panel ended up on screen, recorded as it is drawn so a click
    // can be turned back into a panel without repeating the projection.
    let hits = [];

    const paint = (force) => {
      // Measured every time, never guessed: this canvas is inside a panel that
      // starts hidden, and a hidden element measures zero.
      const width = Math.round(canvas.clientWidth);
      if (width <= 0) return false;
      const active = (picking && typeof picking.active === 'function')
        ? picking.active() : null;
      if (!force && width === lastWidth && active === lastActive) return true;
      lastActive = active;
      const height = Math.round(width * FRAME_H / FRAME_W);
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      canvas.style.height = `${height}px`;
      const ctx = canvas.getContext('2d');
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.fillStyle = '#05070d';
      ctx.fillRect(0, 0, width, height);

      const centre = { ra: target.ra * 15.0, dec: target.dec };
      // `fov` is the width of the cutout on the sky and its pixels are square,
      // so this is the scale for both axes.
      const scale = width / fov;
      if (image) ctx.drawImage(image, 0, 0, width, height);

      // North up, east left — what the cutout is, and what the overlay assumes.
      const project = (ra, dec) => {
        const offset = skyToOffset(centre.ra, centre.dec, ra, dec);
        if (!offset) return null;
        return [width / 2 - offset[0] * scale, height / 2 - offset[1] * scale];
      };

      hits = [];
      if (field.width) {
        const chosen = (picking && picking.chosen) || new Set();
        const tonight = (picking && picking.tonight) || new Set();
        // Two looks, and a ring. Green: the run will be on this panel tonight.
        // Blue: any other panel of the mosaic. A panel is either reachable
        // tonight or it is not; there is no third state worth a colour. The
        // one the telescope is on right now gets a bright ring over whichever
        // of those it is, drawn last so nothing overlaps it.
        //
        // On an ordinary mosaic with panels picked for a re-shoot, the picked
        // ones are what tonight means; on a collaboration the server's list
        // is, trimmed to what the window reaches.
        const look = (panel) => {
          const wanted = picking && picking.onPick ? chosen : tonight;
          if (wanted.has(panel.index)) {
            return { fill: 'rgba(75, 184, 122, 0.30)', line: '#4bb87a',
              bar: 'rgba(75, 184, 122, 0.75)', text: '#e3ffe9', bold: true };
          }
          return { fill: null,
            line: panels.length > 1 ? 'rgba(91, 141, 217, 0.95)' : '#5b8dd9',
            bar: 'rgba(91, 141, 217, 0.5)', text: 'rgba(219, 232, 255, 0.85)',
            bold: false };
        };
        let lit = null;
        for (const panel of panels) {
          const at = project(panel.ra, panel.dec);
          if (!at) continue;
          // The angle to draw at is not the angle to shoot at: north turns
          // across the picture, and a camera at a fixed sky angle turns with
          // it. Same correction the planner makes.
          const drawAngle = panel.rotation
            + northAngle(centre.ra, centre.dec, panel.ra, panel.dec);
          const w = field.width * scale;
          const h = field.height * scale;
          hits.push({ index: panel.index, x: at[0], y: at[1], w, h,
            angle: drawAngle });
          if (active !== null && panel.index === active) {
            lit = { at, w, h, angle: drawAngle };
          }
          // A panel with a claim on it is filled as well as outlined: with
          // eight overlapping rectangles a change of line colour alone is not
          // something you can see at a glance, which is the only way this is
          // worth looking at.
          const style = look(panel);
          ctx.save();
          ctx.translate(at[0], at[1]);
          ctx.rotate(-drawAngle * DEG);
          if (style.fill) {
            ctx.fillStyle = style.fill;
            ctx.fillRect(-w / 2, -h / 2, w, h);
          }
          ctx.strokeStyle = style.line;
          ctx.lineWidth = style.fill ? 2.2 : 1.4;
          ctx.strokeRect(-w / 2, -h / 2, w, h);
          // A bar along the top edge says which way is up in the frame.
          ctx.fillStyle = style.bar;
          ctx.fillRect(-w / 2, -h / 2, w, Math.min(5, h * 0.06));
          if (panels.length > 1 && w > 26) {
            ctx.fillStyle = style.text;
            ctx.font = `${style.bold ? 'bold ' : ''}10px ui-monospace, monospace`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(String(panel.index), 0, 0);
          }
          ctx.restore();
        }
        // The panel being shot right now: a thick white ring with a glow,
        // over everything, and the word so it needs no key. Drawn after the
        // loop so a neighbour drawn later cannot cover it.
        if (lit) {
          ctx.save();
          ctx.translate(lit.at[0], lit.at[1]);
          ctx.rotate(-lit.angle * DEG);
          ctx.shadowColor = 'rgba(255, 244, 180, 0.95)';
          ctx.shadowBlur = 14;
          ctx.strokeStyle = '#fff4b4';
          ctx.lineWidth = 3.5;
          ctx.strokeRect(-lit.w / 2, -lit.h / 2, lit.w, lit.h);
          ctx.shadowBlur = 0;
          ctx.fillStyle = 'rgba(255, 244, 180, 0.18)';
          ctx.fillRect(-lit.w / 2, -lit.h / 2, lit.w, lit.h);
          if (lit.w > 44) {
            ctx.fillStyle = '#fff4b4';
            ctx.font = 'bold 9px ui-monospace, monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.shadowColor = 'rgba(0, 0, 0, 0.95)';
            ctx.shadowBlur = 4;
            ctx.fillText('SHOOTING', 0, Math.min(lit.h / 2 - 9, 14));
          }
          ctx.restore();
        }
      }

      // The collaboration's own rectangle, when this is a chunk of one: what
      // the whole project covers, drawn under the panels so the two shapes can
      // be told apart. North-up, turned only by the convergence at its centre,
      // the way the Planner drew it — the mosaic is laid along the camera's
      // axes and circumscribes it, so on a turned camera the two differ a lot.
      const outline = picking && picking.outline;
      if (outline && outline.width > 0 && outline.height > 0) {
        const at = project(outline.ra, outline.dec);
        if (at) {
          const angle = -northAngle(centre.ra, centre.dec, outline.ra, outline.dec) * DEG;
          const hw = outline.width / 2 * scale;
          const hh = outline.height / 2 * scale;
          const cos = Math.cos(angle), sin = Math.sin(angle);
          const corner = (dx, dy) => [at[0] + dx * cos - dy * sin, at[1] + dx * sin + dy * cos];
          const box = [corner(-hw, -hh), corner(hw, -hh), corner(hw, hh), corner(-hw, hh)];
          ctx.save();
          ctx.beginPath();
          box.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
          ctx.closePath();
          ctx.strokeStyle = 'rgba(0, 0, 0, 0.8)';
          ctx.lineWidth = 4;
          ctx.stroke();
          ctx.strokeStyle = 'rgba(126, 231, 165, 0.95)';
          ctx.lineWidth = 2;
          ctx.setLineDash([7, 5]);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.font = 'bold 11px ui-monospace, monospace';
          ctx.fillStyle = 'rgba(190, 245, 210, 0.95)';
          ctx.textAlign = 'left';
          ctx.shadowColor = 'rgba(0, 0, 0, 0.95)';
          ctx.shadowBlur = 4;
          ctx.fillText('collaboration', box[0][0] + 6, box[0][1] + 14);
          ctx.restore();
        }
      }

      // A compass, because north-up-east-left is a convention worth stating.
      ctx.strokeStyle = 'rgba(219, 227, 238, 0.5)';
      ctx.fillStyle = 'rgba(219, 227, 238, 0.7)';
      ctx.lineWidth = 1;
      ctx.font = '9px ui-monospace, monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      const cx = width - 22;
      const cy = height - 22;
      ctx.beginPath();
      ctx.moveTo(cx, cy + 10); ctx.lineTo(cx, cy - 10);
      ctx.moveTo(cx - 10, cy); ctx.lineTo(cx + 10, cy);
      ctx.stroke();
      ctx.fillText('N', cx, cy - 15);
      ctx.fillText('E', cx - 15, cy);
      lastWidth = width;
      return true;
    };

    const fetchImage = async () => {
      fetched = true;
      try {
        const url = `/api/survey/image?ra=${target.ra.toFixed(6)}`
          + `&dec=${target.dec.toFixed(6)}&fov=${fov.toFixed(4)}`
          + `&width=${FRAME_W}&height=${FRAME_H}`;
        const response = await fetch(url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const blob = await response.blob();
        image = await new Promise((resolve, reject) => {
          const element = new Image();
          element.onload = () => resolve(element);
          element.onerror = () => reject(new Error('the image could not be decoded'));
          element.src = URL.createObjectURL(blob);
        });
        status.hidden = true;
      } catch (error) {
        // The overlay is the useful half and does not need the picture, so it
        // is still drawn — over an empty sky, with the reason underneath.
        status.textContent = 'No survey image (offline?) — the framing is still '
          + 'drawn below.';
      }
      paint(true);
    };

    // A picker with panels to highlight but no handler draws the selection and
    // takes no clicks: a collaboration's share is shown, not edited.
    if (picking && picking.onPick && panels.length > 1) {
      canvas.classList.add('pickable');
      canvas.title = 'Click a panel to shoot only that one';
      canvas.addEventListener('click', (event) => {
        const rect = canvas.getBoundingClientRect();
        const px = event.clientX - rect.left;
        const py = event.clientY - rect.top;
        // Panels overlap, so the smallest containing rectangle wins — which on
        // a regular mosaic means the one whose centre is nearest, and is what
        // you get by taking the last match in draw order rather than the first.
        let best = null;
        for (const hit of hits) {
          // Into the panel's own frame, where the test is a plain rectangle.
          const angle = -hit.angle * DEG;
          const dx = px - hit.x;
          const dy = py - hit.y;
          const lx = dx * Math.cos(-angle) - dy * Math.sin(-angle);
          const ly = dx * Math.sin(-angle) + dy * Math.cos(-angle);
          if (Math.abs(lx) <= hit.w / 2 && Math.abs(ly) <= hit.h / 2) {
            const distance = dx * dx + dy * dy;
            if (!best || distance < best.distance) best = { hit, distance };
          }
        }
        if (best) picking.onPick(best.hit.index);
      });
    }

    // Called on every status tick as well as on open. `paint` returns at once
    // unless the width has actually changed, so this costs two integer compares
    // in the common case — and in exchange the picture can never be left drawn
    // at a size the canvas no longer is, which is the one way this goes wrong
    // and shows a sensor the wrong shape.
    return (force) => {
      paint(!!force);
      if (!fetched) fetchImage();
    };
  }

  /* A night at a time.
   *
   * A running total cannot answer the question this does. Four hours made of
   * forty good subs and four hours made of eighty subs half of which were
   * trailed are the same number and are not the same night — so each night
   * carries what it was shot at as well as how much of it there was. */
  function buildNightReport(entry) {
    const wrap = document.createElement('div');
    wrap.className = 'plan-report';
    const totals = (entry.target && entry.target.integration) || {};
    const log = [...(totals.log || [])].reverse();   // newest first

    const heading = document.createElement('h4');
    heading.textContent = 'Night by night';
    wrap.appendChild(heading);

    if (!log.length) {
      const empty = document.createElement('p');
      empty.className = 'small muted';
      empty.textContent = totals.seconds
        ? 'This target was shot before the night log existed, so only the total '
          + 'above is known for it. Nights from here on are recorded in full.'
        : 'Nothing shot on this target yet. Each night it runs will appear here '
          + 'with what it was shot at.';
      wrap.appendChild(empty);
      return wrap;
    }

    const table = document.createElement('table');
    table.className = 'night-table';
    table.innerHTML = '<thead><tr>'
      + '<th>Night</th><th class="num">Frames</th><th class="num">Integration</th>'
      + '<th>Filters</th><th class="num">HFR</th><th class="num">Guiding</th>'
      + '<th>Trouble</th></tr></thead>';
    const body = document.createElement('tbody');

    for (const night of log) {
      const row = document.createElement('tr');
      const hfr = night.hfrCount ? night.hfrSum / night.hfrCount : null;
      const rms = night.guideCount ? night.guideSum / night.guideCount : null;
      const filters = Object.entries(night.byFilter || {})
        .sort((a, b) => b[1].seconds - a[1].seconds)
        .map(([name, value]) => `${name} ${duration(value.seconds)}`)
        .join(', ');

      const trouble = [];
      if (night.guideLost) trouble.push(`${night.guideLost} unguided`);
      if (night.recoveries) {
        trouble.push(`${night.recoveries} recover${night.recoveries === 1 ? 'y' : 'ies'}`);
      }

      row.innerHTML = '<td class="mono night"></td><td class="num mono"></td>'
        + '<td class="num mono"></td><td class="filters"></td>'
        + '<td class="num mono"></td><td class="num mono"></td>'
        + '<td class="trouble"></td>';
      const cells = row.children;
      cells[0].textContent = night.night;
      cells[0].title = night.first && night.last
        ? `${clock(night.first)} – ${clock(night.last)}` : '';
      cells[1].textContent = night.frames;
      cells[2].textContent = duration(night.seconds);
      cells[3].textContent = filters || '—';
      cells[4].textContent = hfr === null ? '—' : hfr.toFixed(2);
      cells[4].title = hfr === null ? ''
        : `${night.hfrMin.toFixed(2)} – ${night.hfrMax.toFixed(2)} over `
          + `${night.hfrCount} measured frame${night.hfrCount === 1 ? '' : 's'}`;
      cells[5].textContent = rms === null ? '—' : `${rms.toFixed(2)}"`;
      cells[6].textContent = trouble.join(', ') || '—';
      cells[6].classList.toggle('warn', trouble.length > 0);
      body.appendChild(row);
    }
    table.appendChild(body);
    // Its own scroll box, so a season's worth of nights does not push the rest
    // of the plan off the page. The headings are pinned inside it.
    const scroll = document.createElement('div');
    scroll.className = 'night-scroll';
    scroll.appendChild(table);
    wrap.appendChild(scroll);

    // The comparison worth having: how this target's nights sit against each
    // other. A night whose stars are half a pixel fatter than the rest is the
    // one to go and look at.
    const measured = log.filter((n) => n.hfrCount);
    if (measured.length > 1) {
      const best = measured.reduce((a, b) =>
        (a.hfrSum / a.hfrCount) <= (b.hfrSum / b.hfrCount) ? a : b);
      const note = document.createElement('p');
      note.className = 'small muted';
      note.textContent = `Best seeing was ${best.night} at HFR `
        + `${(best.hfrSum / best.hfrCount).toFixed(2)}, over `
        + `${log.length} night${log.length === 1 ? '' : 's'} and `
        + `${duration(totals.seconds || 0)} in total.`;
      wrap.appendChild(note);
    }
    return wrap;
  }

  /** What a collaboration imposes on this target, in a sentence.
   *
   *  The altitude floor and the Moon distance are the project's rules, set
   *  by whoever started it and written onto the entry on every sync. They are
   *  shown here so a night that skips the target is explained, and not
   *  offered for editing, because they are not this rig's to change.
   */
  function projectRules(entry) {
    const options = entry.options || {};
    const rules = [];
    const floor = Number(options.minAltitude || 0);
    const moon = Number(options.moonAvoidance || 0);
    if (floor > 0) rules.push(`only above ${floor.toFixed(0)}°`);
    if (moon > 0) rules.push(`only with the Moon ${moon.toFixed(0)}° or more away`);
    if (!rules.length) return 'The collaboration sets no altitude or Moon rule of its own; the observatory floor from Site & Optics applies.';
    return `The collaboration's rules: ${rules.join(', ')}. The observatory floor from Site & Optics applies as well.`;
  }

  function buildOptions(entry, data) {
    const wrap = document.createElement('div');
    wrap.className = 'plan-options';
    const options = entry.options || {};

    const heading = document.createElement('h4');
    heading.textContent = 'How this target runs';
    wrap.appendChild(heading);

    // Phrased as the thing being asked for rather than as its opposite: the
    // question in front of you is "should tonight leave this one out?", and
    // ticking a box to say no is a sentence you have to read twice.
    const skip = checkbox('Skip this target', options.enabled === false,
      'Ticked, it stays in the plan but is not shot — for weather, a tree, or a '
      + 'target you want back tomorrow.');
    skip.row.classList.add('opt-skip');
    wrap.appendChild(skip.row);

    const focus = checkbox('Autofocus when it starts', !!options.focusOnStart,
      'Sweeps whatever the clock and temperature triggers think. A long slew '
      + 'across the sky is exactly when focus has moved.');
    wrap.appendChild(focus.row);

    // A collaboration on a rig with a rotator: turn the camera to the
    // project's angle, or leave it where it sits. Changing it lays the mosaic
    // out again, so it saves on its own rather than with the button below.
    if (entry.collab && data && data.rotator) {
      const pa = Number(((entry.collab || {}).region || {}).rotation || 0);
      const turn = checkbox(`Turn the rotator to the collaboration's angle (${pa.toFixed(0)}°)`,
        options.collabMatchRotation !== false,
        'On: the camera is turned to the project’s angle, so a single target '
        + 'is framed the way it was framed and a mosaic’s panels lie along the '
        + 'project’s grid. Off: the camera stays where it is and the mosaic is '
        + 'laid at that angle instead.');
      turn.input.addEventListener('change', async () => {
        try {
          await app.api(`/api/plan/entries/${entry.id}/options`, 'POST',
            { collabMatchRotation: turn.input.checked });
          app.toast(turn.input.checked
            ? 'The mosaic is laid along the collaboration’s angle'
            : 'The mosaic is laid at the camera’s own angle', 'success');
          load();
        } catch (error) { app.toast(error.message, 'error'); }
      });
      wrap.appendChild(turn.row);
    }

    const orderRow = document.createElement('label');
    orderRow.className = 'opt';
    orderRow.innerHTML = '<span>Filter order</span>';
    const order = document.createElement('select');
    order.appendChild(new Option('All of one, then the next (fewest changes)', 'grouped'));
    order.appendChild(new Option('Rotate: L, R, G, B, L, R…', 'rotate'));
    order.value = options.filterOrder === 'rotate' ? 'rotate' : 'grouped';
    orderRow.appendChild(order);
    wrap.appendChild(orderRow);
    wrap.appendChild(note('Rotating costs a filter change per frame and buys the '
      + 'thing a filter change cannot: a session cut short by cloud at forty per '
      + 'cent is forty per cent of every channel, not all of the luminance and '
      + 'none of the red.'));

    if (entry.collab) wrap.appendChild(note(projectRules(entry)));

    const save = document.createElement('button');
    save.className = 'btn small primary';
    save.textContent = 'Save target settings';
    const row = document.createElement('div');
    row.className = 'row gap';
    row.appendChild(save);
    wrap.appendChild(row);

    save.addEventListener('click', async () => {
      const body = {
        enabled: !skip.input.checked,
        focusOnStart: focus.input.checked,
        filterOrder: order.value,
      };
      try {
        await app.api(`/api/plan/entries/${entry.id}/options`, 'POST', body);
      } catch (error) { app.toast(error.message, 'error'); return; }
      app.toast(`${entry.name}: settings saved`, 'success');
      load();
    });
    return wrap;
  }

  function checkbox(label, checked, hint) {
    const row = document.createElement('label');
    row.className = 'inline';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !!checked;
    row.appendChild(input);
    row.appendChild(document.createTextNode(` ${label}`));
    row.title = hint || '';
    return { row, input };
  }

  function note(text) {
    const node = document.createElement('p');
    node.className = 'small muted';
    node.textContent = text;
    return node;
  }

  /** What has already been shot on this target, across every night. */
  function buildIntegration(entry) {
    const row = document.createElement('div');
    row.className = 'plan-integration small';
    const totals = (entry.target && entry.target.integration) || {};
    const seconds = totals.seconds || 0;

    const hint = '<span class="expand-hint muted">double-click for the night '
      + 'report and this target\'s settings</span>';

    if (!seconds) {
      row.innerHTML = `<span class="muted">nothing shot on this target yet</span>${hint}`;
      return row;
    }
    const byFilter = Object.entries(totals.byFilter || {})
      .sort((a, b) => b[1] - a[1])
      .map(([name, value]) => `${name} ${duration(value)}`)
      .join(', ');
    const nights = (totals.nights || []).length;
    row.innerHTML = `<span class="done"></span><span class="muted meta"></span>${hint}`;
    row.querySelector('.done').textContent =
      `${duration(seconds)} done · ${totals.frames} frame${totals.frames === 1 ? '' : 's'}`;
    row.querySelector('.meta').textContent =
      (byFilter ? `  ${byFilter}` : '')
      + (nights ? `  over ${nights} night${nights === 1 ? '' : 's'}` : '');
    return row;
  }

  /** The line showing when this target is pinned to run, and how to unpin it. */
  function buildSlot(entry, hint = 'click the graph to pin one') {
    const row = document.createElement('div');
    row.className = 'plan-slot small';
    const has = entry.startAt || entry.endAt;
    const text = document.createElement('span');
    text.className = 'mono';
    text.textContent = has
      ? `runs ${entry.startAt ? clock(entry.startAt) : 'any time'}`
        + ` → ${entry.endAt ? clock(entry.endAt) : 'any time'}`
      : `no start or end set — ${hint}`;
    text.classList.toggle('muted', !has);
    row.appendChild(text);

    // Whose decision this slot was. A pinned one is left alone by Auto-arrange,
    // which works around it; one the arranger wrote is its to move next time.
    if (has) {
      const source = document.createElement('span');
      source.className = `slot-source${entry.timesPinned ? ' pinned' : ''}`;
      source.textContent = entry.timesPinned ? 'pinned' : 'auto-arranged';
      source.title = entry.timesPinned
        ? 'You set these times, so Auto-arrange works around them rather than '
          + 'over them. Clear them to hand this target back to the arranger.'
        : 'Auto-arrange chose these times and is free to move them next time. '
          + 'Click the graph to pin your own.';
      row.appendChild(source);
    }

    if (has) {
      const clear = document.createElement('button');
      clear.className = 'btn small ghost';
      clear.textContent = 'Clear';
      clear.addEventListener('click', async () => {
        try {
          await app.api(`/api/plan/entries/${entry.id}/times`, 'POST',
            { clearStart: true, clearEnd: true });
        } catch (error) { app.toast(error.message, 'error'); return; }
        load();
      });
      row.appendChild(clear);
    }
    return row;
  }

  function buildFilters(entry, data, unusable) {
    // A collaboration chunk is not yours to expose. Signing up for one is
    // agreeing to shoot it the project's way — frames at somebody else's sub
    // length, in somebody else's proportions — because a stack assembled from
    // contributors who each chose their own is not a stack. What is yours is
    // how long the telescope is theirs for, and that gets its own control.
    if (entry.collab) return buildCollabFilters(entry, data, unusable);

    const wrap = document.createElement('div');
    wrap.className = 'plan-filters';

    const telescopes = data.telescopes || [];
    // Which telescope's allocation is being edited. Remembered per entry so
    // switching to the slave on one target does not switch it on all of them.
    const chosen = sched.rigTab.get(entry.id)
      || data.masterRig || (telescopes[0] || {}).id;

    if (telescopes.length > 1) {
      const tabs = document.createElement('div');
      tabs.className = 'rig-tabs';
      for (const rig of telescopes) {
        const seconds = (entry.rigSeconds || {})[rig.id] || 0;
        const button = document.createElement('button');
        button.className = `btn small rig-tab${rig.id === chosen ? ' active' : ''}`;
        if (rig.role === 'master') button.classList.add('master');
        button.textContent = `${rig.name}  ${seconds ? duration(seconds) : '—'}`;
        const mirrors = rig.id !== data.masterRig
          && !((entry.rigFilters || {})[rig.id]);
        button.title = mirrors
          ? 'Following the master. Change anything here to give this telescope '
            + 'its own filters and exposures.'
          : 'Its own allocation.';
        if (mirrors) button.classList.add('mirrored');
        button.addEventListener('click', () => {
          sched.rigTab.set(entry.id, rig.id);
          render();
        });
        tabs.appendChild(button);
      }
      wrap.appendChild(tabs);
    }

    const overheads = data.overheads;
    const allocation = (entry.allocations || {})[chosen] || entry.filters || [];
    const rigInfo = telescopes.find((r) => r.id === chosen) || {};
    const known = new Map(allocation.map((f) => [f.name, f]));
    // A telescope's own wheel decides which filters it can be asked for.
    const names = [...new Set([...(rigInfo.filters || data.filters || []),
      ...known.keys()])];

    const table = document.createElement('div');
    table.className = 'plan-filter-grid';
    table.innerHTML = '<span class="muted">Filter</span>'
      + '<span class="muted">Exposure</span>'
      + `<span class="muted">Frames${entry.panels > 1 ? ' / panel' : ''}</span>`
      + '<span class="muted">Total</span><span class="muted">Time</span>';

    const rows = [];
    for (const name of names) {
      const current = known.get(name) || { name, exposure: 300, count: 0 };
      const row = { name, exposure: current.exposure, count: current.count };
      rows.push(row);

      const label = document.createElement('span');
      label.className = 'plan-filter-name';
      label.textContent = name;

      const exposure = document.createElement('input');
      exposure.type = 'number';
      exposure.min = '1'; exposure.max = '3600'; exposure.step = '1';
      exposure.value = String(current.exposure);
      exposure.disabled = unusable;

      const count = document.createElement('input');
      count.type = 'number';
      count.min = '0'; count.step = '1';
      count.value = String(current.count);
      count.disabled = unusable;

      const total = document.createElement('span');
      total.className = 'mono muted';
      const time = document.createElement('span');
      time.className = 'mono muted';

      const refresh = () => {
        row.exposure = Math.max(1, Number(exposure.value) || 1);
        row.count = Math.max(0, Number(count.value) || 0);
        const others = rows.filter((r) => r !== row && r.count > 0);
        const ceiling = maxCount(row.exposure, entry.panels,
          entry.availableSeconds, others, overheads);
        count.max = String(ceiling);
        // Full and over are not the same thing, and colouring them the same
        // made a plan that used every minute of the night look like a fault.
        // At the ceiling is the *goal*; past it is a number that will be
        // trimmed when it saves, which is a note rather than an alarm.
        const full = row.count > 0 && row.count >= ceiling;
        count.classList.toggle('at-limit', full && row.count <= ceiling);
        count.classList.toggle('over', row.count > ceiling);
        count.title = row.count > ceiling
          ? `Only ${ceiling} fit tonight; this will be trimmed to that when it saves`
          : full ? 'This fills the time this target has tonight' : '';
        total.textContent = row.count ? String(row.count * entry.panels) : '—';
        time.textContent = row.count
          ? duration(row.count * entry.panels * frameSeconds(row.exposure, overheads))
          : '—';
        updateBar();
      };

      exposure.addEventListener('input', () => { refresh(); queueSave(entry.id, rows, chosen); });
      count.addEventListener('input', () => { refresh(); queueSave(entry.id, rows, chosen); });
      // Leaving the field is when a value the server trimmed is safe to show.
      count.addEventListener('blur', () => {
        if (Number(count.value) !== row.count) count.value = String(row.count);
        refresh();
      });

      table.append(label, exposure, count, total, time);
      row.refresh = refresh;
      row.countInput = count;
      row.exposureInput = exposure;
    }
    wrap.appendChild(table);

    const bar = document.createElement('div');
    bar.className = 'plan-bar';
    bar.innerHTML = '<div class="plan-bar-fill"></div>';
    const barText = document.createElement('div');
    barText.className = 'plan-bar-text small mono';
    wrap.append(bar, barText);

    function updateBar() {
      const used = planSeconds(rows.filter((r) => r.count > 0), entry.panels, overheads);
      const share = entry.availableSeconds
        ? Math.min(1, used / entry.availableSeconds) : 0;
      bar.querySelector('.plan-bar-fill').style.width = `${share * 100}%`;
      bar.classList.toggle('full', share > 0.995);
      // Each telescope is measured against the whole window, not a share of
      // it: they shoot at the same time, so the same stretch of night is what
      // limits every one of them.
      barText.textContent =
        `${duration(used)} of ${duration(entry.availableSeconds)} used`
        + `   ·   ${duration(Math.max(0, entry.availableSeconds - used))} left`
        + (telescopes.length > 1 ? `   ·   ${rigInfo.name || 'this telescope'}` : '');
    }

    rows.forEach((row) => row.refresh());
    return wrap;
  }

  /* ------------------------------------------------- a collaboration chunk */

  /** The chunk's own block: what the project set, what everyone has collected,
   *  and the one number that belongs to the operator — how long tonight.
   */
  function buildCollabFilters(entry, data, unusable) {
    const wrap = document.createElement('div');
    wrap.className = 'plan-filters plan-collab';
    const info = entry.collab || {};
    const overheads = data.overheads;
    const allocation = entry.filters || [];

    // -- which part of the sky is this telescope's -------------------------
    // The mosaic on the plan is the whole project, so the picture above shows
    // all of it; this says which panels are this rig's to shoot. The framing
    // fills the ones tonight reaches in green and the rest of the list in
    // red, the heading names them, and the Tonight line says which fit.
    const share = info.share || [];
    const total = info.totalPanels || entry.panels || 1;
    if (total > 1) {
      const mine = document.createElement('div');
      mine.className = 'collab-share small';
      // What tonight actually reaches, from the same forecast the Tonight
      // line uses. The server sizes its list from the hours this rig reports,
      // so the two agree; when they briefly do not - the window shrank since
      // the last check-in - the difference is said, and the next check-in
      // hands the rest back.
      const forecast = (entry.tonight || {})[data && data.masterRig]
        || Object.values(entry.tonight || {})[0] || {};
      const reached = new Set(forecast.panels || []);
      const listed = share.length ? [...share].sort((a, b) => a - b) : [];
      const unreached = listed.filter((index) => !reached.has(index));
      if (share.length && share.length < total) {
        mine.innerHTML = `<b>Tonight’s panels</b> `
          + `<span class="mono">${listed.filter((i) => reached.has(i)).join(', ') || '—'}</span>`
          + ` <span class="muted">— of the ${total} in the mosaic, dealt by the `
          + 'server for this night from what everybody has collected.</span>'
          + (unreached.length
            ? `<div class="muted">The server dealt ${share.length}; tonight’s window `
              + `reaches ${share.length - unreached.length}. The rest are handed back `
              + 'on the next check-in, so nobody waits on them.</div>'
            : '');
      } else {
        mine.innerHTML = `<b>Your share</b> all ${total} panels`
          + ' <span class="muted">— nobody else is on this yet.</span>';
      }
      wrap.appendChild(mine);
    }

    // -- what the project says to shoot -----------------------------------
    // Per panel: what the project wants at every point of its sky, over
    // however many nights it takes, and beside it tonight's visit — the slice
    // of that depth the server has sized for this rig tonight, enough frames
    // on each panel to stack and as many panels as the night holds. The
    // Tonight line above says which panels those frames land on.
    const spec = document.createElement('div');
    spec.className = 'collab-spec';
    spec.innerHTML = '<div class="collab-spec-head small muted">'
      + 'Set by the project — not yours to change</div>';
    const table = document.createElement('div');
    table.className = 'plan-filter-grid collab-grid';
    const visit = (info.visit && info.visit.frames) || {};
    const nightly = Object.keys(visit).length > 0;
    table.innerHTML = '<span class="muted">Filter</span>'
      + '<span class="muted">Exposure</span>'
      + `<span class="muted">${nightly ? 'Tonight / panel' : 'Frames / panel'}</span>`
      + `<span class="muted">${nightly ? 'Full depth / panel' : (entry.panels > 1 ? 'Whole share' : 'Total')}</span>`
      + `<span class="muted">${nightly ? 'Over all nights' : (entry.panels > 1 ? 'Over all nights' : 'Time')}</span>`;

    const byName = new Map(allocation.map((row) => [row.name, row]));
    const shareCount = (share.length && share.length < total) ? share.length : entry.panels;
    for (const row of (info.filters || [])) {
      const planned = byName.get(row.filter) || { count: 0 };
      const count = planned.count || 0;
      const full = row.exposure > 0 ? Math.ceil((row.hours || 0) * 3600 / row.exposure) : 0;
      const cell = (text, className = '') => {
        const node = document.createElement('span');
        node.className = className;
        node.textContent = text;
        return node;
      };
      if (nightly) {
        table.append(
          cell(row.filter, 'plan-filter-name'),
          cell(`${Number(row.exposure).toFixed(0)}s`, 'mono'),
          cell(count ? String(count) : '—', 'mono'),
          cell(full ? String(full) : '—', 'mono muted'),
          cell(full
            ? duration(full * shareCount * frameSeconds(row.exposure, overheads))
            : '—', 'mono muted'));
      } else {
        table.append(
          cell(row.filter, 'plan-filter-name'),
          cell(`${Number(row.exposure).toFixed(0)}s`, 'mono'),
          cell(count ? String(count) : '—', 'mono'),
          cell(count ? String(count * entry.panels) : '—', 'mono muted'),
          cell(count
            ? duration(count * entry.panels * frameSeconds(row.exposure, overheads))
            : '—', 'mono muted'));
      }
    }
    spec.appendChild(table);
    if (nightly) {
      const note = document.createElement('div');
      note.className = 'small muted';
      note.textContent = 'Tonight’s frames per panel are chosen by the server each night '
        + 'from what everybody has collected so far: enough on each panel for your own '
        + 'stack, spread over as many panels as your hours hold, on the panels you have '
        + 'been to least.';
      spec.appendChild(note);
    }
    wrap.appendChild(spec);

    // -- the one number that is yours -------------------------------------
    const mine = document.createElement('div');
    mine.className = 'row gap collab-hours';
    const label = document.createElement('span');
    label.className = 'small';
    label.textContent = 'Give it';
    const hours = document.createElement('input');
    hours.type = 'number';
    hours.min = '0'; hours.max = '24'; hours.step = '0.25';
    hours.disabled = unusable;
    // Tonight's, not the share's. This box used to open on the cost of the
    // whole share at full depth — eight hundred hours on a big mosaic — under
    // a label that said "tonight". What it shows is the time pinned on the
    // entry if there is one, and otherwise the window the target is up for.
    const pinned = entry.startAt && entry.endAt ? (entry.endAt - entry.startAt) / 3600 : 0;
    const tonightHours = pinned > 0 ? pinned : (entry.availableSeconds || 0) / 3600;
    hours.value = tonightHours.toFixed(2).replace(/\.?0+$/, '') || '0';
    const unit = document.createElement('span');
    unit.className = 'small muted';
    unit.textContent = 'hours tonight';

    const fit = document.createElement('button');
    fit.className = 'btn small';
    fit.textContent = 'All the time it has';
    fit.title = 'As much as the window allows';
    fit.disabled = unusable;
    fit.addEventListener('click', () => {
      // Zero means "no limit": the whole of the window the target is up for.
      // This used to pin a duration read off `availableSeconds` — which is the
      // window *after* clipping by the times already pinned — so every press
      // shaved a few minutes off and the end crept away from dawn.
      hours.value = '0';
      save();
    });

    let pending = null;
    function save() {
      clearTimeout(pending);
      pending = setTimeout(async () => {
        $('schedSaved').textContent = 'saving…';
        try {
          const answer = await app.api(
            `/api/collab/entries/${entry.id}/hours`, 'POST',
            { hours: Math.max(0, Number(hours.value) || 0) });
          $('schedSaved').textContent = answer.clamped
            ? 'saved — trimmed to what fits' : 'saved';
          load();
        } catch (error) {
          $('schedSaved').textContent = error.message;
        }
      }, 450);
    }
    hours.addEventListener('input', save);
    mine.append(label, hours, unit, fit);
    wrap.appendChild(mine);

    const note = document.createElement('p');
    note.className = 'small muted';
    note.textContent = 'The server decides the filters, the sub lengths and how '
      + 'deep each panel goes; you decide how long the telescope is theirs '
      + 'tonight. It walks your panels in order at full depth and stops when '
      + 'the time runs out — Tonight, above, says which panels that is.';
    wrap.appendChild(note);

    // -- what everybody together has collected ----------------------------
    wrap.appendChild(buildDepth(info));
    return wrap;
  }

  /** Depth per filter, across every contributor.
   *
   *  The question a participant actually has is "is this nearly done, and does
   *  it still need me?" — and no amount of detail about their own frames
   *  answers it. This is the shared ledger: what the project wants at every
   *  point of its sky, and what everyone together has put there.
   */
  function buildDepth(info) {
    const box = document.createElement('div');
    box.className = 'collab-depth';
    const goals = info.goals || {};
    const got = info.collected || {};
    const names = [...new Set([...Object.keys(goals), ...Object.keys(got)])].sort();

    const heading = document.createElement('h4');
    heading.textContent = 'Collected by everyone';
    box.appendChild(heading);

    if (!info.known) {
      // Never imply a number is current when it could not be fetched.
      box.innerHTML += '<p class="small muted">The server has not been reached '
        + 'since this was opened, so there is nothing current to show.</p>';
      return box;
    }
    if (!names.length) {
      box.innerHTML += '<p class="small muted">Nothing contributed yet.</p>';
      return box;
    }

    const table = document.createElement('div');
    table.className = 'depth-grid';
    for (const name of names) {
      const want = Number(goals[name] || 0);
      const have = Number(got[name] || 0);
      const share = want > 0 ? Math.min(1, have / want) : (have > 0 ? 1 : 0);

      const label = document.createElement('span');
      label.className = 'plan-filter-name';
      label.textContent = name;

      const meter = document.createElement('div');
      meter.className = `meter${share >= 1 ? ' met' : ''}`;
      meter.innerHTML = '<div class="meter-fill"></div>';
      meter.querySelector('.meter-fill').style.width = `${share * 100}%`;

      const value = document.createElement('span');
      value.className = 'mono small';
      value.textContent = want > 0
        ? `${have.toFixed(1)} / ${want.toFixed(0)}h`
        : `${have.toFixed(1)}h`;

      table.append(label, meter, value);
    }
    box.appendChild(table);
    return box;
  }

  /* ------------------------------------------------------------- saving */

  function queueSave(entryId, rows, rigId) {
    const key = `${entryId}:${rigId || ''}`;
    clearTimeout(sched.dirty.get(key));
    sched.dirty.set(key, setTimeout(async () => {
      sched.dirty.delete(key);
      const filters = rows.filter((r) => r.count > 0)
        .map((r) => ({ name: r.name, exposure: r.exposure, count: r.count }));
      $('schedSaved').textContent = 'saving…';
      try {
        const path = `/api/plan/entries/${entryId}/filters`
          + (rigId && rigId !== (sched.data && sched.data.masterRig)
            ? `?rig=${encodeURIComponent(rigId)}` : '');
        const result = await app.api(path, 'POST', { filters });
        if (result.clamped && result.clamped.length) {
          for (const item of result.clamped) {
            app.toast(`${item.name}: only ${item.given} frames per panel fit tonight `
              + `(asked for ${item.asked})`, 'info', 5000);
            // Corrected in place rather than by rebuilding the plan. A rebuild
            // replaces the very box being typed in, and asking for one frame
            // too many is the normal way to find the ceiling — so it happened
            // on nearly every press of the up arrow.
            const row = rows.find((r) => r.name === item.name);
            if (!row) continue;
            row.count = item.given;
            // Except while the cursor is in it: the operator is still deciding,
            // and the field already shows that it is over with a note saying it
            // will be trimmed. Corrected when they leave it.
            if (row.countInput && document.activeElement !== row.countInput) {
              row.countInput.value = String(item.given);
            }
            if (row.refresh) row.refresh();
          }
          $('schedSaved').textContent = 'saved';
          await refreshTotals();
        } else {
          $('schedSaved').textContent = 'saved';
          await refreshTotals();
        }
      } catch (error) {
        app.toast(error.message, 'error');
        $('schedSaved').textContent = 'not saved';
      }
    }, 500));
  }

  /** Update the footer without rebuilding the boxes and losing focus. */
  async function refreshTotals() {
    try {
      const data = await app.api('/api/plan');
      sched.data = { ...data, };
      let seconds = 0;
      let frames = 0;
      for (const entry of data.entries) { seconds += entry.usedSeconds; frames += entry.frames; }
      showSummary(data, seconds, frames);
    } catch { /* the next full load will catch up */ }
  }

  /* ------------------------------------------------------------- loading */

  async function load() {
    if (sched.loading) return;
    sched.loading = true;
    try {
      const [data, targetList] = await Promise.all([
        app.api('/api/plan'),
        app.api('/api/targets'),
      ]);
      sched.data = data;
      sched.targets = targetList.targets;
      $('schedName').value = data.plan.name || '';
      // Which night this payload describes, so the rollover check below knows
      // when what is on screen has stopped being tonight.
      sched.night = data.night ? data.night.date : null;
      render();
      $('schedSaved').textContent = 'saved automatically';
    } catch (error) {
      $('schedList').innerHTML = '';
      const message = document.createElement('p');
      message.className = 'muted small';
      message.textContent = error.message;
      $('schedList').appendChild(message);
    } finally {
      sched.loading = false;
    }
  }

  /* ------------------------------------------------------ drag to reorder */

  /* The running order is the order of the boxes, so dragging one is how the
     sequence gets rearranged by hand. */
  function bindDragging() {
    const host = $('schedList');
    let dragged = null;

    host.addEventListener('dragstart', (event) => {
      const box = event.target.closest('.plan-box');
      if (!box) return;
      dragged = box;
      box.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      // Firefox will not start a drag without something on the transfer.
      event.dataTransfer.setData('text/plain', box.dataset.entryId);
    });

    host.addEventListener('dragend', () => {
      if (dragged) dragged.classList.remove('dragging');
      dragged = null;
      host.querySelectorAll('.drop-before, .drop-after')
        .forEach((el) => el.classList.remove('drop-before', 'drop-after'));
    });

    host.addEventListener('dragover', (event) => {
      if (!dragged) return;
      event.preventDefault();
      const over = event.target.closest('.plan-box');
      host.querySelectorAll('.drop-before, .drop-after')
        .forEach((el) => el.classList.remove('drop-before', 'drop-after'));
      if (!over || over === dragged) return;
      const box = over.getBoundingClientRect();
      const after = (event.clientY - box.top) > box.height / 2;
      over.classList.add(after ? 'drop-after' : 'drop-before');
    });

    host.addEventListener('drop', async (event) => {
      if (!dragged) return;
      event.preventDefault();
      const over = event.target.closest('.plan-box');
      if (over && over !== dragged) {
        const box = over.getBoundingClientRect();
        const after = (event.clientY - box.top) > box.height / 2;
        over.parentNode.insertBefore(dragged, after ? over.nextSibling : over);
      }
      const ids = [...host.querySelectorAll('.plan-box')].map((b) => b.dataset.entryId);
      try {
        await app.api('/api/plan/order', 'POST', { entryIds: ids });
        $('schedSaved').textContent = 'order saved';
      } catch (error) { app.toast(error.message, 'error'); }
      load();
    });
  }

  /* --------------------------------------------------------- the sequencer */

  function updateSequenceBar(status) {
    const sequence = status || {};
    const running = !!sequence.running;
    const paused = !!sequence.paused;

    $('btnSeqStart').disabled = running;
    $('btnSeqLoop').disabled = running;
    $('btnSeqPause').disabled = !running;
    $('btnSeqSkip').disabled = !running;
    $('btnSeqStop').disabled = !running;
    $('btnSeqPause').textContent = paused ? 'Resume' : 'Pause';

    // Abort is not gated on a sequence running: "something is wrong, shut it
    // down" is not a thing that only happens mid-plan. It is gated on a
    // shutdown already being under way, because parking twice is a slew twice.
    const shutdown = sequence.shutdown || {};
    $('btnSeqAbort').disabled = !!shutdown.busy;
    // Says which step it is on: a shutdown can take minutes between a slow park
    // and a slow roof, and a button that says nothing for that long gets
    // pressed again.
    $('btnSeqAbort').textContent = shutdown.busy
      ? (sequence.shutdownStep ? `${sequence.shutdownStep}…` : 'Shutting down…')
      : 'Abort & park';

    const dot = $('seqDot');
    dot.className = 'seq-dot'
      + (sequence.error ? ' error'
        : sequence.recovery ? ' paused'
          : running ? (paused ? ' paused' : ' running') : '');

    const bits = [];
    // Held by the weather reads very differently from held by a person, and at
    // three in the morning the difference is the whole message.
    if (sequence.weatherHold) bits.push('waiting for the sky to clear');
    else if (sequence.error) bits.push(`error: ${sequence.error}`);
    else if (sequence.recovery) {
      bits.push(`recovering — ${sequence.recovery.reason || sequence.recovery.kind}`);
    } else if (running) {
      // On loop, between nights, the run is alive but nothing is moving:
      // say when the next night opens rather than "waiting".
      if (sequence.loop) {
        bits.push(`on loop${sequence.nights ? ` · night ${sequence.nights + 1}` : ''}`);
        if (sequence.loopNext) {
          const at = new Date(sequence.loopNext * 1000);
          bits.push(`next night opens at ${at.getHours()}:${String(at.getMinutes()).padStart(2, '0')}`);
        }
      }
      bits.push(paused ? 'paused' : (sequence.state || 'running'));
      if (sequence.message) bits.push(sequence.message);
      if (sequence.panels > 1) bits.push(`panel ${sequence.panel}/${sequence.panels}`);
      if (sequence.frames) bits.push(`frame ${sequence.frame}/${sequence.frames}`);
      if (sequence.flips) bits.push(`${sequence.flips} flip${sequence.flips === 1 ? '' : 's'}`);
      if (sequence.focusRuns) bits.push(`${sequence.focusRuns} focus run(s)`);
      // Worth saying even once the rescue is over: a run that has been put back
      // on its feet four times is a run with something wrong with it.
      if (sequence.rescues) {
        bits.push(`${sequence.rescues} recover${sequence.rescues === 1 ? 'y' : 'ies'}`);
      }
    } else {
      bits.push(sequence.message || 'idle');
    }
    const focus = sequence.focus && sequence.focus.last;
    if (!running && focus && focus.bestPosition !== null) {
      bits.push(`last focus ${focus.bestPosition}`
        + (focus.bestHfd ? ` (HFD ${focus.bestHfd})` : ''));
    }
    $('seqState').textContent = bits.join('  ·  ');
  }

  function bindSequence() {
    $('btnSeqStart').addEventListener('click', async () => {
      const data = sched.data;
      const count = data && data.entries ? data.entries.length : 0;
      const frames = data && data.entries
        ? data.entries.reduce((sum, e) => sum + e.frames, 0) : 0;
      const ok = await app.confirmAction(
        `Run the sequence now? It will slew the mount, rotate the camera, focus `
        + `and take ${frames} frames across ${count} target`
        + `${count === 1 ? '' : 's'}, unattended.`,
        { title: 'Run sequence', confirmLabel: 'Run', danger: true });
      if (!ok) return;
      try { await app.api('/api/sequence/start', 'POST'); }
      catch (error) { app.toast(error.message, 'error'); }
    });

    $('btnSeqLoop').addEventListener('click', async () => {
      const data = sched.data;
      const count = data && data.entries ? data.entries.length : 0;
      const ok = await app.confirmAction(
        `Run the plan tonight and every night after, unattended? Each night `
        + `opens before dark with the mount released and homed, the cameras `
        + `cooling and the flat panel's cover open; it shoots the ${count} `
        + `target${count === 1 ? '' : 's'} on the plan; and at dawn, or when `
        + `the plan runs out, it closes the cover, homes and parks the mount, `
        + `shuts the roof and warms the cameras — then waits for the next dusk. `
        + `Stop or Abort & park ends it.`,
        { title: 'Run on loop', confirmLabel: 'Run every night', danger: true });
      if (!ok) return;
      try { await app.api('/api/sequence/loop', 'POST'); }
      catch (error) { app.toast(error.message, 'error'); }
    });

    $('btnSeqPause').addEventListener('click', () => {
      const sequence = (app.state.status && app.state.status.sequence) || {};
      app.api(`/api/sequence/${sequence.paused ? 'resume' : 'pause'}`, 'POST')
        .catch((error) => app.toast(error.message, 'error'));
    });

    $('btnSeqSkip').addEventListener('click', async () => {
      const ok = await app.confirmAction('Skip the target being shot and move on?',
        { title: 'Skip target', confirmLabel: 'Skip', danger: true });
      if (ok) app.api('/api/sequence/skip', 'POST').catch(() => {});
    });

    $('btnSeqStop').addEventListener('click', async () => {
      const sequence = (app.state.status && app.state.status.sequence) || {};
      const ok = await app.confirmAction(
        'Stop the sequence? The current frame is abandoned. The mount keeps '
        + 'tracking where it is; it is not parked.'
        + (sequence.loop ? ' This ends the loop as well: no further nights run '
          + 'until you start it again.' : ''),
        { title: 'Stop sequence', confirmLabel: 'Stop', danger: true });
      if (ok) app.api('/api/sequence/stop', 'POST').catch(() => {});
    });

    $('btnSeqAbort').addEventListener('click', async () => {
      const ok = await app.confirmAction(
        'Stop the sequence and put the observatory to bed? Guiding stops, the '
        + 'cameras stop, the cover closes, the mount parks and the cameras warm '
        + 'up. The mount will slew to its park position.',
        { title: 'Abort and park', confirmLabel: 'Abort & park', danger: true });
      if (!ok) return;
      try { await app.api('/api/sequence/shutdown', 'POST'); }
      catch (error) { app.toast(error.message, 'error'); }
    });
  }

  function bindControls() {
    $('btnSchedArrange').addEventListener('click', async () => {
      const fill = $('planChooseExposures').checked;
      let result;
      try {
        result = await app.api('/api/plan/arrange', 'POST',
          { chooseExposures: fill });
      } catch (error) { app.toast(error.message, 'error'); return; }

      const placed = result.order.length;
      app.toast(
        `Arranged ${placed} target${placed === 1 ? '' : 's'} — `
        + `${duration(result.usedSeconds)} imaging, ${duration(result.idleSeconds)} idle`,
        'success');
      for (const item of result.unplaced) {
        app.toast(`${item.name}: ${item.reason}`, 'info', 6000);
      }
      sched.arranged = result;
      load();
    });

    $('btnSchedAdd').addEventListener('click', async () => {
      const targetId = $('schedAddTarget').value;
      if (!targetId) return;
      try {
        await app.api('/api/plan/entries', 'POST', { targetId });
      } catch (error) { app.toast(error.message, 'error'); return; }
      load();
    });

    $('schedName').addEventListener('change', () => {
      app.api('/api/plan', 'POST', { name: $('schedName').value })
        .catch((error) => app.toast(error.message, 'error'));
    });

    bindSequences();

    // The graphs are sized from their container, so they need a redraw when it
    // changes width. Width only, and the panel rather than the list: now that
    // the whole tab scrolls, the list grows with its own contents, and watching
    // its height would have every render trigger the next one.
    let resizeTimer = null;
    let lastWidth = 0;
    const panel = document.querySelector('[data-panel="plan"]');
    new ResizeObserver((entries) => {
      const width = Math.round(entries[0].contentRect.width);
      if (width === lastWidth) return;
      lastWidth = width;
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (sched.data && sched.data.night) render();
      }, 200);
    }).observe(panel);
  }

  function init() {
    bindControls();
    bindDragging();
    bindSequence();
    loadSequences();
    let wasOn = false;
    app.onStatus((status, tab) => {
      updateSequenceBar(status && status.sequence);
      // Cheap unless a framing canvas has actually changed width — which it
      // does whenever a side panel opens, without the plan re-rendering.
      if ($('schedList').offsetParent !== null) showFramings();
      // The Earth keeps turning whether or not anyone touches the interface.
      checkNightRolled();
      // On arriving at the tab, not on every tick. `load` rebuilds every box
      // from the server, so running it four times a second replaces the input
      // under the cursor while it is being typed into — the frame count jumps
      // back to what the server last saw, then forward again when the save
      // lands, and a mosaic makes it worse because the per-panel ceiling is
      // small enough to be hit on the way up.
      const arrived = tab === 'plan' && !wasOn;
      wasOn = tab === 'plan';
      if (arrived) load();
      // While a sequence runs, keep the boxes in step with it without
      // fighting whatever the operator is typing.
      const sequence = (status && status.sequence) || {};
      if (sequence.running && sched.data
          && $('schedList').offsetParent !== null
          && !document.activeElement.matches('input')) {
        const active = document.querySelector('.plan-box.active');
        const badge = active && active.querySelector('.run-badge');
        const stage = sequence.paused ? 'paused'
          : sequence.recovery ? 'recovering' : (sequence.state || 'running');
        // Re-render when the run moves on, and also when the stage changes, so
        // the badge on the running target keeps up with what it is doing.
        if (!active || active.dataset.entryId !== sequence.entryId
            || (badge && badge.textContent !== stage)) render();
        // The panel being shot moves without any of the above changing; the
        // framing repaints itself only when it has, so this is cheap.
        else showFramings(active);
      }
    });
  }

  document.addEventListener('DOMContentLoaded', init);
}());
