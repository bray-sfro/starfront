/* Starfront planetarium — a star chart drawn from a local catalogue.
 *
 * This renders the sky itself rather than streaming survey imagery: 8404 stars
 * from the Yale Bright Star Catalogue plus our deep-sky list, projected
 * stereographically the way Cartes du Ciel draws a chart.  Everything is local,
 * so it opens instantly and works with the network unplugged — which is the
 * normal state of a capture PC at a dark site.
 *
 * Angles are degrees throughout; RA is degrees internally and converted to
 * hours only when talking to the API, which is the one place hours are used.
 */
'use strict';

(function () {
  const STAR_CATALOG = 'vendor/stars.json?v=2';
  // The Milky Way as photographed: ESO/S. Brunier's panorama (CC BY 4.0),
  // reduced to half a degree a pixel with the stars taken out, in galactic
  // coordinates with the centre in the middle and longitude to the left.
  const MILKY_WAY = 'vendor/milkyway.png?v=1';
  const DEG = Math.PI / 180;
  const app = window.astro;
  const $ = app.$;

  const sky = {
    canvas: null,
    ctx: null,
    width: 0,
    height: 0,
    ready: false,
    dirty: true,

    centre: { ra: 0, dec: 45 },   // where the chart is looking, degrees
    fov: 60,                      // degrees across the smaller screen dimension
    scale: 1,                     // pixels per projected unit, from fov

    stars: [],                    // [ra, dec, mag, bv, proper, designation]
    deepSky: [],
    site: null,                   // { latitude, longitude }
    scope: null,                  // { ra, dec, slewing }
    frame: null,                  // { width, height, rotation } camera field
    selected: null,
    timeOffset: 0,                // seconds from now, for the time controls

    show: {
      horizon: true, grid: false, eqGrid: false, meridian: true,
      labels: true, deepSky: true, solar: true, scope: true, fov: true,
      others: true,
    },
    others: [],                   // the collaboration's other telescopes
    othersKey: '',
  };

  /* ------------------------------------------------------------ time & site */

  const now = () => new Date(Date.now() + sky.timeOffset * 1000);
  const julianDate = () => now().getTime() / 86400000.0 + 2440587.5;

  function lstDegrees() {
    if (!sky.site) return 0;
    const jd = julianDate();
    const t = (jd - 2451545.0) / 36525.0;
    let gmst = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
      + 0.000387933 * t * t - t * t * t / 38710000.0) % 360.0;
    if (gmst < 0) gmst += 360;
    return ((gmst + sky.site.longitude) % 360 + 360) % 360;
  }

  function altAz(raDeg, decDeg) {
    if (!sky.site) return null;
    const hourAngle = (lstDegrees() - raDeg) * DEG;
    const dec = decDeg * DEG;
    const lat = sky.site.latitude * DEG;
    const sinAlt = Math.sin(dec) * Math.sin(lat)
      + Math.cos(dec) * Math.cos(lat) * Math.cos(hourAngle);
    const altitude = Math.asin(Math.max(-1, Math.min(1, sinAlt)));
    const cosAlt = Math.cos(altitude);
    if (cosAlt < 1e-9) return { altitude: altitude / DEG, azimuth: 0 };
    const sinAz = -Math.cos(dec) * Math.sin(hourAngle) / cosAlt;
    const cosAz = (Math.sin(dec) - sinAlt * Math.sin(lat)) / (cosAlt * Math.cos(lat));
    return {
      altitude: altitude / DEG,
      azimuth: ((Math.atan2(sinAz, cosAz) / DEG) % 360 + 360) % 360,
    };
  }

  const altitudeOf = (ra, dec) => {
    const result = altAz(ra, dec);
    return result === null ? null : result.altitude;
  };

  function altAzToRaDec(altDeg, azDeg) {
    const lst = lstDegrees();
    const alt = altDeg * DEG, az = azDeg * DEG, lat = sky.site.latitude * DEG;
    const sinDec = Math.sin(alt) * Math.sin(lat) + Math.cos(alt) * Math.cos(lat) * Math.cos(az);
    const dec = Math.asin(Math.max(-1, Math.min(1, sinDec)));
    const cosDec = Math.cos(dec);
    if (cosDec < 1e-9) return [lst, dec / DEG];
    const cosHa = (Math.sin(alt) - Math.sin(lat) * sinDec) / (Math.cos(lat) * cosDec);
    let hourAngle = Math.acos(Math.max(-1, Math.min(1, cosHa)));
    if (Math.sin(az) > 0) hourAngle = 2 * Math.PI - hourAngle;
    return [((lst - hourAngle / DEG) % 360 + 360) % 360, dec / DEG];
  }

  const raHours = (raDeg) => ((raDeg / 15) % 24 + 24) % 24;
  const raDegrees = (hours) => ((hours * 15) % 360 + 360) % 360;

  /* ----------------------------------------------------------- projection */

  /* Stereographic, north up. It is conformal — star patterns keep their shape
     right out to the edge — which is why charts have used it for centuries. */
  const view = { sinDec: 0, cosDec: 1, ra: 0 };

  function updateProjection() {
    view.ra = sky.centre.ra * DEG;
    view.sinDec = Math.sin(sky.centre.dec * DEG);
    view.cosDec = Math.cos(sky.centre.dec * DEG);
    const half = Math.min(sky.width, sky.height) / 2;
    sky.scale = half / (2 * Math.tan(Math.min(sky.fov, 200) * DEG / 4));
  }

  /** Sky to screen. Returns null for anything behind the projection point. */
  function project(raDeg, decDeg) {
    const dRa = raDeg * DEG - view.ra;
    const dec = decDeg * DEG;
    const sinD = Math.sin(dec), cosD = Math.cos(dec);
    const cosDra = Math.cos(dRa);
    const cosC = view.sinDec * sinD + view.cosDec * cosD * cosDra;
    if (cosC < -0.92) return null;
    const k = 2 / (1 + cosC);
    return [
      sky.width / 2 + k * cosD * Math.sin(dRa) * sky.scale,
      sky.height / 2 - k * (view.cosDec * sinD - view.sinDec * cosD * cosDra) * sky.scale,
    ];
  }

  /** Screen to sky. */
  function unproject(px, py) {
    const x = (px - sky.width / 2) / sky.scale;
    const y = -(py - sky.height / 2) / sky.scale;
    const r = Math.hypot(x, y);
    if (r < 1e-9) return [sky.centre.ra, sky.centre.dec];
    const c = 2 * Math.atan(r / 2);
    const sinC = Math.sin(c), cosC = Math.cos(c);
    const dec = Math.asin(cosC * view.sinDec + y * sinC * view.cosDec / r);
    const ra = view.ra + Math.atan2(
      x * sinC, r * view.cosDec * cosC - y * view.sinDec * sinC);
    return [((ra / DEG) % 360 + 360) % 360, dec / DEG];
  }

  const pixelsPerDegree = () => sky.scale * DEG;

  /* --------------------------------------------------- Sun, Moon, planets */

  function sunPosition(jd) {
    const n = jd - 2451545.0;
    const meanLongitude = (280.460 + 0.9856474 * n) % 360;
    const anomaly = ((357.528 + 0.9856003 * n) % 360) * DEG;
    const lambda = (meanLongitude + 1.915 * Math.sin(anomaly)
      + 0.020 * Math.sin(2 * anomaly)) * DEG;
    const obliquity = 23.4393 * DEG;
    let ra = Math.atan2(Math.cos(obliquity) * Math.sin(lambda), Math.cos(lambda)) / DEG;
    if (ra < 0) ra += 360;
    return [ra, Math.asin(Math.sin(obliquity) * Math.sin(lambda)) / DEG];
  }

  function moonPosition(jd) {
    const n = jd - 2451545.0;
    const meanLongitude = (218.316 + 13.176396 * n) % 360;
    const meanAnomaly = ((134.963 + 13.064993 * n) % 360) * DEG;
    const argLatitude = ((93.272 + 13.229350 * n) % 360) * DEG;
    const sunAnomaly = ((357.528 + 0.985600 * n) % 360) * DEG;
    const lambda = (meanLongitude + 6.289 * Math.sin(meanAnomaly)
      - 1.274 * Math.sin(2 * argLatitude - meanAnomaly)
      + 0.658 * Math.sin(2 * argLatitude) - 0.214 * Math.sin(2 * meanAnomaly)
      - 0.186 * Math.sin(sunAnomaly)) * DEG;
    const beta = (5.128 * Math.sin(argLatitude)
      + 0.281 * Math.sin(meanAnomaly + argLatitude)
      - 0.277 * Math.sin(meanAnomaly - argLatitude)) * DEG;
    const obliquity = 23.4393 * DEG;
    const sinDec = Math.sin(beta) * Math.cos(obliquity)
      + Math.cos(beta) * Math.sin(obliquity) * Math.sin(lambda);
    const dec = Math.asin(Math.max(-1, Math.min(1, sinDec))) / DEG;
    const cosDec = Math.cos(dec * DEG);
    if (cosDec < 1e-9) return [0, dec];
    const sinRa = (Math.cos(beta) * Math.cos(obliquity) * Math.sin(lambda)
      - Math.sin(beta) * Math.sin(obliquity)) / cosDec;
    const cosRa = Math.cos(beta) * Math.cos(lambda) / cosDec;
    let ra = Math.atan2(sinRa, cosRa) / DEG;
    if (ra < 0) ra += 360;
    return [ra, dec];
  }

  const moonPhase = (jd) => ((((jd - 2451550.1) % 29.53059) + 29.53059) % 29.53059) / 29.53059;

  // name, colour, size, L0, dL/century, a, e, inclination, longitude of
  // perihelion, longitude of ascending node, dNode/century.  Meeus, low precision.
  const PLANET_ELEMENTS = [
    ['Mercury', 'rgba(200, 185, 165, 0.95)', 3, 252.250906, 149472.6746358, 0.387098, 0.205630, 7.004979, 77.455863, 48.330893, 1.1861368],
    ['Venus', 'rgba(255, 240, 205, 0.95)', 5, 181.979801, 58517.8156760, 0.723332, 0.006772, 3.394662, 131.563703, 76.679920, 0.9011190],
    ['Mars', 'rgba(240, 150, 110, 0.95)', 4, 355.433000, 19140.2993313, 1.523679, 0.093400, 1.849691, 336.060234, 49.558093, 0.7720959],
    ['Jupiter', 'rgba(240, 225, 190, 0.95)', 6, 34.351519, 3034.9056606, 5.202603, 0.048498, 1.303267, 14.331168, 100.464407, 1.0209774],
    ['Saturn', 'rgba(235, 215, 165, 0.95)', 5, 50.077444, 1222.1138488, 9.537070, 0.055546, 2.488879, 93.057237, 113.665524, 0.8770979],
    ['Uranus', 'rgba(180, 220, 230, 0.95)', 4, 314.055005, 428.4669983, 19.18916, 0.046381, 0.773196, 173.005159, 74.005957, 0.5211278],
    ['Neptune', 'rgba(150, 175, 235, 0.95)', 4, 304.348665, 218.4862002, 30.06992, 0.009456, 1.769952, 48.123691, 131.784057, 1.1022039],
  ];

  function planetPositions(jd) {
    const t = (jd - 2451545.0) / 36525.0;
    const wrap = (x) => ((x % 360) + 360) % 360;
    const sin = (x) => Math.sin(x * DEG);
    const cos = (x) => Math.cos(x * DEG);

    const earthAnomaly = wrap(357.5291092 + 35999.0502909 * t);
    const earthEccentricity = 0.016708634 - 0.000042037 * t;
    const centre = (1.9146 - 0.004817 * t) * sin(earthAnomaly)
      + (0.019993 - 0.000101 * t) * sin(2 * earthAnomaly) + 0.00029 * sin(3 * earthAnomaly);
    const earthRadius = 1.000001018 * (1 - earthEccentricity ** 2)
      / (1 + earthEccentricity * cos(earthAnomaly + centre));
    const earthLongitude = wrap(100.4664568 + 35999.3728565 * t + centre);
    const earthX = earthRadius * cos(earthLongitude);
    const earthY = earthRadius * sin(earthLongitude);
    const obliquity = (23.439291111 - 0.013004167 * t) * DEG;

    return PLANET_ELEMENTS.map(([name, colour, size, l0, dl, a, e, inc, peri, node, dNode]) => {
      const meanLongitude = wrap(l0 + dl * t);
      const inclination = inc * DEG;
      const perihelion = wrap(peri) * DEG;
      const ascending = wrap(node + dNode * t) * DEG;
      const meanAnomaly = wrap(meanLongitude - peri);

      // Kepler's equation by Newton-Raphson; converges in a handful of steps
      // for every eccentricity in this table.
      let eccentric = meanAnomaly * DEG;
      for (let i = 0; i < 12; i += 1) {
        eccentric -= (eccentric - e * Math.sin(eccentric) - meanAnomaly * DEG)
          / (1 - e * Math.cos(eccentric));
      }
      const trueAnomaly = 2 * Math.atan2(
        Math.sqrt(1 + e) * Math.sin(eccentric / 2),
        Math.sqrt(1 - e) * Math.cos(eccentric / 2));
      const radius = a * (1 - e * Math.cos(eccentric));
      const argument = trueAnomaly + perihelion - ascending;

      const x = radius * (Math.cos(ascending) * Math.cos(argument)
        - Math.sin(ascending) * Math.sin(argument) * Math.cos(inclination)) - earthX;
      const y = radius * (Math.sin(ascending) * Math.cos(argument)
        + Math.cos(ascending) * Math.sin(argument) * Math.cos(inclination)) - earthY;
      const z = radius * Math.sin(argument) * Math.sin(inclination);

      const yEq = y * Math.cos(obliquity) - z * Math.sin(obliquity);
      const zEq = y * Math.sin(obliquity) + z * Math.cos(obliquity);
      let ra = Math.atan2(yEq, x) / DEG;
      if (ra < 0) ra += 360;
      return { name, colour, size, ra, dec: Math.atan2(zEq, Math.hypot(x, yEq)) / DEG };
    });
  }

  /* ------------------------------------------------------------- rendering */

  /** How faint to draw, given how much sky is on screen.  Roughly matches what
      a printed chart shows at the same scale: naked-eye stars across a whole
      constellation, everything the catalogue has once you are down to a
      telescope field. */
  function limitingMagnitude() {
    return Math.max(4.2, Math.min(6.5, 5.4 + 1.6 * Math.log2(60 / sky.fov)));
  }

  /* B-V to an RGB tint. Real star colours are pastel — the eye sees Rigel as
     barely blue and Betelgeuse as amber, never orange — so these sit close to
     white and only lean one way or the other. Returned as [r, g, b] so the
     glow can be built from them at any alpha. */
  function starTint(bv) {
    const b = Math.max(-0.4, Math.min(2.0, Number.isFinite(bv) ? bv : 0.6));
    // Piecewise between anchors: O/B blue-white, A white, G yellow-white,
    // K pale amber, M amber.
    const stops = [
      [-0.4, [190, 210, 255]], [0.0, [215, 228, 255]], [0.3, [245, 246, 255]],
      [0.6, [255, 248, 235]], [1.0, [255, 236, 205]], [1.5, [255, 220, 175]],
      [2.0, [255, 205, 150]],
    ];
    let i = 0;
    while (i < stops.length - 2 && b > stops[i + 1][0]) i += 1;
    const [b0, c0] = stops[i], [b1, c1] = stops[i + 1];
    const t = Math.max(0, Math.min(1, (b - b0) / (b1 - b0)));
    return c0.map((v, k) => Math.round(v + (c1[k] - v) * t));
  }

  const rgba = (c, a) => `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${a})`;

  /* A star as a point of light rather than a disc: a bright core with a
     falloff, drawn as a radial gradient. The core stays small however bright
     the star; brightness reads as the size and softness of the glow around
     it, which is how film and the eye both render it. */
  function drawStarGlyph(ctx, x, y, mag, limit, tint) {
    const above = Math.max(0, limit - mag);      // how far above the limit
    // Core radius grows slowly; glow radius grows faster.
    const core = Math.min(3.4, 1.0 + above * 0.3);
    const glow = Math.min(14, core + 1.5 + above * 1.1);
    const alpha = Math.min(1, 0.7 + above * 0.1);

    const halo = ctx.createRadialGradient(x, y, 0, x, y, glow);
    halo.addColorStop(0, rgba(tint, alpha));
    halo.addColorStop(0.28, rgba(tint, alpha * 0.45));
    halo.addColorStop(1, rgba(tint, 0));
    ctx.fillStyle = halo;
    ctx.beginPath();
    ctx.arc(x, y, glow, 0, Math.PI * 2);
    ctx.fill();
    // The core, pushed toward white so even a red giant has a hot centre.
    const hot = tint.map((v) => Math.round(v + (255 - v) * 0.65));
    ctx.fillStyle = rgba(hot, 1);
    ctx.beginPath();
    ctx.arc(x, y, core, 0, Math.PI * 2);
    ctx.fill();
  }

  const labelBoxes = [];

  const LABEL_FONT = '11px "Segoe UI", system-ui, sans-serif';
  const LABEL_FONT_SMALL = '10px "Segoe UI", system-ui, sans-serif';

  function placeLabel(text, x, y, colour, font = LABEL_FONT) {
    const ctx = sky.ctx;
    ctx.font = font;
    const width = ctx.measureText(text).width;
    const box = { x, y: y - 9, w: width, h: 12 };
    for (const other of labelBoxes) {
      if (box.x < other.x + other.w && box.x + box.w > other.x
        && box.y < other.y + other.h && box.y + box.h > other.y) return false;
    }
    labelBoxes.push(box);
    ctx.fillStyle = colour;
    // A thin dark edge rather than a heavy blur, so the text sits on the
    // sky instead of floating in a smudge.
    ctx.shadowColor = 'rgba(0, 0, 8, 0.9)';
    ctx.shadowBlur = 2;
    ctx.textAlign = 'left';
    ctx.fillText(text, x, y);
    ctx.shadowBlur = 0;
    return true;
  }

  /* ------------------------------------------------------------ the sky */

  /* Galactic coordinates from J2000 equatorial, for the Milky Way. */
  const GAL_POLE_RA = 192.85948 * DEG;
  const GAL_POLE_DEC = 27.12825 * DEG;
  const GAL_NODE = 122.932 * DEG;

  function galacticLatitude(raDeg, decDeg) {
    const ra = raDeg * DEG, dec = decDeg * DEG;
    const sinB = Math.sin(dec) * Math.sin(GAL_POLE_DEC)
      + Math.cos(dec) * Math.cos(GAL_POLE_DEC) * Math.cos(ra - GAL_POLE_RA);
    return Math.asin(Math.max(-1, Math.min(1, sinB))) / DEG;
  }

  function galacticLongitude(raDeg, decDeg) {
    const ra = raDeg * DEG, dec = decDeg * DEG;
    const y = Math.cos(dec) * Math.sin(ra - GAL_POLE_RA);
    const x = Math.sin(dec) * Math.cos(GAL_POLE_DEC)
      - Math.cos(dec) * Math.sin(GAL_POLE_DEC) * Math.cos(ra - GAL_POLE_RA);
    return (((GAL_NODE - Math.atan2(y, x)) / DEG) % 360 + 360) % 360;
  }

  /* How bright the sky is, 0 (astronomical dark) to 1 (daylight), from the
     Sun's altitude. Drives the background: a chart at noon is not black. */
  function skyBrightness() {
    if (!sky.site) return 0;
    const sun = sunPosition(julianDate());
    const altitude = altitudeOf(sun[0], sun[1]);
    if (altitude === null) return 0;
    // -18 dark, -6 civil twilight, 0 sunrise, +8 full day.
    return Math.max(0, Math.min(1, (altitude + 18) / 26));
  }

  /* The Milky Way map, once loaded: pixels of `MILKY_WAY` and its size. */
  const milkyWay = { data: null, width: 0, height: 0 };

  function loadMilkyWay() {
    const image = new Image();
    image.onload = () => {
      const canvas = document.createElement('canvas');
      canvas.width = image.width;
      canvas.height = image.height;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(image, 0, 0);
      milkyWay.data = ctx.getImageData(0, 0, image.width, image.height).data;
      milkyWay.width = image.width;
      milkyWay.height = image.height;
      if (sky.ready) draw();
    };
    // Without it the chart still draws; the sky is just plain.
    image.onerror = () => {};
    image.src = MILKY_WAY;
  }

  /* The map sampled at a galactic longitude and latitude, bilinear, as
     [r, g, b] 0-255. Longitude runs leftward across the map, the centre in
     the middle, and wraps. */
  function milkyWayAt(lon, lat) {
    const { data, width, height } = milkyWay;
    const fx = ((((180 - lon) % 360) + 360) % 360) / 360 * width;
    const fy = Math.max(0, Math.min(height - 1.001, (90 - lat) / 180 * height));
    const x0 = Math.floor(fx) % width, y0 = Math.floor(fy);
    const x1 = (x0 + 1) % width, y1 = Math.min(height - 1, y0 + 1);
    const tx = fx - Math.floor(fx), ty = fy - y0;
    const at = (x, y, c) => data[(y * width + x) * 4 + c];
    const out = [0, 0, 0];
    for (let c = 0; c < 3; c += 1) {
      out[c] = at(x0, y0, c) * (1 - tx) * (1 - ty) + at(x1, y0, c) * tx * (1 - ty)
        + at(x0, y1, c) * (1 - tx) * ty + at(x1, y1, c) * tx * ty;
    }
    return out;
  }

  /* The background: a deep blue-black that lifts toward the horizon, warms
     into twilight as the Sun comes up, and carries the Milky Way as it was
     photographed. Drawn coarsely on a small offscreen canvas and scaled up,
     so it costs a few thousand samples rather than a million, and the blur
     comes free. */
  const backdrop = { canvas: null, ctx: null };

  function drawBackground() {
    const ctx = sky.ctx;
    const bright = skyBrightness();

    // Base: night is nearly black with a blue cast; day is a pale blue.
    const night = [4, 6, 16], day = [92, 130, 190];
    const base = night.map((v, i) => Math.round(v + (day[i] - v) * bright));
    ctx.fillStyle = rgba(base, 1);
    ctx.fillRect(0, 0, sky.width, sky.height);

    if (bright > 0.85) return;                 // daylight washes everything out

    // Finer cells when zoomed in, where a degree is many pixels and the
    // dust lanes have to hold their shape; coarser when the whole sky is
    // on screen and a cell is a degree anyway.
    const cell = sky.fov < 40 ? 6 : 10;         // backdrop resolution, px
    const cols = Math.ceil(sky.width / cell), rows = Math.ceil(sky.height / cell);
    // A hidden tab has a zero-sized stage, and a zero-sized image data
    // throws rather than drawing nothing.
    if (cols < 1 || rows < 1) return;
    if (!backdrop.canvas) {
      backdrop.canvas = document.createElement('canvas');
      backdrop.ctx = backdrop.canvas.getContext('2d');
    }
    backdrop.canvas.width = cols;
    backdrop.canvas.height = rows;
    const image = backdrop.ctx.createImageData(cols, rows);
    const data = image.data;
    const dim = 1 - bright;

    for (let j = 0; j < rows; j += 1) {
      for (let i = 0; i < cols; i += 1) {
        const px = (i + 0.5) * cell, py = (j + 0.5) * cell;
        const pos = unproject(px, py);
        let r = 0, g = 0, b = 0, a = 0;
        if (pos) {
          const lat = galacticLatitude(pos[0], pos[1]);
          const lon = galacticLongitude(pos[0], pos[1]);
          if (milkyWay.data) {
            // The photograph: its own colour, its own dust lanes, the
            // Magellanic Clouds and Andromeda where they are. Brightness
            // becomes opacity over the sky colour, kept short of the point
            // where the bulge would compete with the stars drawn over it.
            const [mr, mg, mb] = milkyWayAt(lon, lat);
            const lum = (0.3 * mr + 0.5 * mg + 0.2 * mb) / 255;
            // Lift the colour a little toward the map's own tint, so the
            // bulge stays warm and the arms stay blue-grey when faint.
            const boost = 1 / Math.max(0.35, lum + 0.2);
            r = Math.min(255, mr * boost * 0.85);
            g = Math.min(255, mg * boost * 0.85);
            b = Math.min(255, mb * boost * 0.9);
            a = Math.pow(lum, 0.9) * 0.62 * dim;
          } else {
            // No map: a cosine band along the plane stands in.
            const centre = Math.cos(lon * DEG);
            const width = 5 + 4 * (centre + 1);
            const band = Math.exp(-(lat * lat) / (2 * width * width));
            r = 165; g = 175; b = 205;
            a = band * (0.45 + 0.55 * (centre + 1) / 2) * 0.34 * dim;
          }
          // A faint lift toward the horizon from airglow and skyglow.
          if (sky.site) {
            const altitude = altitudeOf(pos[0], pos[1]);
            if (altitude !== null && altitude < 25 && altitude > -1) {
              const t = (25 - altitude) / 25;
              const glow = t * t * 0.07 * dim;
              const mix = glow / (glow + a + 1e-6);
              r = Math.round(r * (1 - mix) + 120 * mix);
              g = Math.round(g * (1 - mix) + 110 * mix);
              b = Math.round(b * (1 - mix) + 130 * mix);
              a += glow;
            }
          }
        }
        const k = (j * cols + i) * 4;
        data[k] = r; data[k + 1] = g; data[k + 2] = b;
        data[k + 3] = Math.round(Math.min(1, a) * 255);
      }
    }
    backdrop.ctx.putImageData(image, 0, 0);
    ctx.save();
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(backdrop.canvas, 0, 0, cols, rows, 0, 0, cols * cell, rows * cell);
    ctx.restore();

    // Twilight: a warm band along the horizon nearest the Sun.
    if (bright > 0.05 && sky.site) {
      const sun = sunPosition(julianDate());
      const sunAz = altAz(sun[0], sun[1]).azimuth;
      const point = project(...altAzToRaDec(2, sunAz));
      if (point) {
        const radius = Math.max(sky.width, sky.height) * 0.9;
        const warm = ctx.createRadialGradient(point[0], point[1], 0, point[0], point[1], radius);
        warm.addColorStop(0, `rgba(255, 150, 80, ${0.45 * bright})`);
        warm.addColorStop(0.35, `rgba(200, 120, 110, ${0.18 * bright})`);
        warm.addColorStop(1, 'rgba(60, 60, 120, 0)');
        ctx.fillStyle = warm;
        ctx.fillRect(0, 0, sky.width, sky.height);
      }
    }
  }

  function draw() {
    if (!sky.ready) return;
    const ctx = sky.ctx;
    updateProjection();
    labelBoxes.length = 0;

    ctx.clearRect(0, 0, sky.width, sky.height);
    drawBackground();

    if (sky.show.eqGrid) drawEquatorialGrid();
    if (sky.site && sky.show.grid) drawAltAzGrid();
    if (sky.site && sky.show.meridian) drawMeridian();

    drawStars();
    if (sky.show.deepSky) drawDeepSky();
    if (sky.show.solar) drawSolarSystem();
    if (sky.site && sky.show.horizon) drawHorizon();
    if (sky.selected) drawSelection();
    if (sky.show.others && sky.others.length) drawOthers();
    if (sky.show.scope && sky.scope) drawTelescope();

    $('skyFov').textContent = sky.fov >= 1
      ? `${sky.fov.toFixed(sky.fov < 10 ? 1 : 0)}°`
      : `${(sky.fov * 60).toFixed(0)}′`;
  }

  function drawStars() {
    const ctx = sky.ctx;
    const limit = limitingMagnitude();
    const nameLimit = limit - 2.6;
    const showDesignations = sky.fov < 22;
    const margin = 30;

    for (const [ra, dec, mag, bv, proper, designation] of sky.stars) {
      if (mag > limit) break;                 // catalogue is sorted brightest first
      const point = project(ra, dec);
      if (!point) continue;
      const [x, y] = point;
      if (x < -margin || x > sky.width + margin
        || y < -margin || y > sky.height + margin) continue;

      const tint = starTint(bv);
      drawStarGlyph(ctx, x, y, mag, limit, tint);
      const radius = Math.min(2.6, 0.55 + (limit - mag) * 0.19) + 1.5;

      if (!sky.show.labels) continue;
      if (proper && mag <= nameLimit) {
        placeLabel(proper, x + radius + 4, y + 4, 'rgba(205, 216, 235, 0.82)');
      } else if (showDesignations && designation && mag <= limit - 1.0) {
        placeLabel(greekDesignation(designation), x + radius + 4, y + 4,
          'rgba(150, 165, 190, 0.7)', LABEL_FONT_SMALL);
      }
    }
  }

  /* "Alp Ori" as a chart writes it: "α Ori". */
  const GREEK = {
    Alp: 'α', Bet: 'β', Gam: 'γ', Del: 'δ', Eps: 'ε', Zet: 'ζ', Eta: 'η', The: 'θ',
    Iot: 'ι', Kap: 'κ', Lam: 'λ', Mu: 'μ', Nu: 'ν', Xi: 'ξ', Omi: 'ο', Pi: 'π',
    Rho: 'ρ', Sig: 'σ', Tau: 'τ', Ups: 'υ', Phi: 'φ', Chi: 'χ', Psi: 'ψ', Ome: 'ω',
  };

  function greekDesignation(text) {
    const match = /^([A-Z][a-z]+)(\d?)\s+(\w+)$/.exec(text || '');
    if (!match || !GREEK[match[1]]) return text;
    const index = match[2] ? String.fromCharCode(0x2070 + Number(match[2])) : '';
    return `${GREEK[match[1]]}${index === 'ⁱ' ? '¹' : index} ${match[3]}`;
  }

  /* Muted, near-monochrome: the symbol says what the object is, the colour
     only hints. Saturated purple galaxies and green nebulae were most of
     what made the chart look like a cartoon. */
  const DEEP_SKY_COLOUR = {
    galaxy: 'rgba(214, 190, 230, 0.75)',
    'globular cluster': 'rgba(235, 215, 170, 0.8)',
    'open cluster': 'rgba(230, 225, 190, 0.75)',
    'emission nebula': 'rgba(190, 220, 200, 0.75)',
    'reflection nebula': 'rgba(190, 210, 230, 0.75)',
    'dark nebula': 'rgba(160, 150, 140, 0.7)',
    'planetary nebula': 'rgba(180, 225, 225, 0.8)',
    'supernova remnant': 'rgba(225, 190, 180, 0.75)',
  };

  function drawDeepSky() {
    const ctx = sky.ctx;
    const perDegree = pixelsPerDegree();

    for (const object of sky.deepSky) {
      const point = project(object.ra, object.dec);
      if (!point) continue;
      const [x, y] = point;
      if (x < -40 || x > sky.width + 40 || y < -40 || y > sky.height + 40) continue;

      const colour = DEEP_SKY_COLOUR[object.type] || 'rgba(170, 185, 205, 0.75)';
      // Draw objects at their true angular size once that is big enough to see.
      const radius = Math.max(4, Math.min(240, (object.size || 5) / 60 * perDegree / 2));
      ctx.strokeStyle = colour;
      ctx.lineWidth = 0.9;
      ctx.setLineDash([]);

      // A whisper of fill on the extended objects, so a nebula reads as a
      // patch of something rather than an empty box.
      const wash = colour.replace(/[\d.]+\)$/, '0.07)');

      switch (object.type) {
        case 'galaxy':
          ctx.beginPath();
          ctx.ellipse(x, y, radius, radius * 0.45, -Math.PI / 6, 0, Math.PI * 2);
          ctx.fillStyle = wash;
          ctx.fill();
          ctx.stroke();
          break;
        case 'globular cluster':
          ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(x - radius, y); ctx.lineTo(x + radius, y);
          ctx.moveTo(x, y - radius); ctx.lineTo(x, y + radius);
          ctx.stroke();
          break;
        case 'open cluster':
          ctx.setLineDash([3, 3]);
          ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.stroke();
          ctx.setLineDash([]);
          break;
        case 'planetary nebula':
          ctx.beginPath(); ctx.arc(x, y, radius * 0.7, 0, Math.PI * 2); ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(x - radius - 3, y); ctx.lineTo(x - radius * 0.7, y);
          ctx.moveTo(x + radius * 0.7, y); ctx.lineTo(x + radius + 3, y);
          ctx.moveTo(x, y - radius - 3); ctx.lineTo(x, y - radius * 0.7);
          ctx.moveTo(x, y + radius * 0.7); ctx.lineTo(x, y + radius + 3);
          ctx.stroke();
          break;
        case 'supernova remnant':
          ctx.setLineDash([2, 4]);
          ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.stroke();
          ctx.setLineDash([]);
          break;
        default:                                   // nebulae and everything else
          ctx.fillStyle = wash;
          ctx.fillRect(x - radius, y - radius, radius * 2, radius * 2);
          ctx.strokeRect(x - radius, y - radius, radius * 2, radius * 2);
      }

      if (sky.show.labels && (sky.fov < 90 || (object.magnitude ?? 99) < 7)) {
        placeLabel(object.id, x + radius + 4, y + 4, colour, LABEL_FONT_SMALL);
      }
    }
  }

  function drawSolarSystem() {
    const ctx = sky.ctx;
    const jd = julianDate();

    const body = (ra, dec, radius, fill, label, labelColour) => {
      const point = project(ra, dec);
      if (!point) return;
      const altitude = altitudeOf(ra, dec);
      ctx.save();
      ctx.globalAlpha = altitude !== null && altitude < 0 ? 0.32 : 1;
      if (fill) {
        // A planet is a bright point with a glow, like a star that has a
        // colour of its own - not a coloured disc.
        const glow = ctx.createRadialGradient(point[0], point[1], 0,
          point[0], point[1], radius * 2.2);
        glow.addColorStop(0, fill);
        glow.addColorStop(0.4, fill.replace(/[\d.]+\)$/, '0.35)'));
        glow.addColorStop(1, fill.replace(/[\d.]+\)$/, '0)'));
        ctx.fillStyle = glow;
        ctx.beginPath(); ctx.arc(point[0], point[1], radius * 2.2, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = 'rgba(255, 255, 255, 0.9)';
        ctx.beginPath(); ctx.arc(point[0], point[1], Math.max(1.2, radius * 0.45), 0, Math.PI * 2);
        ctx.fill();
      }
      placeLabel(label + (altitude !== null && altitude < 0 ? ' (below)' : ''),
        point[0] + radius + 5, point[1] + 4, labelColour, LABEL_FONT);
      ctx.restore();
      return point;
    };

    const sun = sunPosition(jd);
    body(sun[0], sun[1], 7, 'rgba(255, 236, 170, 0.95)', 'Sun', 'rgba(255, 240, 180, 0.9)');

    const moon = moonPosition(jd);
    const phase = moonPhase(jd);
    const moonPoint = project(moon[0], moon[1]);
    if (moonPoint) {
      const altitude = altitudeOf(moon[0], moon[1]);
      const illumination = Math.round((1 - Math.cos(phase * 2 * Math.PI)) / 2 * 100);
      ctx.save();
      ctx.globalAlpha = altitude !== null && altitude < 0 ? 0.32 : 1;
      drawMoonIcon(moonPoint[0], moonPoint[1], phase, 9);
      placeLabel(`Moon ${illumination}%`, moonPoint[0] + 15, moonPoint[1] + 4,
        'rgba(205, 216, 235, 0.85)', LABEL_FONT);
      ctx.restore();
    }

    for (const planet of planetPositions(jd)) {
      body(planet.ra, planet.dec, planet.size * 0.8, planet.colour, planet.name,
        planet.colour.replace(/[\d.]+\)$/, '0.85)'));
    }
  }

  function drawMoonIcon(x, y, phase, radius) {
    const ctx = sky.ctx;
    const terminator = Math.cos(phase * 2 * Math.PI) * radius;
    ctx.save();
    // A soft glow around the lit limb, the way the Moon actually looks in a
    // sky, then the dark side and the lit side.
    const halo = ctx.createRadialGradient(x, y, radius * 0.8, x, y, radius * 2.4);
    halo.addColorStop(0, 'rgba(200, 210, 235, 0.28)');
    halo.addColorStop(1, 'rgba(200, 210, 235, 0)');
    ctx.fillStyle = halo;
    ctx.beginPath(); ctx.arc(x, y, radius * 2.4, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = 'rgba(22, 26, 42, 0.95)';
    ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = 'rgba(228, 232, 242, 0.95)';
    ctx.beginPath();
    if (phase < 0.5) {
      ctx.arc(x, y, radius, -Math.PI / 2, Math.PI / 2, false);
      ctx.ellipse(x, y, Math.abs(terminator), radius, 0,
        Math.PI / 2, -Math.PI / 2, terminator >= 0);
    } else {
      ctx.arc(x, y, radius, Math.PI / 2, -Math.PI / 2, false);
      ctx.ellipse(x, y, Math.abs(terminator), radius, 0,
        -Math.PI / 2, Math.PI / 2, terminator <= 0);
    }
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = 'rgba(140, 155, 185, 0.5)';
    ctx.lineWidth = 0.8;
    ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.stroke();
    ctx.restore();
  }

  /** Trace a curve through sky positions, breaking it where it leaves the chart. */
  function strokePath(points) {
    const ctx = sky.ctx;
    ctx.beginPath();
    let drawing = false;
    for (const [ra, dec] of points) {
      const point = project(ra, dec);
      if (!point || point[0] < -400 || point[0] > sky.width + 400
        || point[1] < -400 || point[1] > sky.height + 400) { drawing = false; continue; }
      if (drawing) ctx.lineTo(point[0], point[1]);
      else { ctx.moveTo(point[0], point[1]); drawing = true; }
    }
    ctx.stroke();
  }

  function drawHorizon() {
    const ctx = sky.ctx;
    const ring = [];
    for (let az = 0; az <= 360; az += 1) ring.push(altAzToRaDec(0, az));

    // Fill the ground as one closed path rather than testing every pixel for
    // altitude: it is both faster and gives a clean edge.
    const projected = ring.map(([ra, dec]) => project(ra, dec));
    if (projected.every((point) => point !== null)) {
      const trace = () => projected.forEach((point, index) => {
        if (index === 0) ctx.moveTo(point[0], point[1]);
        else ctx.lineTo(point[0], point[1]);
      });

      ctx.save();
      // The ground: a deep, slightly warm dark rather than a black overlay,
      // so the stars below the horizon show as through haze.
      ctx.fillStyle = 'rgba(8, 7, 10, 0.72)';
      ctx.beginPath(); trace(); ctx.closePath();

      // The ring encloses either the sky or the ground depending on where the
      // chart is pointed; the zenith says which, so ask the path directly.
      const zenith = project(...altAzToRaDec(89.9, 0));
      if (zenith && ctx.isPointInPath(zenith[0], zenith[1])) {
        ctx.beginPath();
        ctx.rect(0, 0, sky.width, sky.height);
        trace();
        ctx.closePath();
        ctx.fill('evenodd');
      } else {
        ctx.fill();
      }
      ctx.restore();
    }

    ctx.save();
    ctx.strokeStyle = 'rgba(150, 175, 215, 0.45)';
    ctx.lineWidth = 1.1;
    ctx.lineJoin = 'round';
    strokePath(ring);

    ctx.font = '600 11px "Segoe UI", system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.shadowColor = 'rgba(0, 0, 8, 0.9)';
    ctx.shadowBlur = 3;
    for (const [label, az] of [['N', 0], ['NE', 45], ['E', 90], ['SE', 135],
      ['S', 180], ['SW', 225], ['W', 270], ['NW', 315]]) {
      const point = project(...altAzToRaDec(1.5, az));
      if (!point || point[0] < 14 || point[0] > sky.width - 14
        || point[1] < 14 || point[1] > sky.height - 14) continue;
      ctx.fillStyle = 'rgba(175, 195, 230, 0.9)';
      ctx.fillText(label, point[0], point[1]);
    }
    ctx.restore();
  }

  function drawAltAzGrid() {
    const ctx = sky.ctx;
    ctx.save();
    ctx.strokeStyle = 'rgba(90, 115, 160, 0.22)';
    ctx.lineWidth = 0.7;
    ctx.setLineDash([]);
    for (let altitude = 10; altitude <= 80; altitude += 10) {
      const ring = [];
      for (let az = 0; az <= 360; az += 3) ring.push(altAzToRaDec(altitude, az));
      strokePath(ring);
    }
    for (let az = 0; az < 360; az += 30) {
      const spoke = [];
      for (let altitude = 0; altitude <= 88; altitude += 3) spoke.push(altAzToRaDec(altitude, az));
      strokePath(spoke);
    }
    ctx.setLineDash([]);
    ctx.restore();
  }

  function drawEquatorialGrid() {
    const ctx = sky.ctx;
    ctx.save();
    ctx.strokeStyle = 'rgba(85, 110, 155, 0.22)';
    ctx.lineWidth = 0.7;
    const decStep = sky.fov < 15 ? 2 : sky.fov < 45 ? 5 : 10;
    const raStep = sky.fov < 15 ? 7.5 : sky.fov < 45 ? 15 : 30;

    for (let dec = -80; dec <= 80; dec += decStep) {
      const ring = [];
      for (let ra = 0; ra <= 360; ra += 3) ring.push([ra, dec]);
      strokePath(ring);
    }
    for (let ra = 0; ra < 360; ra += raStep) {
      const meridian = [];
      for (let dec = -88; dec <= 88; dec += 2) meridian.push([ra, dec]);
      strokePath(meridian);
    }
    ctx.restore();
  }

  function drawMeridian() {
    const ctx = sky.ctx;
    ctx.save();
    ctx.strokeStyle = 'rgba(130, 170, 230, 0.45)';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 6]);
    for (const az of [180, 0]) {
      const arc = [];
      for (let altitude = 0; altitude <= 90; altitude += 2) arc.push(altAzToRaDec(altitude, az));
      strokePath(arc);
    }
    ctx.setLineDash([]);
    ctx.restore();
  }

  function drawSelection() {
    const point = project(sky.selected.ra, sky.selected.dec);
    if (!point) return;
    const ctx = sky.ctx;
    ctx.save();
    ctx.strokeStyle = '#7ee7a5';
    ctx.lineWidth = 1.4;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.arc(point[0], point[1], 17, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = '600 11px "Segoe UI", system-ui, sans-serif';
    ctx.fillStyle = '#bff2d3';
    ctx.textAlign = 'center';
    ctx.shadowColor = 'rgba(0, 0, 8, 0.95)';
    ctx.shadowBlur = 3;
    ctx.fillText(sky.selected.name, point[0], point[1] - 23);
    ctx.restore();
  }

  /** The other telescopes in the collaboration, where they last said they
   *  were pointing. A small ringed dot with the telescope's name and what it
   *  is on; dimmed once its last check-in is old. */
  /* Discord profile pictures for the other telescopes, fetched once each.
     Drawn with CORS so the canvas stays readable - the Milky Way sampling
     reads pixels back, and one tainted draw would break that. A picture
     that fails to load, or has not arrived yet, leaves the plain dot. */
  const avatars = new Map();                       // url -> Image | null
  const AVATAR_RADIUS = 11;

  function avatarFor(url) {
    if (!url) return null;
    if (avatars.has(url)) return avatars.get(url);
    avatars.set(url, null);
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => { avatars.set(url, img); draw(); };
    img.onerror = () => avatars.set(url, null);
    img.src = url;
    return null;
  }

  function drawOthers() {
    const ctx = sky.ctx;
    for (const other of sky.others) {
      const point = project(other.ra, other.dec);
      if (!point) continue;
      const [x, y] = point;
      if (x < -30 || x > sky.width + 30 || y < -30 || y > sky.height + 30) continue;
      const fresh = other.online;
      const picture = avatarFor(other.avatar);
      ctx.save();
      ctx.globalAlpha = fresh ? 0.95 : 0.45;
      ctx.strokeStyle = '#7ee7a5';
      ctx.fillStyle = '#7ee7a5';
      ctx.lineWidth = 1.2;
      ctx.shadowColor = 'rgba(0, 0, 8, 0.9)';
      ctx.shadowBlur = 3;
      let labelX = x + 11;
      if (picture) {
        // The person's picture in a mint ring, with a small dot at the exact
        // pointing so the ring's size does not read as a field of view.
        ctx.beginPath(); ctx.arc(x, y, AVATAR_RADIUS + 1.5, 0, Math.PI * 2); ctx.stroke();
        ctx.shadowBlur = 0;
        ctx.save();
        ctx.beginPath(); ctx.arc(x, y, AVATAR_RADIUS, 0, Math.PI * 2); ctx.clip();
        ctx.drawImage(picture, x - AVATAR_RADIUS, y - AVATAR_RADIUS,
          AVATAR_RADIUS * 2, AVATAR_RADIUS * 2);
        ctx.restore();
        labelX = x + AVATAR_RADIUS + 6;
      } else {
        ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke();
        ctx.beginPath(); ctx.arc(x, y, 2, 0, Math.PI * 2); ctx.fill();
        ctx.shadowBlur = 0;
      }
      const who = other.ownerName && other.ownerName !== other.name
        ? ` · ${other.ownerName}` : '';
      const what = other.target ? ` — ${other.target}` : (other.state ? ` (${other.state})` : '');
      const age = other.ageSeconds >= 60 ? `, ${Math.round(other.ageSeconds / 60)} min ago` : '';
      placeLabel(`${other.name}${who}${what}${fresh ? '' : age}`, labelX, y + 4,
        fresh ? 'rgba(190, 240, 210, 0.9)' : 'rgba(160, 180, 170, 0.7)', LABEL_FONT_SMALL);
      ctx.restore();
    }
  }

  function drawTelescope() {
    const point = project(sky.scope.ra, sky.scope.dec);
    if (!point) return;
    const ctx = sky.ctx;
    const [x, y] = point;
    const colour = sky.scope.slewing ? '#d9a03d' : '#4bb87a';

    ctx.save();
    ctx.strokeStyle = colour;
    ctx.lineWidth = 1.8;
    ctx.shadowColor = 'rgba(0, 0, 0, 0.9)';
    ctx.shadowBlur = 4;
    const arm = 22, gap = 6;
    ctx.beginPath();
    ctx.moveTo(x - arm, y); ctx.lineTo(x - gap, y);
    ctx.moveTo(x + gap, y); ctx.lineTo(x + arm, y);
    ctx.moveTo(x, y - arm); ctx.lineTo(x, y - gap);
    ctx.moveTo(x, y + gap); ctx.lineTo(x, y + arm);
    ctx.stroke();
    ctx.beginPath(); ctx.arc(x, y, gap * 1.3, 0, Math.PI * 2); ctx.stroke();
    ctx.shadowBlur = 0;
    placeLabel(sky.scope.slewing ? 'slewing' : 'telescope', x + arm + 5, y + 4, colour,
      '600 11px "Segoe UI", system-ui, sans-serif');

    // The camera's real footprint, so framing can be judged here rather than
    // after a test exposure.
    if (sky.show.fov && sky.frame) {
      const perDegree = pixelsPerDegree();
      const halfWidth = sky.frame.width / 2 * perDegree;
      const halfHeight = sky.frame.height / 2 * perDegree;
      if (halfWidth > 1.5 && halfHeight > 1.5) {
        const angle = (sky.frame.rotation || 0) * DEG;
        const cos = Math.cos(angle), sin = Math.sin(angle);
        ctx.strokeStyle = colour;
        ctx.globalAlpha = 0.7;
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        [[-halfWidth, -halfHeight], [halfWidth, -halfHeight],
          [halfWidth, halfHeight], [-halfWidth, halfHeight]].forEach(([cx, cy], index) => {
          const px = x + cx * cos - cy * sin;
          const py = y + cx * sin + cy * cos;
          if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        });
        ctx.closePath();
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }
    ctx.restore();
  }

  /* ---------------------------------------------------------------- format */

  function formatRa(raDeg) {
    const hours = raHours(raDeg);
    const h = Math.floor(hours);
    const m = Math.floor((hours - h) * 60);
    const s = ((hours - h) * 60 - m) * 60;
    return `${String(h).padStart(2, '0')}h ${String(m).padStart(2, '0')}m ${s.toFixed(1)}s`;
  }

  function formatDec(decDeg) {
    const sign = decDeg < 0 ? '-' : '+';
    const abs = Math.abs(decDeg);
    const d = Math.floor(abs);
    const m = Math.floor((abs - d) * 60);
    const s = ((abs - d) * 60 - m) * 60;
    return `${sign}${String(d).padStart(2, '0')}° ${String(m).padStart(2, '0')}' ${s.toFixed(0)}"`;
  }

  /* ----------------------------------------------------------- interaction */

  function select(object) {
    sky.selected = object;
    const altitude = altitudeOf(object.ra, object.dec);
    $('skyTarget').textContent =
      `${object.name}   ${formatRa(object.ra)}  ${formatDec(object.dec)}`
      + (altitude === null ? '' : `   alt ${altitude.toFixed(1)}°`);
    document.querySelectorAll('.sky-object').forEach((row) => {
      row.classList.toggle('active', row.dataset.objectId === object.id);
    });
    draw();
  }

  function centreOn(ra, dec) {
    sky.centre = { ra, dec: Math.max(-89.5, Math.min(89.5, dec)) };
    draw();
  }

  /** Whatever is nearest the pointer, within a comfortable grab radius. */
  function pick(px, py) {
    let best = null;
    let bestDistance = 18;

    if (sky.show.deepSky) {
      for (const object of sky.deepSky) {
        const point = project(object.ra, object.dec);
        if (!point) continue;
        const distance = Math.hypot(point[0] - px, point[1] - py);
        if (distance < bestDistance) { bestDistance = distance; best = object; }
      }
    }
    const limit = limitingMagnitude();
    for (const [ra, dec, mag, , proper, designation] of sky.stars) {
      if (mag > limit) break;
      const point = project(ra, dec);
      if (!point) continue;
      const distance = Math.hypot(point[0] - px, point[1] - py);
      // Brighter stars win ties, which is what the eye expects.
      if (distance < bestDistance - (proper ? 4 : 0)) {
        bestDistance = distance;
        const name = proper || designation || `mag ${mag.toFixed(1)} star`;
        best = { id: name, name, ra, dec, type: 'star', magnitude: mag };
      }
    }
    return best;
  }

  function bindStage() {
    const stage = $('skyChart').parentElement;
    const canvas = $('skyChart');
    let dragging = false;
    let moved = 0;
    let lastX = 0, lastY = 0;

    canvas.addEventListener('mousedown', (event) => {
      if (event.button !== 0) return;
      dragging = true; moved = 0;
      lastX = event.clientX; lastY = event.clientY;
      stage.classList.add('panning');
    });

    window.addEventListener('mouseup', () => {
      dragging = false;
      stage.classList.remove('panning');
    });

    canvas.addEventListener('mousemove', (event) => {
      const rect = canvas.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;

      if (dragging) {
        const dx = event.clientX - lastX;
        const dy = event.clientY - lastY;
        moved += Math.abs(dx) + Math.abs(dy);
        lastX = event.clientX; lastY = event.clientY;
        const perRadian = sky.scale;
        const cosDec = Math.max(0.05, Math.cos(sky.centre.dec * DEG));
        sky.centre.ra = ((sky.centre.ra - (dx / perRadian) / DEG / cosDec) % 360 + 360) % 360;
        sky.centre.dec = Math.max(-89.5, Math.min(89.5,
          sky.centre.dec + (dy / perRadian) / DEG));
        draw();
        return;
      }

      const [ra, dec] = unproject(px, py);
      const parts = [`${formatRa(ra)}  ${formatDec(dec)}`];
      const position = altAz(ra, dec);
      if (position) {
        parts.push(`alt ${position.altitude.toFixed(1)}°`);
        parts.push(`az ${position.azimuth.toFixed(1)}°`);
      }
      const under = pick(px, py);
      if (under) parts.push(`— ${under.name}`);
      $('skyHudCursor').textContent = parts.join('   ');
      canvas.style.cursor = under ? 'pointer' : 'crosshair';
    });

    canvas.addEventListener('wheel', (event) => {
      event.preventDefault();
      const factor = Math.exp(event.deltaY * 0.0012);
      sky.fov = Math.max(0.05, Math.min(200, sky.fov * factor));
      draw();
    }, { passive: false });

    canvas.addEventListener('click', (event) => {
      if (moved > 4) return;                       // that was a pan, not a click
      const rect = canvas.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      const object = pick(px, py);
      if (object) select(object);
    });

    canvas.addEventListener('dblclick', (event) => {
      const rect = canvas.getBoundingClientRect();
      const [ra, dec] = unproject(event.clientX - rect.left, event.clientY - rect.top);
      centreOn(ra, dec);
    });

    canvas.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      const object = pick(px, py);
      if (object) {
        select(object);
        openMenu(event, object);
      } else {
        const [ra, dec] = unproject(px, py);
        openMenu(event, { id: 'sky', name: 'this position', ra, dec });
      }
    });

    document.addEventListener('click', (event) => {
      if (!$('skyMenu').hidden && !$('skyMenu').contains(event.target)) closeMenu();
    });
  }

  function closeMenu() {
    $('skyMenu').hidden = true;
    $('skyMenu').innerHTML = '';
  }

  function openMenu(event, object) {
    const menu = $('skyMenu');
    const mount = (app.state.status && app.state.status.devices.mount) || {};
    const solver = (app.state.status && app.state.status.solver) || {};
    const stage = menu.parentElement.getBoundingClientRect();

    const title = document.createElement('div');
    title.className = 'sky-menu-title';
    title.innerHTML = '<b></b><span></span>';
    title.querySelector('b').textContent = object.name;
    const altitude = altitudeOf(object.ra, object.dec);
    title.querySelector('span').textContent =
      `${formatRa(object.ra)}  ${formatDec(object.dec)}`
      + (altitude === null ? '' : `   alt ${altitude.toFixed(1)}°`);

    menu.innerHTML = '';
    menu.appendChild(title);

    const item = (label, handler, { disabled = false, hint = '' } = {}) => {
      const button = document.createElement('button');
      button.textContent = label;
      button.disabled = disabled;
      if (hint) button.title = hint;
      button.addEventListener('click', () => { closeMenu(); handler(); });
      menu.appendChild(button);
    };
    const rule = () => menu.appendChild(document.createElement('hr'));

    const hours = raHours(object.ra);
    const notConnected = 'Connect a mount first';
    const belowHorizon = altitude !== null && altitude < 0;

    item(belowHorizon ? 'Slew here (below horizon)' : 'Slew here', async () => {
      if (belowHorizon) {
        const ok = await app.confirmAction(
          `${object.name} is ${Math.abs(altitude).toFixed(1)}° below your horizon. `
          + 'Slew there anyway?',
          { title: 'Below the horizon', confirmLabel: 'Slew anyway', danger: true });
        if (!ok) return;
      }
      app.send('/api/mount/slew', 'POST', { ra: hours, dec: object.dec },
        `Slewing to ${object.name}`);
    }, { disabled: !mount.connected, hint: mount.connected ? '' : notConnected });

    item('Slew and centre (plate solve)', () => {
      app.send('/api/solve', 'POST', { mode: 'center', ra: hours, dec: object.dec },
        `Centring on ${object.name}`);
    }, {
      disabled: !mount.connected || !solver.available || solver.busy,
      hint: solver.available ? '' : 'ASTAP was not found',
    });

    rule();

    item('Sync mount here', async () => {
      const ok = await app.confirmAction(
        `Tell the mount it is currently pointing at ${object.name}? `
        + 'Only do this when the telescope really is on this object — it changes '
        + 'the pointing model.',
        { title: 'Sync mount', confirmLabel: 'Sync', danger: true });
      if (!ok) return;
      app.send('/api/mount/sync', 'POST', { ra: hours, dec: object.dec },
        `Synced to ${object.name}`);
    }, { disabled: !mount.connected, hint: mount.connected ? '' : notConnected });

    item('Set as capture target', () => {
      $('targetName').value = object.name;
      $('targetRa').value = hours.toFixed(6);
      $('targetDec').value = object.dec.toFixed(5);
      app.send('/api/capture/output', 'POST', { target: object.name },
        `Capture target set to ${object.name}`);
    });

    item('Plan a framing here', () => {
      if (!window.planner) { app.toast('The planner is not loaded', 'error'); return; }
      window.planner.frame(hours, object.dec, object.name, object.size);
    });

    rule();

    item('Centre the chart here', () => centreOn(object.ra, object.dec));
    item('Copy coordinates', () => {
      const text = `${formatRa(object.ra)} ${formatDec(object.dec)}`;
      navigator.clipboard.writeText(text)
        .then(() => app.toast('Coordinates copied', 'success'))
        .catch(() => app.toast(text, 'info'));
    });

    menu.hidden = false;
    const x = event.clientX - stage.left;
    const y = event.clientY - stage.top;
    menu.style.left = `${Math.max(4, Math.min(x, stage.width - menu.offsetWidth - 6))}px`;
    menu.style.top = `${Math.max(4, Math.min(y, stage.height - menu.offsetHeight - 6))}px`;
  }

  /* ---------------------------------------------------------------- loading */

  let loadStarted = false;

  async function activate() {
    resize();
    if (loadStarted) { draw(); return; }
    loadStarted = true;
    loadMilkyWay();

    try {
      const [starPayload, catalogPayload] = await Promise.all([
        // Revalidate rather than trusting the cache: it is a local file over
        // loopback, so caching buys nothing, and a cached 404 from a half
        // updated install would otherwise stick until the profile was cleared.
        fetch(STAR_CATALOG, { cache: 'no-cache' }).then((response) => {
          if (!response.ok) throw new Error(`${STAR_CATALOG} — HTTP ${response.status}`);
          return response.json();
        }),
        app.api('/api/catalog'),
      ]);
      sky.stars = starPayload.stars;
      sky.deepSky = catalogPayload.objects.map((entry) => ({
        ...entry, ra: raDegrees(entry.ra),
      }));
    } catch (error) {
      const detail = (error && error.message) || String(error);
      loadStarted = false;
      $('skyLoading').hidden = true;
      $('skyOffline').hidden = false;
      $('skyOfflineDetail').textContent = detail;
      // Say it three ways: on the panel, as a toast, and in the session log, so
      // the reason cannot be missed or lost by switching tabs.
      app.toast(`Planetarium: ${detail}`, 'error', 9000);
      app.api('/api/log', 'POST',
        { message: `Planetarium: ${detail}`, level: 'error' }).catch(() => {});
      return;
    }

    $('skyLoading').hidden = true;
    $('skyOffline').hidden = true;
    sky.ready = true;

    // Open looking at something worth seeing: the telescope if it is connected,
    // otherwise the meridian at a comfortable altitude.
    if (sky.scope) centreOn(sky.scope.ra, sky.scope.dec);
    else if (sky.site) centreOn(lstDegrees(), Math.max(-30, sky.site.latitude - 25));
    else draw();
    renderCatalog();
  }

  function resize() {
    const canvas = $('skyChart');
    const stage = canvas.parentElement;
    const ratio = window.devicePixelRatio || 1;
    sky.canvas = canvas;
    sky.ctx = canvas.getContext('2d');
    sky.width = stage.clientWidth;
    sky.height = stage.clientHeight;
    canvas.width = Math.max(1, Math.round(sky.width * ratio));
    canvas.height = Math.max(1, Math.round(sky.height * ratio));
    sky.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    if (sky.ready) draw();
  }

  /* ---------------------------------------------------------------- catalog */

  function renderCatalog() {
    const host = $('skyCatalogList');
    if (!host) return;
    const filter = $('skyCatalogFilter').value.trim().toLowerCase();
    const sort = $('skyCatalogSort').value;
    const visibleOnly = $('chkSkyVisibleOnly').checked && !!sky.site;

    let rows = sky.deepSky.map((entry) => ({
      ...entry, altitude: altitudeOf(entry.ra, entry.dec),
    }));
    if (filter) {
      rows = rows.filter((entry) => `${entry.id} ${entry.name} ${entry.type} ${entry.constellation}`
        .toLowerCase().includes(filter));
    }
    if (visibleOnly) rows = rows.filter((entry) => entry.altitude > 0);
    if (sort === 'altitude') rows.sort((a, b) => (b.altitude ?? -99) - (a.altitude ?? -99));
    else if (sort === 'magnitude') rows.sort((a, b) => (a.magnitude ?? 99) - (b.magnitude ?? 99));

    host.innerHTML = '';
    if (!rows.length) {
      host.innerHTML = '<p class="muted small">Nothing matches.</p>';
      return;
    }
    for (const entry of rows) {
      const row = document.createElement('div');
      row.className = 'sky-object'
        + (entry.altitude !== null && entry.altitude <= 0 ? ' below' : '')
        + (sky.selected && sky.selected.id === entry.id ? ' active' : '');
      row.dataset.objectId = entry.id;
      row.innerHTML = '<div class="id"></div><div class="alt"></div><div class="meta"></div>';
      row.querySelector('.id').textContent = entry.id;
      row.querySelector('.alt').textContent = entry.altitude === null
        ? '' : `${entry.altitude.toFixed(0)}°`;
      row.querySelector('.meta').textContent = [
        entry.name !== entry.id ? entry.name : null,
        entry.type,
        entry.magnitude !== null && entry.magnitude !== undefined ? `m${entry.magnitude}` : null,
        entry.constellation,
      ].filter(Boolean).join(' · ');

      row.addEventListener('click', () => { select(entry); centreOn(entry.ra, entry.dec); });
      row.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        select(entry);
        openMenu(event, entry);
      });
      host.appendChild(row);
    }
  }

  /* ----------------------------------------------------------------- search */

  let searchTimer = null;

  function bindSearch() {
    const input = $('skySearch');
    const results = $('skyResults');

    input.addEventListener('input', () => {
      clearTimeout(searchTimer);
      const query = input.value.trim();
      if (!query) { results.hidden = true; return; }
      searchTimer = setTimeout(() => runSearch(query), 200);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { results.hidden = true; input.blur(); }
      if (event.key === 'Enter') {
        const first = results.querySelector('.sky-result');
        if (first) first.click();
      }
    });
    input.addEventListener('blur', () => setTimeout(() => { results.hidden = true; }, 160));
    input.addEventListener('focus', () => {
      if (results.children.length) results.hidden = false;
    });
  }

  /** Bright stars are searched locally; deep sky goes through the server, which
      also falls back to SIMBAD when it is online. */
  function searchStars(query, limit = 6) {
    const needle = query.toLowerCase();
    const hits = [];
    for (const [ra, dec, mag, , proper, designation] of sky.stars) {
      const haystack = `${proper} ${designation}`.toLowerCase();
      if (haystack.includes(needle)) {
        const name = proper || designation;
        hits.push({ id: name, name, ra, dec, type: 'star', magnitude: mag,
          constellation: (designation.split(' ')[1] || '') });
        if (hits.length >= limit) break;
      }
    }
    return hits;
  }

  async function runSearch(query) {
    const results = $('skyResults');
    const rows = searchStars(query);
    const seen = new Set(rows.map((entry) => entry.name.toLowerCase()));
    try {
      const found = await app.api(`/api/catalog/search?q=${encodeURIComponent(query)}`);
      for (const raw of found.results) {
        // Sesame resolves bright stars too, so it would otherwise offer a second
        // "Vega" alongside the one already in the local catalogue.
        if (seen.has((raw.name || raw.id || '').toLowerCase())) continue;
        rows.push({ ...raw, ra: raDegrees(raw.ra),
          source: found.source === 'sesame' ? 'via SIMBAD' : null });
      }
    } catch (error) { /* offline is fine; the local star hits still stand */ }

    results.innerHTML = '';
    if (!rows.length) {
      results.innerHTML = '<div class="sky-result"><div class="meta">Nothing found.</div></div>';
      results.hidden = false;
      return;
    }

    for (const entry of rows) {
      const altitude = altitudeOf(entry.ra, entry.dec);
      const row = document.createElement('div');
      row.className = `sky-result${altitude !== null && altitude <= 0 ? ' below' : ''}`;
      row.innerHTML = '<div class="id"></div><div class="alt"></div><div class="meta"></div>';
      row.querySelector('.id').textContent = entry.id;
      row.querySelector('.alt').textContent = altitude === null
        ? '' : `alt ${altitude.toFixed(0)}°`;
      row.querySelector('.meta').textContent = [
        entry.name !== entry.id ? entry.name : null,
        entry.type,
        entry.magnitude !== null && entry.magnitude !== undefined ? `m${entry.magnitude}` : null,
        entry.constellation,
        entry.source,
      ].filter(Boolean).join(' · ');

      row.addEventListener('mousedown', (event) => event.preventDefault());
      row.addEventListener('click', () => {
        results.hidden = true;
        $('skySearch').value = entry.id;
        select(entry);
        centreOn(entry.ra, entry.dec);
      });
      results.appendChild(row);
    }
    results.hidden = false;
  }

  /* ------------------------------------------------------------ live status */

  function cameraFrame(status) {
    const camera = status.devices.camera || {};
    const optics = (app.state.settings && app.state.settings.optics) || {};
    const solved = status.solver && status.solver.result;

    // A real solve beats any arithmetic: it knows the true scale and angle.
    if (solved && solved.fovWidth && solved.fovHeight) {
      return { width: solved.fovWidth, height: solved.fovHeight,
        rotation: solved.rotation || 0 };
    }
    const sensor = camera.sensor || [0, 0];
    const pixel = camera.pixelSizeUm || 0;
    if (!optics.focalLength || !pixel || !sensor[0] || !sensor[1]) return null;
    const arcsecPerPixel = 206.265 * pixel / optics.focalLength;
    return {
      width: arcsecPerPixel * sensor[0] / 3600,
      height: arcsecPerPixel * sensor[1] / 3600,
      rotation: optics.rotation || 0,
    };
  }

  function applyStatus(status) {
    if (!status) return;

    const site = status.site || {};
    const hadSite = !!sky.site;
    sky.site = site.latitude === null || site.latitude === undefined ? null : {
      latitude: site.latitude, longitude: site.longitude,
    };
    $('skyHudSite').textContent = sky.site
      ? `${sky.site.latitude.toFixed(3)}° N  ${sky.site.longitude.toFixed(3)}° E`
        + (status.lst !== null && status.lst !== undefined
          ? `   LST ${app.fmtHours(status.lst)}` : '')
      : 'site not set — open Site & Optics';

    const mount = status.devices.mount || {};
    const previous = sky.scope;
    if (mount.connected) {
      sky.scope = { ra: raDegrees(mount.ra), dec: mount.dec, slewing: !!mount.slewing };
      $('skyScopeReadout').textContent =
        `telescope ${formatRa(sky.scope.ra)}  ${formatDec(sky.scope.dec)}`
        + (mount.slewing ? '  (slewing)' : '');
    } else {
      sky.scope = null;
      $('skyScopeReadout').textContent = 'mount not connected';
    }
    $('btnSkyScope').disabled = !sky.scope;

    sky.frame = cameraFrame(status);

    // Everybody else's telescope, as the server last said. Our own is drawn
    // from the mount, so it is left out of this list.
    const collab = status.collab || {};
    const mine = collab.agentId || '';
    const others = ((collab.presence || {}).telescopes || [])
      .filter((t) => t.id !== mine && t.ra !== null && t.ra !== undefined
        && t.dec !== null && t.dec !== undefined)
      .map((t) => ({ ...t, ra: raDegrees(t.ra) }));
    const othersKey = JSON.stringify(others.map((t) => [t.id, t.ra, t.dec, t.online, t.target, t.avatar, t.ownerName]));
    const othersMoved = othersKey !== sky.othersKey;
    sky.others = others;
    sky.othersKey = othersKey;

    if (!sky.ready) return;
    const scopeMoved = (!!previous !== !!sky.scope)
      || (previous && sky.scope && (Math.abs(previous.ra - sky.scope.ra) > 0.0005
        || Math.abs(previous.dec - sky.scope.dec) > 0.0005
        || previous.slewing !== sky.scope.slewing));
    if (scopeMoved || othersMoved || !!sky.site !== hadSite) draw();
    if (!!sky.site !== hadSite) renderCatalog();
  }

  /* ----------------------------------------------------------------- wiring */

  function bindControls() {
    for (const [id, key] of [['chkSkyHorizon', 'horizon'], ['chkSkyGrid', 'grid'],
      ['chkSkyEqGrid', 'eqGrid'], ['chkSkyMeridian', 'meridian'],
      ['chkSkyLabels', 'labels'], ['chkSkyDeepSky', 'deepSky'],
      ['chkSkySolar', 'solar'], ['chkSkyScope', 'scope'], ['chkSkyFov', 'fov'],
      ['chkSkyOthers', 'others']]) {
      $(id).addEventListener('change', () => { sky.show[key] = $(id).checked; draw(); });
    }

    const zoom = (factor) => {
      sky.fov = Math.max(0.05, Math.min(200, sky.fov * factor));
      draw();
    };
    $('btnSkyZoomIn').addEventListener('click', () => zoom(1 / 1.5));
    $('btnSkyZoomOut').addEventListener('click', () => zoom(1.5));

    $('btnSkyFrame').addEventListener('click', () => {
      if (!sky.frame) {
        app.toast('Set the focal length in Site & Optics first', 'error');
        return;
      }
      sky.fov = Math.max(0.05, Math.max(sky.frame.width, sky.frame.height) * 2.4);
      if (sky.scope) centreOn(sky.scope.ra, sky.scope.dec); else draw();
    });

    $('btnSkyCatalog').addEventListener('click', () => {
      const panel = $('skyCatalog');
      panel.hidden = !panel.hidden;
      $('btnSkyCatalog').classList.toggle('active', !panel.hidden);
      resize();
      if (!panel.hidden) renderCatalog();
    });
    $('btnSkyScope').addEventListener('click', () => {
      if (sky.scope) centreOn(sky.scope.ra, sky.scope.dec);
    });
    $('btnSkyRetry').addEventListener('click', () => {
      $('skyOffline').hidden = true;
      $('skyLoading').hidden = false;
      activate();
    });

    $('skyCatalogFilter').addEventListener('input', renderCatalog);
    $('skyCatalogSort').addEventListener('change', renderCatalog);
    $('chkSkyVisibleOnly').addEventListener('change', renderCatalog);

    document.querySelectorAll('[data-sky-time]').forEach((button) => {
      button.addEventListener('click', () => {
        sky.timeOffset += Number(button.dataset.skyTime);
        updateTimeLabel();
      });
    });
    $('btnSkyNow').addEventListener('click', () => { sky.timeOffset = 0; updateTimeLabel(); });

    new ResizeObserver(resize).observe($('skyWrap'));
  }

  function updateTimeLabel() {
    const seconds = sky.timeOffset;
    if (!seconds) {
      $('skyTimeLabel').textContent = 'now';
    } else {
      const total = Math.abs(seconds);
      const hours = Math.floor(total / 3600);
      const minutes = Math.round((total % 3600) / 60);
      $('skyTimeLabel').textContent =
        `${seconds < 0 ? '−' : '+'}${hours ? `${hours}h ` : ''}${minutes}m`
        + `  ·  ${now().toLocaleTimeString([], { hour12: false })}`;
    }
    if (sky.ready) draw();
    renderCatalog();
  }

  /** Used by the Plate Solve panel to drop a solved position onto the chart. */
  window.sky = {
    goto(hours, decDeg, name = 'position') {
      const object = { id: name, name, ra: raDegrees(hours), dec: decDeg };
      activate().then(() => { select(object); centreOn(object.ra, object.dec); });
    },
  };

  function init() {
    bindControls();
    bindSearch();
    bindStage();
    updateTimeLabel();
    app.onStatus((status, tab) => {
      applyStatus(status);
      if (tab === 'sky') activate();
    });
    // Altitudes drift; keep the catalogue list and the horizon honest.
    setInterval(() => {
      if ($('skyWrap').offsetParent === null) return;
      if (sky.ready) draw();
      if (!$('skyCatalog').hidden) renderCatalog();
    }, 30000);
  }

  document.addEventListener('DOMContentLoaded', init);
}());
