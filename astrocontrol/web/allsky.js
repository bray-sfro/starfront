/* The all-sky survey tab: the grid, where it is in the sky, and how far it has
 * got.
 *
 * Right ascension runs right to left and declination upwards, which is the way
 * a star chart runs — so the constellations come out the right way round rather
 * than mirrored. Bright stars, the deep-sky catalogue and the Moon are drawn
 * under the grid, because "field R034F0122" means nothing on its own and
 * "the field on Cygnus' wing" means a great deal.
 *
 * Progress is drawn per field rather than as a number, because a survey that is
 * "14% done" tells you nothing about whether the 14% is a band, a patch, or a
 * scatter of half-finished fields.
 */
'use strict';

(function () {
  const app = window.astro;
  const $ = app.$;
  const DEG = Math.PI / 180;

  const as = {
    surveys: [],       // /api/allsky
    id: null,          // the survey being looked at
    grid: null,        // /api/allsky/<id>/grid, as parallel arrays
    tonight: null,     // /api/allsky/<id>/tonight
    goal: [],          // the per-field filters, as edited
    dirty: false,
    hover: -1,
    selected: null,
    boxes: [],         // pixel rectangles, for hit testing
    settings: {},
    filters: [],
    // The sky itself, loaded once and shared with the planetarium's catalogue.
    stars: [],
    bright: [],        // the naked-eye ones, with unit vectors ready
    links: [],         // bright-star links, per constellation
    labels: [],        // constellation names at their bright-star centroids
    deepSky: [],
    zoom: 1,
    panX: 0,
    panY: 0,
    drawn: 0,          // how many cells the last pass actually painted
  };

  const PAD = { left: 46, right: 12, top: 12, bottom: 26 };

  /* The three-letter designations the star catalogue uses, spelled out. Only
     for the label on the chart; anything not listed shows its abbreviation. */
  const CONSTELLATIONS = {
    And: 'Andromeda', Ant: 'Antlia', Aps: 'Apus', Aqr: 'Aquarius',
    Aql: 'Aquila', Ara: 'Ara', Ari: 'Aries', Aur: 'Auriga', Boo: 'Boötes',
    Cae: 'Caelum', Cam: 'Camelopardalis', Cnc: 'Cancer',
    CVn: 'Canes Venatici', CMa: 'Canis Major', CMi: 'Canis Minor',
    Cap: 'Capricornus', Car: 'Carina', Cas: 'Cassiopeia', Cen: 'Centaurus',
    Cep: 'Cepheus', Cet: 'Cetus', Cha: 'Chamaeleon', Cir: 'Circinus',
    Col: 'Columba', Com: 'Coma Berenices', CrA: 'Corona Australis',
    CrB: 'Corona Borealis', Crv: 'Corvus', Crt: 'Crater', Cru: 'Crux',
    Cyg: 'Cygnus', Del: 'Delphinus', Dor: 'Dorado', Dra: 'Draco',
    Equ: 'Equuleus', Eri: 'Eridanus', For: 'Fornax', Gem: 'Gemini',
    Gru: 'Grus', Her: 'Hercules', Hor: 'Horologium', Hya: 'Hydra',
    Hyi: 'Hydrus', Ind: 'Indus', Lac: 'Lacerta', Leo: 'Leo',
    LMi: 'Leo Minor', Lep: 'Lepus', Lib: 'Libra', Lup: 'Lupus', Lyn: 'Lynx',
    Lyr: 'Lyra', Men: 'Mensa', Mic: 'Microscopium', Mon: 'Monoceros',
    Mus: 'Musca', Nor: 'Norma', Oct: 'Octans', Oph: 'Ophiuchus',
    Ori: 'Orion', Pav: 'Pavo', Peg: 'Pegasus', Per: 'Perseus', Phe: 'Phoenix',
    Pic: 'Pictor', Psc: 'Pisces', PsA: 'Piscis Austrinus', Pup: 'Puppis',
    Pyx: 'Pyxis', Ret: 'Reticulum', Sge: 'Sagitta', Sgr: 'Sagittarius',
    Sco: 'Scorpius', Scl: 'Sculptor', Sct: 'Scutum', Ser: 'Serpens',
    Sex: 'Sextans', Tau: 'Taurus', Tel: 'Telescopium', Tri: 'Triangulum',
    TrA: 'Triangulum Australe', Tuc: 'Tucana', UMa: 'Ursa Major',
    UMi: 'Ursa Minor', Vel: 'Vela', Vir: 'Virgo', Vol: 'Volans',
    Vul: 'Vulpecula',
  };

  /* --------------------------------------------------------------- helpers */

  function duration(seconds) {
    if (!seconds || seconds <= 0) return '0m';
    const hours = seconds / 3600;
    if (hours >= 48) return `${Math.round(hours / 24)} days`;
    if (hours >= 1) return `${hours.toFixed(hours < 10 ? 1 : 0)}h`;
    return `${Math.round(seconds / 60)}m`;
  }

  const clock = (ts) => (!ts ? '—'
    : new Date(ts * 1000).toLocaleTimeString([],
      { hour: '2-digit', minute: '2-digit', hour12: false }));

  const css = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  /* --------------------------------------------------------------- the sky */

  /** The star catalogue, once, shared with the planetarium's copy. */
  async function loadSky() {
    if (as.stars.length) return;
    try {
      const response = await fetch('vendor/stars.json?v=2', { cache: 'force-cache' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      as.stars = (payload.stars || []).map(([ra, dec, mag, , name, bayer]) => ({
        ra: ra / 15, dec, mag, name, bayer,
        // "Alp CMa" — the last word is the constellation.
        constellation: (bayer || '').split(' ').pop() || '',
      }));
      buildConstellations();
    } catch (error) {
      console.warn('all-sky: no star catalogue —', error);
    }
    try {
      const payload = await app.api('/api/catalog');
      as.deepSky = (payload.objects || []).filter((o) => o.ra !== undefined);
    } catch { /* landmarks are a nicety, not a requirement */ }
  }

  /** Join each constellation's bright stars to their nearest neighbours.
   *
   *  These are *not* the traditional figures — no such data ships with the
   *  catalogue, and drawing invented ones would be worse than drawing none.
   *  What this does is connect each constellation's brightest stars into a
   *  minimum spanning tree and throw away any link longer than a constellation
   *  plausibly spans. For most of the sky that lands close to the familiar
   *  shape, and where it does not it is still an honest picture of where that
   *  constellation's bright stars actually are. The legend says so. */
  function buildConstellations() {
    const groups = new Map();
    for (const star of as.stars) {
      if (!star.constellation || star.mag > 4.2) continue;
      if (!groups.has(star.constellation)) groups.set(star.constellation, []);
      groups.get(star.constellation).push(star);
    }

    as.links = [];
    as.labels = [];
    // Kept with their unit vectors, for the hover lookup.
    as.bright = as.stars
      .filter((s) => s.constellation && s.mag <= 4.2)
      .map((s) => ({ constellation: s.constellation, v: vector(s.ra, s.dec) }));
    for (const [name, stars] of groups) {
      if (stars.length < 2) continue;

      // A constellation can straddle RA 0, so everything is done in unit
      // vectors — no wrapping to get wrong.
      const points = stars.map((s) => ({ s, v: vector(s.ra, s.dec) }));
      const inTree = [0];
      const outside = points.map((_, i) => i).slice(1);
      while (outside.length) {
        let best = null;
        for (const a of inTree) {
          for (const b of outside) {
            const d = angle(points[a].v, points[b].v);
            if (!best || d < best.d) best = { a, b, d };
          }
        }
        // A single link longer than this is not a constellation figure, it is
        // two halves of the sky being joined for the sake of it.
        if (best.d <= 28) {
          as.links.push([points[best.a].s, points[best.b].s]);
        }
        inTree.push(best.b);
        outside.splice(outside.indexOf(best.b), 1);
      }

      // The label goes at the mean direction of the bright stars, which is the
      // middle of the constellation however it wraps.
      const mean = [0, 0, 0];
      for (const p of points) for (let k = 0; k < 3; k += 1) mean[k] += p.v[k];
      const norm = Math.hypot(...mean) || 1;
      const unit = mean.map((c) => c / norm);
      as.labels.push({
        name: CONSTELLATIONS[name] || name,
        dec: Math.asin(Math.max(-1, Math.min(1, unit[2]))) / DEG,
        ra: ((Math.atan2(unit[1], unit[0]) / DEG + 360) % 360) / 15,
        weight: stars.length,
      });
    }
  }

  const vector = (raHours, dec) => {
    const a = raHours * 15 * DEG;
    const d = dec * DEG;
    return [Math.cos(d) * Math.cos(a), Math.cos(d) * Math.sin(a), Math.sin(d)];
  };

  const angle = (u, v) => Math.acos(
    Math.max(-1, Math.min(1, u[0] * v[0] + u[1] * v[1] + u[2] * v[2]))) / DEG;

  /** Which constellation a point is nearest to, by its closest bright star.
   *
   *  Not the IAU boundaries — the catalogue does not carry them — so the answer
   *  is "in Cygnus" in the sense of "among Cygnus' stars". Near a boundary it
   *  may name the neighbour; in the middle of a constellation it is right.
   *  Called on every mouse move, so the vectors are worked out once. */
  function constellationAt(raHours, dec) {
    if (!as.bright.length) return '';
    const v = vector(raHours, dec);
    let best = null;
    for (const star of as.bright) {
      // The dot product alone orders the same way the angle does, and skips an
      // arccos per star.
      const dot = v[0] * star.v[0] + v[1] * star.v[1] + v[2] * star.v[2];
      if (!best || dot > best.dot) best = { dot, star };
    }
    if (!best) return '';
    return CONSTELLATIONS[best.star.constellation] || best.star.constellation;
  }

  /* ------------------------------------------------------------- the chart */

  /** The projection, and its inverse, for the current zoom and pan. */
  function view(box, grid) {
    const decMin = grid ? grid.allsky.decMin : -90;
    const decMax = grid ? grid.allsky.decMax : 90;
    const width = (box.width - PAD.left - PAD.right) * as.zoom;
    const height = (box.height - PAD.top - PAD.bottom) * as.zoom;
    // Right ascension increases to the *left*, the way it does on a star chart
    // — otherwise every constellation comes out mirrored and unrecognisable.
    const x = (raHours) => PAD.left + as.panX + (1 - raHours / 24) * width;
    const y = (dec) => PAD.top + as.panY
      + (1 - (dec - decMin) / (decMax - decMin)) * height;
    return {
      x, y, width, height, decMin, decMax,
      raAt: (px) => ((1 - (px - PAD.left - as.panX) / width) * 24 + 24) % 24,
      decAt: (py) => decMin
        + (1 - (py - PAD.top - as.panY) / height) * (decMax - decMin),
    };
  }

  function drawChart() {
    const canvas = $('asChart');
    if (!canvas || canvas.hidden) return;
    const box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return;

    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(box.width * ratio);
    canvas.height = Math.round(box.height * ratio);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, box.width, box.height);

    const grid = as.grid;
    if (!grid) {
      ctx.fillStyle = css('--muted');
      ctx.font = '12px system-ui, sans-serif';
      ctx.fillText('No survey selected.', PAD.left, 30);
      as.boxes = [];
      return;
    }

    const v = view(box, grid);

    // Everything is clipped to the chart, so panning does not smear the sky
    // over the axis labels.
    ctx.save();
    ctx.beginPath();
    ctx.rect(PAD.left, PAD.top, box.width - PAD.left - PAD.right,
      box.height - PAD.top - PAD.bottom);
    ctx.clip();

    ctx.fillStyle = '#0b0e14';
    ctx.fillRect(PAD.left, PAD.top, box.width - PAD.left - PAD.right,
      box.height - PAD.top - PAD.bottom);

    drawFields(ctx, grid, v);
    if ($('asShowSky').checked) {
      drawConstellations(ctx, v);
      drawStars(ctx, v);
      drawDeepSky(ctx, v);
    }
    drawMoon(ctx, v);
    drawTonight(ctx, grid, v);
    if ($('asShowSky').checked) drawLabels(ctx, v, box);
    ctx.restore();

    drawAxes(ctx, box, v);

    // Said out loud, so "are they all there?" is a question with an answer on
    // the screen rather than one you have to squint at the chart to guess.
    const tonight = (as.tonight && as.tonight.fields) ? as.tonight.fields.length : 0;
    setText('asDrawn', `${as.drawn.toLocaleString()} of `
      + `${grid.ra.length.toLocaleString()} fields drawn`
      + (tonight ? `, ${tonight} outlined for tonight` : ''));
  }

  function drawFields(ctx, grid, v) {
    const onlyReachable = $('asShowReach').checked;
    const rowHeight = Math.max(
      1.2, v.height / Math.max(1, grid.summary.rings.length));

    as.boxes = [];
    let drawn = 0;
    for (let i = 0; i < grid.ra.length; i += 1) {
      const reachable = grid.reachable[i] === 1;
      if (onlyReachable && !reachable) continue;
      const fraction = grid.fraction[i];
      const cellWidth = Math.max(1.0, v.width / ringSize(grid, grid.ring[i]));
      const left = v.x(grid.ra[i]) - cellWidth / 2;
      const top = v.y(grid.dec[i]) - rowHeight / 2;
      if (left > PAD.left + v.width + 40 || left + cellWidth < PAD.left - 40) continue;

      // Solid, not translucent. The stars are drawn over the top of this
      // rather than under it, so there was never anything to see through to —
      // and a dark fill at half opacity on a near-black chart is a five per
      // cent difference from the background, which is to say invisible.
      // Slate blue to amber to green: a progression in hue as well as in
      // brightness, so the states are told apart by two things at once rather
      // than by one at six pixels across.
      ctx.fillStyle = !reachable ? '#2e3441'
        : fraction >= 1 ? css('--good')
          : fraction > 0 ? partial(fraction)
            : '#3f4d6b';

      // A hair of a gap, so ten thousand cells read as cells rather than as
      // one flat wash of colour. Dropped when they are too small to spare it.
      const gap = (cellWidth > 3.5 && rowHeight > 3.5) ? 0.6 : 0;
      ctx.fillRect(left + gap / 2, top + gap / 2,
        Math.max(0.5, cellWidth - gap), Math.max(0.5, rowHeight - gap));
      drawn += 1;
      as.boxes.push({ i, left, top, w: cellWidth, h: rowHeight });
    }
    as.drawn = drawn;
  }

  /** Amber, darker the less of the field is done — but never so dark that it
   *  cannot be told from an untouched one. */
  function partial(fraction) {
    const t = Math.max(0.32, Math.min(1, fraction));
    return `rgb(${Math.round(62 + 155 * t)},${Math.round(46 + 114 * t)},`
      + `${Math.round(22 + 39 * t)})`;
  }

  function drawConstellations(ctx, v) {
    if (!as.links.length) return;
    ctx.strokeStyle = '#46506a';
    ctx.lineWidth = 1;
    ctx.globalAlpha = 0.75;
    ctx.beginPath();
    for (const [a, b] of as.links) {
      // A link that spans the RA wrap would be drawn straight across the whole
      // chart, which is a lie about where it goes; leave it out.
      if (Math.abs(a.ra - b.ra) > 12) continue;
      ctx.moveTo(v.x(a.ra), v.y(a.dec));
      ctx.lineTo(v.x(b.ra), v.y(b.dec));
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  function drawStars(ctx, v) {
    // Fainter stars are only worth drawing once there is room for them.
    const limit = as.zoom >= 6 ? 7.0 : as.zoom >= 3 ? 6.0 : as.zoom >= 1.8 ? 5.2 : 4.5;
    ctx.fillStyle = '#c9d4e6';
    for (const star of as.stars) {
      if (star.mag > limit) break;              // sorted brightest first
      const px = v.x(star.ra);
      const py = v.y(star.dec);
      if (px < PAD.left - 5 || px > PAD.left + v.width + 5) continue;
      const size = Math.max(0.5, (limit - star.mag) * 0.42);
      ctx.globalAlpha = Math.min(1, 0.35 + (limit - star.mag) * 0.16);
      ctx.beginPath();
      ctx.arc(px, py, size, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;

    // Named stars, once there is room to read them.
    if (as.zoom >= 2.2) {
      ctx.fillStyle = '#8d9ab3';
      ctx.font = '9px system-ui, sans-serif';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      for (const star of as.stars) {
        if (star.mag > 2.6 || !star.name) continue;
        ctx.fillText(star.name, v.x(star.ra) + 4, v.y(star.dec));
      }
    }
  }

  function drawDeepSky(ctx, v) {
    if (!as.deepSky.length || as.zoom < 1.4) return;
    ctx.strokeStyle = '#7fb2d9';
    ctx.fillStyle = '#7fb2d9';
    ctx.font = '9px system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.globalAlpha = 0.85;
    for (const object of as.deepSky) {
      const px = v.x(object.ra);
      const py = v.y(object.dec);
      if (px < PAD.left || px > PAD.left + v.width) continue;
      ctx.beginPath();
      ctx.arc(px, py, 2.5, 0, Math.PI * 2);
      ctx.stroke();
      if (as.zoom >= 2.5) ctx.fillText(object.name || '', px + 5, py);
    }
    ctx.globalAlpha = 1;
  }

  function drawMoon(ctx, v) {
    const moon = as.tonight && as.tonight.moon;
    if (!moon || moon.ra === null || moon.ra === undefined) return;
    const px = v.x(moon.ra);
    const py = v.y(moon.dec);
    // The circle is the radius fields are kept out of, drawn at the scale of
    // the chart so it can be seen eating into the grid.
    const scale = v.width / 360;
    ctx.strokeStyle = css('--warn');
    ctx.globalAlpha = 0.5;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.arc(px, py, Math.max(4, moon.avoidance * scale), 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 0.9;
    ctx.fillStyle = css('--warn');
    ctx.beginPath();
    ctx.arc(px, py, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  function drawTonight(ctx, grid, v) {
    if (!$('asShowTonight').checked || !as.tonight || !as.tonight.fields) return;
    const index = new Map();
    grid.ids.forEach((id, i) => index.set(id, i));
    const rowHeight = Math.max(
      1.2, v.height / Math.max(1, grid.summary.rings.length));

    // The path first, so the walk from one field to the next is visible — the
    // whole point of choosing neighbours is that this line stays short.
    ctx.strokeStyle = css('--accent');
    ctx.globalAlpha = 0.55;
    ctx.lineWidth = 1;
    ctx.beginPath();
    let started = false;
    for (const field of as.tonight.fields) {
      const i = index.get(field.id);
      if (i === undefined) continue;
      const px = v.x(grid.ra[i]);
      const py = v.y(grid.dec[i]);
      if (started) ctx.lineTo(px, py); else { ctx.moveTo(px, py); started = true; }
    }
    ctx.stroke();
    ctx.globalAlpha = 1;

    // A ring around the cell rather than a thick outline over it: at six pixels
    // across, a 1.4px stroke *is* the whole cell, which is why the scheduled
    // fields used to be the only thing on the chart you could see.
    ctx.strokeStyle = css('--accent');
    ctx.lineWidth = 1;
    for (const field of as.tonight.fields) {
      const i = index.get(field.id);
      if (i === undefined) continue;
      const cellWidth = Math.max(2, v.width / ringSize(grid, grid.ring[i]));
      const left = v.x(grid.ra[i]) - cellWidth / 2;
      const top = v.y(grid.dec[i]) - rowHeight / 2;
      // Half-pixel offsets so the stroke lands on the pixel rather than
      // straddling two of it and coming out blurred and twice as fat.
      ctx.strokeRect(Math.round(left) - 1.5, Math.round(top) - 1.5,
        Math.round(cellWidth) + 3, Math.round(rowHeight) + 3);
    }
  }

  function drawLabels(ctx, v, box) {
    if (!as.labels.length) return;
    ctx.fillStyle = '#6e7a92';
    ctx.font = `${as.zoom >= 2 ? 11 : 9}px system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (const label of as.labels) {
      // Only the bigger constellations while zoomed out, or the chart is a
      // wall of overlapping names.
      if (as.zoom < 1.6 && label.weight < 6) continue;
      const px = v.x(label.ra);
      const py = v.y(label.dec);
      if (px < PAD.left || px > box.width - PAD.right) continue;
      if (py < PAD.top || py > box.height - PAD.bottom) continue;
      ctx.fillText(label.name, px, py);
    }
  }

  /** How many fields the ring at this index holds, cached off the summary. */
  function ringSize(grid, ring) {
    if (!grid._ringSize) {
      grid._ringSize = new Map();
      for (const row of grid.summary.rings) grid._ringSize.set(row.ring, row.fields);
    }
    return grid._ringSize.get(ring) || 1;
  }

  function drawAxes(ctx, box, v) {
    ctx.strokeStyle = css('--line');
    ctx.fillStyle = css('--muted');
    ctx.font = '10px var(--mono), monospace';
    ctx.lineWidth = 1;
    const right = box.width - PAD.right;
    const bottom = box.height - PAD.bottom;

    ctx.save();
    ctx.beginPath();
    ctx.rect(PAD.left, PAD.top, right - PAD.left, bottom - PAD.top);
    ctx.clip();
    ctx.globalAlpha = 0.3;
    const raStep = as.zoom >= 4 ? 1 : as.zoom >= 2 ? 2 : 3;
    for (let hour = 0; hour <= 24; hour += raStep) {
      const at = v.x(hour);
      ctx.beginPath();
      ctx.moveTo(at, PAD.top);
      ctx.lineTo(at, bottom);
      ctx.stroke();
    }
    const decStep = as.zoom >= 3 ? 5 : (v.decMax - v.decMin) > 90 ? 30 : 15;
    for (let dec = Math.ceil(v.decMin / decStep) * decStep;
      dec <= v.decMax; dec += decStep) {
      const at = v.y(dec);
      ctx.beginPath();
      ctx.moveTo(PAD.left, at);
      ctx.lineTo(right, at);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    ctx.restore();

    // Labels outside the clip, so they stay put while the sky pans under them.
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (let hour = 0; hour <= 24; hour += raStep) {
      const at = v.x(hour);
      if (at < PAD.left || at > right || hour === 24) continue;
      ctx.fillText(`${hour}h`, at, bottom + 5);
    }
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let dec = Math.ceil(v.decMin / decStep) * decStep;
      dec <= v.decMax; dec += decStep) {
      const at = v.y(dec);
      if (at < PAD.top || at > bottom) continue;
      ctx.fillText(`${dec > 0 ? '+' : ''}${dec}°`, PAD.left - 5, at);
    }
  }

  function setZoom(next, anchorX, anchorY) {
    const canvas = $('asChart');
    const box = canvas.getBoundingClientRect();
    const before = view(box, as.grid);
    const wanted = Math.max(1, Math.min(14, next));
    if (Math.abs(wanted - as.zoom) < 1e-6) return;

    // Keep whatever is under the pointer under the pointer.
    const px = anchorX === undefined ? PAD.left + before.width / 2 : anchorX;
    const py = anchorY === undefined ? PAD.top + before.height / 2 : anchorY;
    const ra = before.raAt(px);
    const dec = before.decAt(py);
    as.zoom = wanted;
    const after = view(box, as.grid);
    as.panX += px - after.x(ra);
    as.panY += py - after.y(dec);
    clampPan(box);
    setText('asZoomLabel', `${as.zoom.toFixed(1)}×`);
    drawChart();
  }

  /** Put the view over the sky tonight's fields are in.
   *
   *  A night's work is a handful of fields out of ten thousand, which at the
   *  whole-sky zoom is a scattering of outlines a few pixels across. This is
   *  the button that answers "so where is it actually going?". */
  function frameTonight() {
    const fields = (as.tonight && as.tonight.fields) || [];
    if (!fields.length || !as.grid) {
      app.toast('Nothing is scheduled for tonight', 'info');
      return;
    }
    const decs = fields.map((f) => f.dec);
    // Right ascension wraps, so the span is measured the short way round from
    // the first field rather than by subtracting the extremes.
    const anchor = fields[0].ra;
    const offsets = fields.map((f) => ((f.ra - anchor + 36) % 24) - 12);
    const raSpan = Math.max(...offsets) - Math.min(...offsets);
    const decSpan = Math.max(...decs) - Math.min(...decs);
    const middleRa = ((anchor + (Math.max(...offsets) + Math.min(...offsets)) / 2)
      + 24) % 24;
    const middleDec = (Math.max(...decs) + Math.min(...decs)) / 2;

    const canvas = $('asChart');
    const box = canvas.getBoundingClientRect();
    const full = as.grid.allsky.decMax - as.grid.allsky.decMin;
    // Enough zoom to fill about two thirds of the chart with the night's work.
    const byRa = raSpan > 0.05 ? (24 / raSpan) * 0.66 : 8;
    const byDec = decSpan > 0.2 ? (full / decSpan) * 0.66 : 8;
    as.zoom = Math.max(1, Math.min(14, Math.min(byRa, byDec)));

    as.panX = 0;
    as.panY = 0;
    const v = view(box, as.grid);
    as.panX = (PAD.left + (box.width - PAD.left - PAD.right) / 2) - v.x(middleRa);
    as.panY = (PAD.top + (box.height - PAD.top - PAD.bottom) / 2) - v.y(middleDec);
    clampPan(box);
    setText('asZoomLabel', `${as.zoom.toFixed(1)}×`);
    drawChart();
  }

  /** Never let the sky be dragged entirely off the chart. */
  function clampPan(box) {
    const width = (box.width - PAD.left - PAD.right) * as.zoom;
    const height = (box.height - PAD.top - PAD.bottom) * as.zoom;
    const visibleW = box.width - PAD.left - PAD.right;
    const visibleH = box.height - PAD.top - PAD.bottom;
    as.panX = Math.max(visibleW - width, Math.min(0, as.panX));
    as.panY = Math.max(visibleH - height, Math.min(0, as.panY));
  }

  const setText = (id, text) => { const n = $(id); if (n) n.textContent = text; };

  function fieldAt(event) {
    const canvas = $('asChart');
    const box = canvas.getBoundingClientRect();
    const px = event.clientX - box.left;
    const py = event.clientY - box.top;
    // Backwards, so the last drawn (and so the visible) cell wins.
    for (let k = as.boxes.length - 1; k >= 0; k -= 1) {
      const cell = as.boxes[k];
      if (px >= cell.left && px <= cell.left + cell.w
          && py >= cell.top && py <= cell.top + cell.h) {
        return cell.i;
      }
    }
    return -1;
  }

  /* ------------------------------------------------------------- the stats */

  function renderSummary() {
    const grid = as.grid;
    if (!grid) {
      $('asStats').textContent = 'No all-sky survey yet. Press New survey.';
      $('asBarFill').style.width = '0%';
      return;
    }
    const s = grid.summary;
    $('asBarFill').style.width = `${(s.fraction * 100).toFixed(2)}%`;
    const parts = [
      `${s.complete.toLocaleString()} of ${s.reachable.toLocaleString()} `
      + `fields complete (${(s.fraction * 100).toFixed(2)}%)`,
      `${s.started.toLocaleString()} part done`,
      `${s.untouched.toLocaleString()} untouched`,
      `${s.frames.toLocaleString()} frames`,
      `${duration(s.seconds)} on sky`,
    ];
    if (s.unreachable) {
      parts.push(`${s.unreachable.toLocaleString()} never rise high enough here`);
    }
    parts.push(`about ${duration(s.remainingSeconds)} left`);
    $('asStats').textContent = parts.join('   ·   ');

    // The tiling only holds at the angle it was laid out for.
    const warning = $('asAngleWarning');
    warning.hidden = !grid.angleWarning;
    warning.textContent = grid.angleWarning || '';
  }

  function renderTonight() {
    const plan = as.tonight;
    const list = $('asTonightList');
    list.innerHTML = '';
    if (!plan) {
      $('asTonightCount').textContent = '—';
      $('asTonightFlips').textContent = '—';
      $('asTonightAir').textContent = '—';
      $('asTonightNote').textContent = '—';
      return;
    }
    const fields = plan.fields || [];
    $('asTonightCount').textContent = String(fields.length);
    $('asTonightFlips').textContent = plan.flips === 0 ? 'none'
      : (plan.flips === null ? 'possible' : String(plan.flips));
    const airmasses = fields.map((f) => f.airmass).filter(Boolean).sort((a, b) => a - b);
    $('asTonightAir').textContent = airmasses.length
      ? airmasses[Math.floor(airmasses.length / 2)].toFixed(3) : '—';
    $('asTonightSlew').textContent = fields.length
      ? `${(plan.slewPerField || 0).toFixed(1)}°` : '—';
    $('asTonightSlewAll').textContent = fields.length
      ? `${Math.round(plan.slewDegrees || 0)}°` : '—';
    const patches = plan.patches || 0;
    $('asTonightPatches').textContent = fields.length ? String(patches) : '—';
    $('asTonightPatches').classList.toggle('warn-text', patches > 1);
    $('asTonightJoined').textContent = fields.length
      ? `${Math.round((plan.contiguous || 0) * 100)}%` : '—';
    $('asTonightJoined').classList.toggle('warn-text', (plan.contiguous || 0) < 0.8);

    // The one number that explains a scattered night, said where it is needed.
    const visitNote = $('asVisitNote');
    if (plan.goalMinutes) {
      const capped = plan.visitMinutes < plan.goalMinutes - 0.5;
      visitNote.textContent = capped
        ? `A field's full goal is ${plan.goalMinutes.toFixed(0)} min; each visit `
          + `takes ${plan.visitMinutes.toFixed(0)} min of it and the rest is `
          + 'picked up on later nights. That is what keeps the night to one '
          + 'joined-up run — the sky turns a new field onto the meridian every '
          + 'nine minutes or so, so a longer visit cannot be followed by its '
          + 'neighbour.'
        : `Each visit shoots the whole ${plan.goalMinutes.toFixed(0)}-minute `
          + 'goal. If that is much more than about ten minutes the night will '
          + 'come out as unconnected patches: the sky turns its neighbours past '
          + 'the meridian while you are still on one field.';
      visitNote.classList.toggle('warn-text', !capped && plan.goalMinutes > 12);
    } else {
      visitNote.textContent = '—';
    }

    const note = [];
    if (plan.detail) note.push(plan.detail);
    if (!plan.scheduled) note.push('not in the plan yet — Add to plan to run it');
    if (plan.windowStart) {
      note.push(`${clock(plan.windowStart)}–${clock(plan.windowEnd)}`);
    }
    if (plan.telescopes > 1) note.push(`${plan.telescopes} telescopes`);
    if (plan.skippedForMoon) note.push(`${plan.skippedForMoon} passed over for the Moon`);
    if (plan.visitMinutes) note.push(`${plan.visitMinutes.toFixed(0)} min a visit`);
    $('asTonightNote').textContent = note.join('  ·  ') || '—';

    for (const [position, field] of fields.slice(0, 60).entries()) {
      const row = document.createElement('div');
      row.className = 'as-tonight-row';
      row.innerHTML = '<b class="mono"></b><span class="mono"></span>'
        + '<span class="muted mono"></span>';
      row.querySelector('b').textContent = `${position + 1}. ${field.id}`;
      row.querySelector('span').textContent = clock(field.startAt);
      row.querySelector('.muted').textContent =
        `${field.altitude.toFixed(0)}° · X${(field.airmass || 0).toFixed(2)}`
        + (position ? ` · ${(field.slew || 0).toFixed(1)}° slew` : '')
        + (field.partial ? ' · part' : '');
      row.addEventListener('click', () => selectField(field.id));
      list.appendChild(row);
    }
    if (fields.length > 60) {
      const more = document.createElement('div');
      more.className = 'muted small';
      more.textContent = `…and ${fields.length - 60} more`;
      list.appendChild(more);
    }
  }

  async function selectField(fieldId) {
    if (!as.id) return;
    let detail;
    try {
      detail = await app.api(`/api/allsky/${as.id}/field/${fieldId}`);
    } catch (error) {
      app.toast(error.message, 'error');
      return;
    }
    as.selected = detail;
    const host = $('asField');
    host.hidden = false;
    const state = detail.state;
    const rows = Object.entries(state.byFilter || {})
      .map(([name, item]) => `${name} ${item.have}/${item.wanted}`)
      .join('   ');
    const window_ = detail.window || {};
    host.innerHTML = '<div class="as-field-head"></div>'
      + '<div class="as-field-body mono small"></div>'
      + '<button class="btn small ghost as-field-close">✕</button>';
    host.querySelector('.as-field-head').textContent =
      `${detail.field.id}   RA ${app.fmtHours(detail.field.ra)}   `
      + `Dec ${app.fmtDegrees(detail.field.dec)}`;
    host.querySelector('.as-field-body').textContent = [
      rows || 'nothing shot yet',
      state.complete ? 'complete' : `${(state.fraction * 100).toFixed(0)}% done`,
      state.lastNight ? `last shot ${state.lastNight}` : '',
      window_.transitAltitude !== undefined
        ? `transits at ${window_.transitAltitude}°` : '',
      detail.folder,
    ].filter(Boolean).join('\n');
    host.querySelector('.as-field-close').addEventListener('click', () => {
      host.hidden = true;
      as.selected = null;
    });
  }

  /* -------------------------------------------------------------- the goal */

  function goalRow(item, index, host, onChange) {
    const row = document.createElement('div');
    row.className = 'cal-set';

    const name = document.createElement('span');
    name.className = 'cal-set-kind';
    name.textContent = item.name || 'no filter';
    row.appendChild(name);

    const field = (label, node) => {
      const wrap = document.createElement('label');
      wrap.className = 'cal-field';
      const caption = document.createElement('span');
      caption.textContent = label;
      wrap.appendChild(caption);
      wrap.appendChild(node);
      row.appendChild(wrap);
      return node;
    };

    const exposure = document.createElement('input');
    exposure.type = 'number';
    exposure.min = 0.1;
    exposure.max = 3600;
    exposure.step = 1;
    exposure.value = item.exposure;
    exposure.addEventListener('change', () => {
      item.exposure = Number(exposure.value) || 1;
      onChange();
    });
    field('Exposure (s)', exposure);

    const count = document.createElement('input');
    count.type = 'number';
    count.min = 1;
    count.max = 1000;
    count.step = 1;
    count.value = item.count;
    count.addEventListener('change', () => {
      item.count = Math.max(1, Math.round(Number(count.value) || 1));
      onChange();
    });
    field('Frames', count);

    const spacer = document.createElement('span');
    spacer.className = 'spacer';
    row.appendChild(spacer);

    const remove = document.createElement('button');
    remove.className = 'btn small ghost danger';
    remove.textContent = 'Remove';
    remove.addEventListener('click', () => {
      host.splice(index, 1);
      onChange();
    });
    row.appendChild(remove);
    return row;
  }

  function renderGoal() {
    const host = $('asGoal');
    host.innerHTML = '';
    if (!as.goal.length) {
      host.innerHTML = '<p class="muted small">No filters set — the survey has '
        + 'nothing to shoot.</p>';
    }
    as.goal.forEach((item, index) => host.appendChild(
      goalRow(item, index, as.goal, () => { as.dirty = true; renderGoal(); })));

    const frames = as.goal.reduce((t, g) => t + (Number(g.count) || 0), 0);
    const seconds = as.goal.reduce(
      (t, g) => t + (Number(g.count) || 0) * (Number(g.exposure) || 0), 0);
    const scopes = (as.tonight && as.tonight.telescopes) || 1;
    $('asGoalNote').textContent = as.goal.length
      ? `${frames} frames, ${duration(seconds)} of exposure a field`
        + (scopes > 1 ? ` — about ${duration(seconds / scopes)} with ${scopes} `
          + 'telescopes' : '')
        + (as.dirty ? '  ·  unsaved' : '')
      : '—';
  }

  function fillFilterPicker(select) {
    const previous = select.value;
    select.innerHTML = '';
    const names = as.filters.length ? as.filters : [''];
    for (const name of names) {
      select.appendChild(new Option(name || 'no filter', name));
    }
    if (previous) select.value = previous;
  }

  /* --------------------------------------------------------------- loading */

  async function loadSurveys() {
    let payload;
    try {
      payload = await app.api('/api/allsky');
    } catch (error) {
      app.toast(error.message, 'error');
      return;
    }
    as.surveys = payload.surveys || [];
    as.settings = payload.settings || {};
    as.filters = payload.filters || [];

    const select = $('asSurvey');
    const previous = as.id;
    select.innerHTML = '';
    if (!as.surveys.length) {
      select.appendChild(new Option('no all-sky survey yet', ''));
    }
    for (const survey of as.surveys) {
      select.appendChild(new Option(
        `${survey.name} (${survey.summary.complete.toLocaleString()}/`
        + `${survey.summary.reachable.toLocaleString()})`, survey.id));
    }
    as.id = (as.surveys.find((s) => s.id === previous) || as.surveys[0] || {}).id
      || null;
    select.value = as.id || '';

    fillSettings(as.settings);
    fillFilterPicker($('asAddFilter'));
    fillFilterPicker($('asNewAddFilter'));
    const field = payload.field || {};
    $('asNewField').value = field.width
      ? `${field.width.toFixed(2)}° × ${field.height.toFixed(2)}°`
        + (field.limitedBy ? ` (${field.limitedBy})` : '')
      : 'not known — set the optics first';

    if (as.id) await loadGrid();
    else { as.grid = null; as.tonight = null; renderSummary(); drawChart(); }
  }

  async function loadGrid() {
    if (!as.id) return;
    try {
      as.grid = await app.api(`/api/allsky/${as.id}/grid`);
    } catch (error) {
      app.toast(error.message, 'error');
      return;
    }
    as.goal = JSON.parse(JSON.stringify(as.grid.allsky.goal || []));
    as.dirty = false;
    renderSummary();
    renderGoal();
    drawChart();
    loadTonight();
  }

  async function loadTonight() {
    if (!as.id) return;
    try {
      as.tonight = await app.api(`/api/allsky/${as.id}/tonight`);
    } catch (error) {
      as.tonight = { fields: [], detail: error.message, flips: null };
    }
    renderTonight();
    renderGoal();
    drawChart();
  }

  function fillSettings(settings) {
    $('asMinAlt').value = settings.minAltitude ?? 40;
    $('asQualityAlt').value = settings.qualityAltitude ?? 70;
    $('asMaxHa').value = settings.maxHourAngle ?? 3;
    $('asMoon').value = settings.moonAvoidance ?? 30;
    $('asSlew').value = settings.slewWeight ?? 2;
    $('asFinish').value = settings.finishBonus ?? 25;
    $('asVisit').value = settings.visitMinutes ?? 10;
    $('asFlips').checked = settings.minimiseFlips !== false;
    $('asMoonScale').checked = settings.moonScaleByPhase !== false;
  }

  /* --------------------------------------------------------------- actions */

  async function saveSettings() {
    try {
      await app.api('/api/allsky/settings', 'POST', {
        minAltitude: Number($('asMinAlt').value),
        qualityAltitude: Number($('asQualityAlt').value),
        maxHourAngle: Number($('asMaxHa').value),
        moonAvoidance: Number($('asMoon').value),
        slewWeight: Number($('asSlew').value),
        finishBonus: Number($('asFinish').value),
        visitMinutes: Number($('asVisit').value),
        minimiseFlips: $('asFlips').checked,
        moonScaleByPhase: $('asMoonScale').checked,
      });
    } catch (error) {
      app.toast(error.message, 'error');
      return;
    }
    app.toast('Saved', 'success');
    await loadSurveys();
  }

  async function saveGoal() {
    if (!as.id || !as.goal.length) {
      app.toast('Add at least one filter first', 'error');
      return;
    }
    try {
      await app.api(`/api/allsky/${as.id}/goal`, 'POST', { goal: as.goal });
    } catch (error) {
      app.toast(error.message, 'error');
      return;
    }
    as.dirty = false;
    app.toast('Saved — fields already finished keep what they have', 'success');
    await loadGrid();
  }

  async function preview() {
    const goal = readNewGoal();
    let result;
    try {
      result = await app.api('/api/allsky/preview', 'POST', {
        decMin: Number($('asNewDecMin').value),
        decMax: Number($('asNewDecMax').value),
        overlap: Number($('asNewOverlap').value) / 100,
        goal,
      });
    } catch (error) {
      $('asPreview').textContent = error.message;
      $('asPreview').classList.add('warn-text');
      return null;
    }
    $('asPreview').classList.remove('warn-text');
    const camera = result.camera || {};
    const lines = [
      `${result.fields.toLocaleString()} fields in ${result.rings} rings`,
      `${result.reachable.toLocaleString()} of them ever rise high enough here`
      + (result.unreachable ? `, ${result.unreachable.toLocaleString()} never do` : ''),
      `overlap ${(result.equatorOverlap * 100).toFixed(0)}% at the equator, `
      + `up to ${(result.poleOverlap * 100).toFixed(0)}% at the pole`,
    ];
    if (camera.width) {
      lines.push(`camera at ${Number(camera.rotation).toFixed(2)}°: tiling on `
        + `${camera.width.toFixed(2)}° × ${camera.height.toFixed(2)}° of the `
        + `${camera.sensorWidth.toFixed(2)}° × ${camera.sensorHeight.toFixed(2)}° `
        + 'sensor'
        + (camera.costFactor > 1.02
          ? ` — ${camera.costFactor.toFixed(2)}× the fields a squared-up camera `
            + 'would need' : ' — squared up, nothing wasted'));
    }
    if (result.perFieldSeconds) {
      lines.push(`${duration(result.perFieldSeconds)} a field on one telescope`
        + (result.telescopes > 1
          ? `, ${duration(result.perFieldWithRig)} on ${result.telescopes}` : ''));
      lines.push(`${duration(result.totalSeconds)} of clear sky to finish`);
    }
    $('asPreview').textContent = lines.join('\n');
    return result;
  }

  function readNewGoal() {
    return newGoal.map((item) => ({
      name: item.name, exposure: Number(item.exposure) || 1,
      count: Math.max(1, Math.round(Number(item.count) || 1)),
    }));
  }

  let newGoal = [];

  function renderNewGoal() {
    const host = $('asNewGoal');
    host.innerHTML = '';
    if (!newGoal.length) {
      host.innerHTML = '<p class="muted small">Add at least one filter.</p>';
    }
    newGoal.forEach((item, index) => host.appendChild(
      goalRow(item, index, newGoal, renderNewGoal)));
  }

  async function create() {
    const goal = readNewGoal();
    if (!goal.length) {
      app.toast('Add at least one filter first', 'error');
      return;
    }
    let result;
    try {
      result = await app.api('/api/allsky', 'POST', {
        name: $('asNewName').value.trim() || 'All-sky survey',
        decMin: Number($('asNewDecMin').value),
        decMax: Number($('asNewDecMax').value),
        overlap: Number($('asNewOverlap').value) / 100,
        goal,
      });
    } catch (error) {
      app.toast(error.message, 'error');
      return;
    }
    $('allskyDialog').close();
    app.toast(`${result.target.name}: ${result.shape.fields.toLocaleString()} `
      + 'fields', 'success');
    as.id = result.target.id;
    await loadSurveys();
  }

  /* ---------------------------------------------------------------- wiring */

  function bind() {
    $('asSurvey').addEventListener('change', () => {
      as.id = $('asSurvey').value || null;
      as.selected = null;
      $('asField').hidden = true;
      loadGrid();
    });

    $('btnAsNew').addEventListener('click', () => {
      newGoal = as.goal.length
        ? JSON.parse(JSON.stringify(as.goal))
        : [{ name: as.filters[0] || '', exposure: 60, count: 6 }];
      renderNewGoal();
      $('asPreview').textContent = 'Press "Work it out" to see what that comes to.';
      if (!$('allskyDialog').open) $('allskyDialog').showModal();
      preview();
    });
    $('btnAsPreview').addEventListener('click', preview);
    $('btnAsCreate').addEventListener('click', create);
    $('btnAsNewAddFilter').addEventListener('click', () => {
      newGoal.push({ name: $('asNewAddFilter').value, exposure: 60, count: 6 });
      renderNewGoal();
    });
    for (const id of ['asNewDecMin', 'asNewDecMax', 'asNewOverlap']) {
      $(id).addEventListener('change', preview);
    }

    $('btnAsAddFilter').addEventListener('click', () => {
      as.goal.push({ name: $('asAddFilter').value, exposure: 60, count: 6 });
      as.dirty = true;
      renderGoal();
    });
    $('btnAsSaveGoal').addEventListener('click', saveGoal);
    $('btnAsSaveSettings').addEventListener('click', saveSettings);

    $('btnAsToPlan').addEventListener('click', async () => {
      if (!as.id) return;
      try {
        await app.api('/api/plan/entries', 'POST', { targetId: as.id });
      } catch (error) {
        app.toast(error.message, 'error');
        return;
      }
      app.toast('Added — set when it runs on the Plan tab', 'success');
      app.showTab('plan');
    });

    $('btnAsDelete').addEventListener('click', async () => {
      if (!as.id) return;
      const survey = as.surveys.find((s) => s.id === as.id);
      const ok = await app.confirmAction(
        `Delete ${survey ? survey.name : 'this survey'} and its record of what `
        + 'has been shot? The frames on disk are not touched, but the progress '
        + 'against them is lost.',
        { title: 'Delete survey', confirmLabel: 'Delete', danger: true });
      if (!ok) return;
      await app.send(`/api/allsky/${as.id}`, 'DELETE', null, 'Survey deleted');
      as.id = null;
      await loadSurveys();
    });

    $('asShowTonight').addEventListener('change', drawChart);
    $('asShowReach').addEventListener('change', drawChart);
    $('asShowSky').addEventListener('change', drawChart);

    $('btnAsFrameTonight').addEventListener('click', frameTonight);
    $('btnAsZoomIn').addEventListener('click', () => setZoom(as.zoom * 1.5));
    $('btnAsZoomOut').addEventListener('click', () => setZoom(as.zoom / 1.5));
    $('btnAsZoomReset').addEventListener('click', () => {
      as.zoom = 1;
      as.panX = 0;
      as.panY = 0;
      setText('asZoomLabel', '1.0×');
      drawChart();
    });

    const canvas = $('asChart');
    canvas.addEventListener('wheel', (event) => {
      event.preventDefault();
      const box = canvas.getBoundingClientRect();
      setZoom(as.zoom * (event.deltaY < 0 ? 1.18 : 1 / 1.18),
        event.clientX - box.left, event.clientY - box.top);
    }, { passive: false });

    let dragging = null;
    canvas.addEventListener('mousedown', (event) => {
      dragging = { x: event.clientX, y: event.clientY,
        panX: as.panX, panY: as.panY, moved: false };
      canvas.style.cursor = 'grabbing';
    });
    window.addEventListener('mousemove', (event) => {
      if (!dragging) return;
      const dx = event.clientX - dragging.x;
      const dy = event.clientY - dragging.y;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragging.moved = true;
      as.panX = dragging.panX + dx;
      as.panY = dragging.panY + dy;
      clampPan(canvas.getBoundingClientRect());
      drawChart();
    });
    window.addEventListener('mouseup', () => {
      if (dragging) canvas.style.cursor = '';
      // A drag that moved is a pan, not a click on whatever it finished over.
      setTimeout(() => { dragging = null; }, 0);
    });

    canvas.addEventListener('mousemove', (event) => {
      if (dragging) return;
      const i = fieldAt(event);
      if (i === as.hover) return;
      as.hover = i;
      const grid = as.grid;
      if (i < 0 || !grid) { $('asHover').textContent = '—'; return; }
      const where = constellationAt(grid.ra[i], grid.dec[i]);
      $('asHover').textContent =
        `${grid.ids[i]}   RA ${app.fmtHours(grid.ra[i])}   `
        + `Dec ${app.fmtDegrees(grid.dec[i])}`
        + (where ? `   in ${where}` : '')
        + `   ${(grid.fraction[i] * 100).toFixed(0)}% done`
        + (grid.reachable[i] ? '' : '   (never rises high enough here)');
    });
    canvas.addEventListener('click', (event) => {
      if (dragging && dragging.moved) return;
      const i = fieldAt(event);
      if (i >= 0 && as.grid) selectField(as.grid.ids[i]);
    });
    window.addEventListener('resize', () => { if (as.grid) drawChart(); });
  }

  function init() {
    bind();
    loadSky().then(() => { if (as.grid) drawChart(); });
    let refreshed = 0;
    app.onStatus((status, tab) => {
      if (tab !== 'allsky') return;
      if (!as.surveys.length && !as.id) { loadSurveys(); return; }
      // A survey being shot is changing under the tab; a survey that is not
      // needs no polling at all.
      const running = status && status.sequence && status.sequence.running;
      if (running && Date.now() - refreshed > 20000) {
        refreshed = Date.now();
        loadGrid();
      }
      drawChart();
    });
    loadSurveys();
  }

  document.addEventListener('DOMContentLoaded', init);
}());
