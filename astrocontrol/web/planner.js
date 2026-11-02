/* Starfront framing planner.
 *
 * Shows what is really up there — a survey cutout from CDS — with the camera's
 * field drawn on top, so a framing decision gets made before the mount moves
 * rather than after a test exposure. Rotate it, tile it into a mosaic, then save
 * it to the target list.
 *
 * The picture is a gnomonic (TAN) projection centred on the search position with
 * north up and east left, which is what hips2fits returns and what the overlay
 * maths below assumes. Angles are degrees; RA is degrees internally and hours
 * only at the API boundary.
 */
'use strict';

(function () {
  const DEG = Math.PI / 180;
  const app = window.astro;
  const $ = app.$;

  const plan = {
    canvas: null,
    ctx: null,
    width: 0,
    height: 0,

    image: null,          // the loaded survey cutout, or an opened FITS frame
    imageCentre: null,    // { ra, dec } the picture is centred on, degrees
    imageFov: 0,          // width of the picture in degrees
    // The sky position angle of the picture's own up axis. A survey cutout is
    // north up and this is 0; one of your own frames is at whatever angle the
    // camera was at, and the overlay has to turn with it or the framing
    // rectangle lands in the wrong place on a picture that is not north up.
    imageRotation: 0,
    imageFlipped: false,  // mirrored, as a solve reports it
    // How much sky the canvas covers, which is not always how much the picture
    // covers. A survey cutout is fetched at the size it will be shown, so the
    // two match; one of your own frames is whatever field your rig has, and it
    // can be a good deal smaller than the camera field being planned — in which
    // case the view pulls back so the framing rectangle is on screen at all,
    // and the frame sits inside it at its true angular size.
    viewFov: 0,
    // Whether the operator has chosen a field themselves. Once they have, the
    // default worked out from the camera stops overriding it.
    touchedFov: false,
    reference: null,      // the opened FITS frame, if that is what is showing
    loading: false,

    target: null,         // { name, ra, dec } the object being framed
    centre: null,         // { ra, dec } where the framing is pointed, degrees
    rotation: 0,          // position angle, degrees
    // Whether the operator has set the framing angle themselves. Until they
    // have, it follows the camera angle the last plate solve measured — the
    // rectangle should start at the angle the camera is actually at, not at
    // north-up, which it never is.
    touchedRotation: false,
    rows: 1,
    columns: 1,
    overlap: 0.1,
    // 'aligned' - each panel's angle is corrected so the composite stays a
    //             rectangle and the overlap holds at any angle. The default,
    //             because that is what a mosaic is.
    // 'fixed'   - the rotator never moves, so panels fan apart as north turns
    align: 'aligned',

    field: null,          // { width, height, scale, source } camera field
    targets: [],
    selectedTarget: null,
    dragging: false,

    // The dock: the target list, and what has been chosen for tonight.
    // Which collaborations to be in is decided on the Collab tab; by the time
    // a chunk reaches here it is a target like any other.
    planEntries: [],      // tonight, as the plan holds it
    night: null,          // dusk and dawn, for turning a clock time into a moment
    dragTarget: null,     // the target id being dragged into tonight

    // Drawing out the patch of sky a collaboration covers. A project's region
    // is a shape, and the only honest way to choose one is to see what falls
    // inside it — so it is dragged onto the picture rather than typed into four
    // boxes whose result nobody can picture.
    //
    // Deliberately independent of the camera field. The coordinator's own rig
    // has nothing to do with how big somebody else's project should be, and
    // tying the rectangle to it would make the one telescope that happens to be
    // running the collaboration decide the shape of everybody's work.
    regionMode: false,
    region: null,         // { ra, dec, width, height, rotation }, sky degrees
    regionDrag: null,     // { x0, y0, x1, y1 } in canvas pixels, while dragging
  };

  const raHours = (deg) => ((deg / 15) % 24 + 24) % 24;
  const raDegrees = (hours) => ((hours * 15) % 360 + 360) % 360;

  /* --------------------------------------------------------- projection */

  /** Tangent-plane offset (east, north) in degrees, matching framing.py. */
  function skyToOffset(ra0, dec0, ra, dec) {
    const r0 = ra0 * DEG, d0 = dec0 * DEG, r = ra * DEG, d = dec * DEG;
    const dRa = r - r0;
    const denominator = Math.sin(d) * Math.sin(d0)
      + Math.cos(d) * Math.cos(d0) * Math.cos(dRa);
    if (denominator <= 0) return null;
    return [
      Math.cos(d) * Math.sin(dRa) / denominator / DEG,
      (Math.sin(d) * Math.cos(d0) - Math.cos(d) * Math.sin(d0) * Math.cos(dRa))
        / denominator / DEG,
    ];
  }

  function offsetToSky(ra0, dec0, east, north) {
    const r0 = ra0 * DEG, d0 = dec0 * DEG;
    const xi = east * DEG, eta = north * DEG;
    const denominator = Math.cos(d0) - eta * Math.sin(d0);
    const ra = r0 + Math.atan2(xi, denominator);
    const dec = Math.atan2(Math.sin(d0) + eta * Math.cos(d0), Math.hypot(xi, denominator));
    return [((ra / DEG) % 360 + 360) % 360, dec / DEG];
  }

  /** Camera-frame offset to sky offset at a position angle (matches framing.py).
   *
   *  x is right in the image, y is up. A north-up image has east on the left, so
   *  at PA 0 the camera's up axis is north and its right axis is west:
   *      up = (sin PA, cos PA), right = (-cos PA, sin PA), as (east, north).
   *  The grid has to turn with the camera — at PA 90 the panel above the centre
   *  moves to its east, which is drawn on the left.
   */
  function rotateOffset(x, y, positionAngle) {
    const angle = positionAngle * DEG;
    const cos = Math.cos(angle), sin = Math.sin(angle);
    return [-x * cos + y * sin, x * sin + y * cos];
  }

  /** Pixels per degree, from how much sky the canvas is showing. */
  const pixelsPerDegree = () => {
    const fov = plan.viewFov || plan.imageFov;
    return fov ? plan.width / fov : 0;
  };

  /* Sky to canvas, and back. North is up and east is left, always.
   *
   * The canvas is a picture of the sky, not a picture of a file. Everything on
   * it — the framing angle, the mosaic grid, the compass, the position angle
   * readout — is stated in sky terms, so the coordinate frame has to be the
   * sky's. A reference frame shot at 35° is *rotated into* this frame when it
   * is drawn, rather than the frame being rotated to match the file.
   *
   * Doing it the other way round very nearly works and is wrong in a way that
   * is hard to see: every overlay is then correct relative to the picture but
   * the whole view is tilted against the sky, so "PA 0" does not look like
   * north up, and switching between a reference and a survey cutout silently
   * reorients everything.
   */
  function project(ra, dec) {
    if (!plan.imageCentre) return null;
    const offset = skyToOffset(plan.imageCentre.ra, plan.imageCentre.dec, ra, dec);
    if (!offset) return null;
    const scale = pixelsPerDegree();
    return [plan.width / 2 - offset[0] * scale, plan.height / 2 - offset[1] * scale];
  }

  /** A movement in canvas pixels, as a movement in (east, north) degrees. */
  function screenToOffset(dx, dy) {
    const scale = pixelsPerDegree();
    if (!scale) return [0, 0];
    return [-dx / scale, -dy / scale];
  }

  function unproject(px, py) {
    if (!plan.imageCentre) return null;
    const [east, north] = screenToOffset(px - plan.width / 2,
      py - plan.height / 2);
    return offsetToSky(plan.imageCentre.ra, plan.imageCentre.dec, east, north);
  }

  /* ------------------------------------------------------------- panels */

  /** Which way north points at (ra, dec) inside the tangent plane at
   *  (ra0, dec0), in degrees. Mirrors framing.north_angle. */
  function northAngle(ra0, dec0, ra, dec) {
    const epsilon = dec + 1e-4 <= 90 ? 1e-4 : -1e-4;
    const a = skyToOffset(ra0, dec0, ra, dec);
    const b = skyToOffset(ra0, dec0, ra, dec + epsilon);
    if (!a || !b) return 0;
    const dXi = epsilon > 0 ? b[0] - a[0] : a[0] - b[0];
    const dEta = epsilon > 0 ? b[1] - a[1] : a[1] - b[1];
    return Math.atan2(dXi, dEta) / DEG;
  }

  /** Panel centres and angles, using the same construction as the server.
   *
   *  Each panel carries the sky position angle it must be shot at, plus the
   *  angle it is drawn at here — which is not the same number, because north
   *  turns across the picture and a camera at a fixed angle turns with it.
   */
  function panelCentres() {
    if (!plan.centre || !plan.field || !plan.field.width) return [];
    const stepX = plan.field.width * (1 - plan.overlap);
    const stepY = plan.field.height * (1 - plan.overlap);
    const panels = [];
    for (let row = 0; row < plan.rows; row += 1) {
      const columns = row % 2 === 0
        ? [...Array(plan.columns).keys()]
        : [...Array(plan.columns).keys()].reverse();
      for (const column of columns) {
        const x = (column - (plan.columns - 1) / 2) * stepX;
        const y = ((plan.rows - 1) / 2 - row) * stepY;
        const [east, north] = rotateOffset(x, y, plan.rotation);
        const [ra, dec] = offsetToSky(plan.centre.ra, plan.centre.dec, east, north);

        // Convergence about the mosaic centre decides the angle to shoot at;
        // convergence about the picture's own centre decides how it is drawn.
        const atCentre = northAngle(plan.centre.ra, plan.centre.dec, ra, dec);
        const skyAngle = plan.align === 'aligned'
          ? plan.rotation - atCentre : plan.rotation;
        const inImage = plan.imageCentre
          ? northAngle(plan.imageCentre.ra, plan.imageCentre.dec, ra, dec) : 0;

        panels.push({
          row, column, ra, dec, index: panels.length + 1,
          skyAngle, convergence: atCentre, drawAngle: skyAngle + inImage,
        });
      }
    }
    return panels;
  }

  /** Does a fixed camera angle still cover the seams at this declination? */
  function seamCheck(panels) {
    if (panels.length < 2 || !plan.field) return null;
    const angles = panels.map((panel) => panel.convergence);
    const spread = Math.max(...angles) - Math.min(...angles);
    const halfDiagonal = Math.hypot(plan.field.width, plan.field.height) / 2;
    const cornerError = Math.abs(halfDiagonal * Math.sin(spread * DEG));
    const margin = Math.min(plan.field.width, plan.field.height) * plan.overlap;
    return { spread, cornerError, margin, gaps: cornerError > margin };
  }

  /* ------------------------------------------------- the collab region */

  /** The rectangle a drag describes, in sky terms.
   *
   *  **North up, always.** It is tempting to square the box to the framing
   *  angle so the slider turns it — but `collab.chunk` lays its tiles out along
   *  RA and declination and does not turn the grid, so a box drawn at an angle
   *  would be covered by a north-up tiling that does not match it. A rectangle
   *  that claims one orientation and is filled in at another is worse than a
   *  plainer rectangle: it looks right and is not.
   *
   *  The framing angle still travels with the region as `rotation`, because it
   *  is worth carrying — it is the camera angle contributing rigs should use,
   *  so that panels from different telescopes stack the same way up. It is the
   *  angle of the *frames*, not of the box.
   */
  function regionFromDrag(drag) {
    if (!plan.imageCentre || !drag) return null;
    // A Region's width and height are angles on the sky measured about its
    // own centre. The drag is a rectangle on a picture whose tangent point
    // is the picture's centre, not the region's, and the two planes differ
    // by a few percent across a wide field - enough that a box measured in
    // one and drawn from the other came back a little bigger or smaller
    // than what was dragged, every time. So the drag's corners are taken to
    // the sky and measured again in the region's own plane.
    const sky = (px, py) => unproject(px, py);
    const centre = sky((drag.x0 + drag.x1) / 2, (drag.y0 + drag.y1) / 2);
    const corners = [sky(drag.x0, drag.y0), sky(drag.x1, drag.y0),
      sky(drag.x1, drag.y1), sky(drag.x0, drag.y1)];
    if (!centre || corners.some((c) => !c)) return null;
    const [ra, dec] = centre;
    const local = corners.map(([r, d]) => skyToOffset(ra, dec, r, d));
    if (local.some((o) => !o)) return null;
    const [tl, tr, br, bl] = local;
    const width = (Math.abs(tr[0] - tl[0]) + Math.abs(br[0] - bl[0])) / 2;
    const height = (Math.abs(tl[1] - bl[1]) + Math.abs(tr[1] - br[1])) / 2;
    if (width < 1e-4 || height < 1e-4) return null;
    return { ra, dec, width, height, rotation: plan.rotation };
  }

  /** A region's four corners on the sky: its width and height laid out on
   *  the plane tangent at its own centre, which is what the server tiles
   *  and what every picture of it draws. */
  function regionCorners(region) {
    const hw = region.width / 2, hh = region.height / 2;
    return [[hw, hh], [-hw, hh], [-hw, -hh], [hw, -hh]]
      .map(([east, north]) => offsetToSky(region.ra, region.dec, east, north));
  }

  function drawRegion() {
    const region = plan.regionDrag ? regionFromDrag(plan.regionDrag) : plan.region;
    if (!region) return;
    const centre = project(region.ra, region.dec);
    if (!centre) return;
    const ctx = plan.ctx;
    const scale = pixelsPerDegree();
    // Drawn from its corners on the sky, each projected onto this picture,
    // so the box is the same sky whichever picture shows it - and, while
    // dragging, sits exactly under the pointer. The framing angle is
    // deliberately not in here - see regionFromDrag.
    const corners = regionCorners(region).map(([r, d]) => project(r, d));
    if (corners.some((c) => !c)) return;

    ctx.save();
    ctx.beginPath();
    corners.forEach(([cx, cy], index) =>
      (index ? ctx.lineTo(cx, cy) : ctx.moveTo(cx, cy)));
    ctx.closePath();
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.8)';
    ctx.lineWidth = 4;
    ctx.stroke();
    ctx.strokeStyle = 'rgba(126, 231, 165, 0.95)';
    ctx.lineWidth = 2;
    ctx.setLineDash([7, 5]);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(126, 231, 165, 0.09)';
    ctx.fill();

    ctx.font = 'bold 12px ui-monospace, monospace';
    ctx.fillStyle = 'rgba(190, 245, 210, 0.95)';
    ctx.textAlign = 'center';
    ctx.shadowColor = 'rgba(0, 0, 0, 0.95)';
    ctx.shadowBlur = 4;
    ctx.fillText(`${region.width.toFixed(2)}° × ${region.height.toFixed(2)}°`,
      centre[0], Math.min(...corners.map((c) => c[1])) - 8);
    ctx.restore();
  }

  function updateRegionBar() {
    const bar = $('planRegionBar');
    const text = $('planRegionText');
    const use = $('btnPlanRegionUse');
    if (!bar) return;
    bar.hidden = !plan.regionMode && !plan.region;
    const region = plan.region;
    if (!region) {
      text.textContent = 'drag a rectangle across the picture';
      use.disabled = true;
      return;
    }
    text.textContent = `${formatRa(region.ra)} ${formatDec(region.dec)}  `
      + `${region.width.toFixed(2)}° × ${region.height.toFixed(2)}°  `
      + `cameras at PA ${(((region.rotation % 360) + 360) % 360).toFixed(0)}°`;
    use.disabled = false;
  }

  function setRegionMode(on) {
    plan.regionMode = on;
    plan.regionDrag = null;
    $('btnPlanRegion').classList.toggle('active', on);
    const stage = $('planCanvas').parentElement;
    stage.classList.toggle('drawing-region', on);
    updateRegionBar();
    draw();
  }

  /* ------------------------------------------------------------ drawing */

  function draw() {
    const ctx = plan.ctx;
    if (!ctx) return;
    ctx.clearRect(0, 0, plan.width, plan.height);
    ctx.fillStyle = '#05070d';
    ctx.fillRect(0, 0, plan.width, plan.height);

    if (plan.image) {
      // Drawn at its true angular size, centred on where it points, and turned
      // to match the sky. Letterboxed rather than stretched: the sky in it is
      // square, and a picture squashed to fill a canvas is one you cannot
      // measure a framing against.
      //
      // The rotation is what puts a reference frame into the same north-up
      // frame as everything else. The image's up axis sits at sky position
      // angle θ, measured north through east; on a canvas with north up and
      // east left that direction is (−sin θ, −cos θ), which is canvas-up turned
      // by −θ. A mirrored frame is un-mirrored first, and its position angle
      // then runs the other way.
      const scale = pixelsPerDegree();
      const drawWidth = plan.imageFov * scale;
      const drawHeight = plan.image.naturalHeight / plan.image.naturalWidth * drawWidth;
      const angle = (plan.imageRotation || 0) * DEG;

      ctx.save();
      ctx.translate(plan.width / 2, plan.height / 2);
      ctx.rotate(plan.imageFlipped ? angle : -angle);
      if (plan.imageFlipped) ctx.scale(-1, 1);
      ctx.drawImage(plan.image, -drawWidth / 2, -drawHeight / 2,
        drawWidth, drawHeight);
      // An outline, so a frame smaller than the view reads as a frame rather
      // than as the edge of the data — and so the angle it was shot at is
      // visible rather than merely implied.
      if (plan.reference) {
        ctx.strokeStyle = 'rgba(120, 150, 190, 0.55)';
        ctx.lineWidth = 1;
        ctx.strokeRect(-drawWidth / 2, -drawHeight / 2, drawWidth, drawHeight);
      }
      ctx.restore();
    }

    // Before the camera-field guard below: a coordinator drawing a region for
    // other people's telescopes may have no optics set on this machine at all,
    // and refusing to draw their rectangle because *this* rig has no focal
    // length would be nonsense.
    drawRegion();

    if (!plan.centre || !plan.field || !plan.field.width) { drawNorthArrow(); return; }

    const panels = panelCentres();
    const scale = pixelsPerDegree();
    const halfWidth = plan.field.width / 2 * scale;
    const halfHeight = plan.field.height / 2 * scale;

    const shapes = panels
      .map((panel) => {
        const point = project(panel.ra, panel.dec);
        return point ? { panel, corners: frameCorners(point[0], point[1],
          halfWidth, halfHeight, panel.drawAngle) } : null;
      })
      .filter(Boolean);

    // Everything outside the frame is dimmed. On a bright object an outline
    // alone disappears; darkening what you will not capture makes the framing
    // readable at a glance, which is the entire job of this view.
    if (shapes.length) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, plan.width, plan.height);
      for (const shape of shapes) {
        shape.corners.forEach(([cx, cy], index) =>
          (index ? ctx.lineTo(cx, cy) : ctx.moveTo(cx, cy)));
        ctx.closePath();
      }
      ctx.fillStyle = 'rgba(5, 7, 13, 0.62)';
      ctx.fill('evenodd');
      ctx.restore();
    }

    for (const shape of shapes) {
      drawFrame(shape.corners, panels.length > 1 ? String(shape.panel.index) : '');
    }

    // The mosaic's overall centre, which is the coordinate that gets saved.
    const centre = project(plan.centre.ra, plan.centre.dec);
    if (centre) {
      ctx.strokeStyle = 'rgba(217, 79, 61, 0.85)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(centre[0] - 9, centre[1]); ctx.lineTo(centre[0] + 9, centre[1]);
      ctx.moveTo(centre[0], centre[1] - 9); ctx.lineTo(centre[0], centre[1] + 9);
      ctx.stroke();
    }
    drawNorthArrow();
  }

  /** The four corners of a frame, in screen pixels.
   *
   *  Screen y grows downward and east is drawn to the left, so the angle used
   *  here is the negative of the sky position angle.
   */
  function frameCorners(x, y, halfWidth, halfHeight, positionAngle) {
    const angle = -positionAngle * DEG;
    const cos = Math.cos(angle), sin = Math.sin(angle);
    const corner = (dx, dy) => [x + dx * cos - dy * sin, y + dx * sin + dy * cos];
    return [
      corner(-halfWidth, -halfHeight), corner(halfWidth, -halfHeight),
      corner(halfWidth, halfHeight), corner(-halfWidth, halfHeight),
    ];
  }

  function drawFrame(corners, label) {
    const ctx = plan.ctx;
    const trace = () => corners.forEach(([cx, cy], index) =>
      (index ? ctx.lineTo(cx, cy) : ctx.moveTo(cx, cy)));

    ctx.save();
    // Dark under-stroke first, so the outline holds up over a bright galaxy as
    // well as over empty sky.
    ctx.beginPath(); trace(); ctx.closePath();
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.85)';
    ctx.lineWidth = 4;
    ctx.stroke();
    ctx.strokeStyle = 'rgba(140, 195, 255, 1)';
    ctx.lineWidth = 1.8;
    ctx.stroke();

    // A bar along the frame's top edge marks which way is "up" in the image the
    // camera will produce — the thing a rotator angle actually controls.
    const [topLeft, topRight] = corners;
    ctx.beginPath();
    ctx.moveTo(topLeft[0], topLeft[1]);
    ctx.lineTo(topRight[0], topRight[1]);
    ctx.strokeStyle = 'rgba(217, 79, 61, 0.95)';
    ctx.lineWidth = 3.5;
    ctx.stroke();

    if (label) {
      const x = (corners[0][0] + corners[2][0]) / 2;
      const y = (corners[0][1] + corners[2][1]) / 2;
      ctx.font = 'bold 15px ui-monospace, monospace';
      ctx.fillStyle = 'rgba(210, 230, 255, 0.95)';
      ctx.textAlign = 'center';
      ctx.shadowColor = 'rgba(0, 0, 0, 0.95)';
      ctx.shadowBlur = 4;
      ctx.fillText(label, x, y + 5);
    }
    ctx.restore();
  }

  /* Which way is north. Always up, because the canvas is the sky: a reference
     frame is turned to match it when it is drawn, not the other way round. */
  function drawNorthArrow() {
    const ctx = plan.ctx;
    const x = plan.width - 32, y = plan.height - 46;
    ctx.save();
    ctx.translate(x, y);
    ctx.strokeStyle = 'rgba(150, 175, 210, 0.75)';
    ctx.fillStyle = 'rgba(150, 175, 210, 0.85)';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(0, 22); ctx.lineTo(0, -12);
    ctx.moveTo(0, 22); ctx.lineTo(-30, 22);
    ctx.stroke();
    ctx.font = '10px ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.fillText('N', 0, -16);
    ctx.textAlign = 'right';
    ctx.fillText('E', -33, 25);
    ctx.restore();
  }

  /* ------------------------------------------------------------- loading */

  /* Set the framing angle.
   *
   * `fromOperator` is what tells a dial somebody turned from an angle the
   * program worked out: once it has been turned, the measured camera angle
   * stops overriding it. */
  function setRotation(value, { fromOperator = true } = {}) {
    const angle = Number(value);
    if (!Number.isFinite(angle)) return;
    plan.rotation = ((angle % 360) + 360) % 360;
    const slider = $('planRotation');
    if (slider) slider.value = Math.round(plan.rotation);
    const box = $('planRotationValue');
    if (box) box.value = Number(plan.rotation.toFixed(1));
    if (fromOperator) plan.touchedRotation = true;
    updateCoverage();
    draw();
  }

  /* How often to ask the server what the camera field is, while the planner is
   * open.
   *
   * It used to be asked exactly once and kept for the life of the page, which
   * is why solving and syncing the rig updated the camera angle everywhere
   * except the planner. A poll rather than a trigger because the angle has
   * several ways of changing — a solve, a sync, a pointing check between subs,
   * or somebody typing it into Site & Optics — and missing any one of them puts
   * the rectangle back to being wrong in a way nobody can see. The call is
   * arithmetic on the server and the planner is one tab, so this is cheap. */
  const FIELD_POLL_MS = 5000;

  let fieldCheckedAt = 0;
  let fieldLoading = false;

  async function loadField() {
    // The status tick is four a second, so without this the poll becomes a
    // flood whenever a call is slower than a tick.
    if (fieldLoading) return;
    fieldLoading = true;
    fieldCheckedAt = Date.now();
    try {
      const payload = await app.api('/api/framing');
      plan.field = payload.field && payload.field.width ? payload.field : null;
      const select = $('planSurvey');
      if (!select.options.length) {
        for (const survey of payload.surveys) {
          select.appendChild(new Option(survey.name, survey.id));
        }
      }
    } catch (error) {
      console.error(error);
      plan.field = null;
    }

    // The camera angle the field came back with is the angle the camera is
    // actually at — measured by a plate solve when there has been one, and the
    // number from Site & Optics otherwise. Starting the framing rectangle at
    // north-up instead meant the box on screen was never the box the camera
    // would cover, however many times the rig was solved and synced.
    if (plan.field && !plan.touchedRotation && Number.isFinite(plan.field.rotation)) {
      setRotation(plan.field.rotation, { fromOperator: false });
    }
    // Open at a field that leaves room to move the framing about, rather than
    // a fixed three degrees that is far too tight for a short focal length and
    // far too wide for a long one. Only before anything has been framed, so it
    // never overrides a field the operator has chosen.
    if (plan.field && plan.field.width && !plan.centre && !plan.touchedFov) {
      $('planFov').value = showFov(defaultViewFov());
    }
    fieldLoading = false;
    updateFieldReadout();
    draw();
  }

  function updateFieldReadout() {
    const node = $('planHudField');
    if (!plan.field || !plan.field.width) {
      node.textContent = 'camera field unknown — set the focal length in Site & Optics';
      $('btnPlanSave').disabled = true;
      return;
    }
    const w = plan.field.width * 60, h = plan.field.height * 60;
    // The angle is in here because it is what the rectangle on screen is drawn
    // at, and "where did that come from" is the question it always raises.
    const angle = Number.isFinite(plan.field.rotation)
      ? `  PA ${(((plan.field.rotation % 360) + 360) % 360).toFixed(1)}°` : '';
    node.textContent = `camera ${w.toFixed(1)}′ × ${h.toFixed(1)}′`
      + (plan.field.scale ? `  ${plan.field.scale.toFixed(2)}″/px` : '') + angle
      + `  (${plan.field.source})`;
    $('btnPlanSave').disabled = !plan.centre;
  }

  function updateCoverage() {
    const node = $('planCoverage');
    if (!plan.field || !plan.field.width || !plan.centre) {
      node.textContent = '';
      node.className = '';
      return;
    }
    const stepX = plan.field.width * (1 - plan.overlap);
    const stepY = plan.field.height * (1 - plan.overlap);
    const width = stepX * (plan.columns - 1) + plan.field.width;
    const height = stepY * (plan.rows - 1) + plan.field.height;
    const count = plan.rows * plan.columns;

    const parts = [
      `${count} panel${count === 1 ? '' : 's'}`,
      `${width.toFixed(2)}° × ${height.toFixed(2)}°`,
      `PA ${plan.rotation.toFixed(1)}°`,
    ];

    const seam = seamCheck(panelCentres());
    node.className = '';
    if (seam && seam.spread > 0.05) {
      if (plan.align === 'aligned') {
        // The rotator has to travel; say how far, and say so loudly if there is
        // no rotator to do the travelling.
        const rotator = (app.state.status && app.state.status.devices.rotator) || {};
        if (rotator.connected) {
          parts.push(`rotator moves ${seam.spread.toFixed(1)}° across the grid`);
        } else {
          parts.push(`needs a rotator — ${seam.spread.toFixed(1)}° of travel, `
            + 'or switch to “Rotator locked”');
          node.className = 'seam-warning';
        }
      } else {
        parts.push(`panels rotate ${seam.spread.toFixed(1)}° apart`);
        if (seam.gaps) {
          const needed = Math.ceil(seam.cornerError
            / Math.min(plan.field.width, plan.field.height) * 100);
          parts.push(`GAPS — needs ~${needed}% overlap, or use “Tiles aligned”`);
          node.className = 'seam-warning';
        }
      }
    }
    node.textContent = parts.join('  ·  ');
  }

  function updateReadout() {
    if (!plan.centre) { $('planReadout').textContent = '—'; return; }
    $('planReadout').textContent =
      `${plan.target ? `${plan.target.name}   ` : ''}`
      + `centre ${formatRa(plan.centre.ra)}  ${formatDec(plan.centre.dec)}`;
  }

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

  async function loadImage() {
    if (!plan.centre) return;
    const fov = Math.max(0.05, Math.min(90, Number($('planFov').value) || 3));
    const survey = $('planSurvey').value || 'CDS/P/DSS2/color';

    plan.loading = true;
    $('planEmpty').hidden = true;
    $('planFailed').hidden = true;
    $('planLoading').hidden = false;

    // Ask for the picture at the size it will be drawn, so nothing is wasted or
    // upscaled, and centre it on the framing rather than the original search.
    const width = Math.min(3000, Math.max(256, Math.round(plan.width * 1.2)));
    const height = Math.round(width * (plan.height / Math.max(1, plan.width)));
    const url = `/api/survey/image?ra=${raHours(plan.centre.ra).toFixed(6)}`
      + `&dec=${plan.centre.dec.toFixed(6)}&fov=${fov}`
      + `&width=${width}&height=${height}&hips=${encodeURIComponent(survey)}`;

    try {
      const response = await fetch(url);
      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try {
          const body = await response.json();
          if (body && body.detail) detail = body.detail;
        } catch { /* not JSON; the status will do */ }
        throw new Error(detail);
      }
      const blob = await response.blob();
      const image = await new Promise((resolve, reject) => {
        const element = new Image();
        element.onload = () => resolve(element);
        element.onerror = () => reject(new Error('the image could not be decoded'));
        element.src = URL.createObjectURL(blob);
      });
      plan.image = image;
      plan.imageCentre = { ra: plan.centre.ra, dec: plan.centre.dec };
      plan.imageFov = fov;
      // A cutout is fetched at the size it will be shown, so the view is the
      // picture.
      plan.viewFov = fov;
      plan.imageRotation = 0;            // survey cutouts are north up
      plan.imageFlipped = false;
      plan.reference = null;
      updateReferenceBar();
      $('planLoading').hidden = true;
      draw();
    } catch (error) {
      plan.image = null;
      $('planLoading').hidden = true;
      $('planFailed').hidden = false;
      $('planFailedDetail').textContent = (error && error.message) || String(error);
    } finally {
      plan.loading = false;
    }
  }

  /* ------------------------------------------------ framing against a FITS */

  /* One of your own frames as the background, instead of a survey cutout.
   *
   * "Put the new mosaic where last spring's one was" is a question about a file
   * on disk. And a survey picture, however pretty, is not what your telescope
   * sees: a real frame shows the field at your focal length, through your
   * filters, with your gradients and your star shapes, which is what a framing
   * decision is actually made against.
   *
   * The frame is plate solved on the way in, so the overlay knows where it
   * points, how wide it is and which way up it was shot — the rectangle lands
   * on the real sky rather than on the pixels. */
  async function openFits() {
    const native = (window.pywebview && window.pywebview.api) || null;
    let path = '';
    if (native && native.pick_fits) {
      try {
        path = await native.pick_fits(plan.lastFitsDir || '');
      } catch (error) { app.toast(String(error), 'error'); return; }
      if (!path) return;
    } else {
      // In a browser there is no file chooser that yields a path the server can
      // open, so the path is typed. The server reads it, not the page — which
      // is also why this works from a tablet: the file is on the capture PC.
      path = await app.askForText(
        'Full path to a FITS frame on the capture PC',
        plan.lastFitsPath || '',
        { title: 'Open FITS', confirmLabel: 'Open' });
      if (!path) return;
    }

    plan.lastFitsPath = path;
    plan.lastFitsDir = path.replace(/[\\/][^\\/]*$/, '');
    $('planEmpty').hidden = true;
    $('planFailed').hidden = true;
    $('planLoading').hidden = false;
    $('planLoading').querySelector('p').textContent =
      'Reading and plate solving the frame…';

    // Polled rather than awaited: the last resort is astrometry.net, which can
    // take minutes, and a request held open that long times out in the browser
    // long before the answer comes back.
    let info;
    try {
      await app.api('/api/framing/reference', 'POST', { path });
      info = await waitForReference();
    } catch (error) {
      $('planLoading').hidden = true;
      $('planFailed').hidden = false;
      $('planFailedDetail').textContent = error.message;
      $('planLoading').querySelector('p').textContent = 'Fetching survey image…';
      return;
    }

    try {
      const image = await new Promise((resolve, reject) => {
        const element = new Image();
        element.onload = () => resolve(element);
        element.onerror = () => reject(new Error('the rendered frame could not be decoded'));
        element.src = `/api/framing/reference/image.png?token=${info.token}`;
      });
      plan.image = image;
      plan.imageCentre = { ra: raDegrees(info.ra), dec: info.dec };
      plan.imageFov = info.fovWidth;
      plan.imageRotation = info.rotation || 0;
      plan.imageFlipped = !!info.flipped;
      plan.reference = info;
      // Pull back far enough that the whole mosaic is on screen beside the
      // frame, with room to move it. A reference shot at 2″/px on a small chip
      // can be a fraction of the field being planned, and a view locked to the
      // picture would put the framing rectangle entirely outside the canvas.
      plan.viewFov = defaultViewFov();
      $('planFov').value = showFov(plan.viewFov);
      // Frame it where the frame is, so the rectangle starts on the field the
      // reference shows rather than wherever the last search left it.
      if (!plan.centre) {
        plan.centre = { ra: raDegrees(info.ra), dec: info.dec };
        plan.target = { name: info.object || info.filename,
                        ra: info.ra, dec: info.dec };
      }
      $('planLoading').hidden = true;
      updateReferenceBar();
      updateFieldReadout();
      updateCoverage();
      draw();
      const how = { header: 'read from its header', astap: 'solved by ASTAP',
        'astrometry.net': 'solved by astrometry.net' }[info.method] || 'solved';
      app.toast(`${info.filename} ${how}`
        + (info.seconds ? ` in ${info.seconds}s` : ''), 'success');
    } catch (error) {
      $('planLoading').hidden = true;
      $('planFailed').hidden = false;
      $('planFailedDetail').textContent = error.message;
    } finally {
      $('planLoading').querySelector('p').textContent = 'Fetching survey image…';
    }
  }

  /** Wait for the server to read and solve it, saying what it is up to.
   *
   *  The three ways it can be solved take a fraction of a second, twenty
   *  seconds and several minutes respectively, so the message matters: nobody
   *  minds waiting once they can see it is uploading to astrometry.net rather
   *  than hung. */
  async function waitForReference() {
    const started = Date.now();
    for (;;) {
      const status = await app.api('/api/framing/reference');
      if (status.detail) {
        const elapsed = Math.round((Date.now() - started) / 1000);
        $('planLoading').querySelector('p').textContent =
          `${status.detail}${elapsed > 4 ? `  (${elapsed}s)` : ''}`;
      }
      if (status.error) throw new Error(status.error);
      if (!status.busy && status.reference) return status.reference;
      if (!status.busy) throw new Error('the frame could not be opened');
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
  }

  /* How much sky to show around a framing, by default.
   *
   * Three times what the framing covers. The view exists to be dragged about in
   * — you are deciding where to put a rectangle, which means seeing what is
   * just outside it — and a picture cropped tight to the frame leaves nowhere
   * to move it to. Three is roughly the point where a target the same size as
   * the field has visible sky on every side of it without the frame becoming a
   * postage stamp.
   */
  const VIEW_MARGIN = 3.0;

  function defaultViewFov(objectArcmin = 0) {
    const framing = mosaicExtentDegrees() * VIEW_MARGIN;
    const object = objectArcmin ? (objectArcmin / 60) * 2.5 : 0;
    const reference = plan.reference ? plan.reference.fovWidth * 1.6 : 0;
    const wanted = Math.max(framing, object, reference);
    return wanted > 0 ? Math.max(0.3, Math.min(60, wanted)) : 3;
  }

  const showFov = (fov) => fov.toFixed(fov < 1 ? 3 : 2);

  /* Keep the view wide enough to hold the framing, without undoing a zoom.
   *
   * Only ever widens, and only to the point where the framing fits with a
   * little air around it — growing a mosaic from 2×2 to 4×4 must not leave half
   * of it off screen, but nor should it throw away a zoom the operator chose. */
  function refitReference() {
    if (!plan.reference) return;
    const needed = mosaicExtentDegrees() * 1.15;
    if (needed > (plan.viewFov || 0)) {
      plan.viewFov = Math.min(60, needed);
      $('planFov').value = showFov(plan.viewFov);
    }
  }

  /** How much sky the whole mosaic covers, across its widest side. */
  function mosaicExtentDegrees() {
    if (!plan.field || !plan.field.width) return 0;
    const stepX = plan.field.width * (1 - plan.overlap);
    const stepY = plan.field.height * (1 - plan.overlap);
    return Math.max(stepX * (plan.columns - 1) + plan.field.width,
      stepY * (plan.rows - 1) + plan.field.height);
  }

  function updateReferenceBar() {
    const bar = $('planReferenceBar');
    if (!bar) return;
    const info = plan.reference;
    bar.hidden = !info;
    $('btnPlanFitsClear').hidden = !info;
    if (!info) return;
    $('planReferenceName').textContent = info.object
      ? `${info.object} — ${info.filename}` : info.filename;
    const bits = [
      `${(info.fovWidth * 60).toFixed(1)}′ × ${(info.fovHeight * 60).toFixed(1)}′`,
      `${info.scale.toFixed(2)}″/px`,
      `${info.rotation.toFixed(1)}°`,
    ];
    if (info.flipped) bits.push('mirrored');
    if (info.filter) bits.push(info.filter);
    if (info.exposure) bits.push(`${info.exposure}s`);
    if (info.date) bits.push(info.date.slice(0, 10));
    // Where the solution came from: a header read and four minutes at
    // astrometry.net are not the same news about the same frame.
    bits.push({ header: 'from its header', astap: 'ASTAP',
      'astrometry.net': 'astrometry.net' }[info.method] || info.method);
    $('planReferenceDetail').textContent = bits.join('  ·  ');
  }

  /* --------------------------------------------------------------- state */

  function frame(object) {
    plan.target = { name: object.name || object.id || 'position' };
    plan.centre = { ra: object.ra, dec: object.dec };
    $('planEmpty').hidden = true;
    updateReadout();
    updateCoverage();
    updateFieldReadout();
    loadImage();
  }

  function resize() {
    const canvas = $('planCanvas');
    const stage = canvas.parentElement;
    const ratio = window.devicePixelRatio || 1;
    plan.canvas = canvas;
    plan.ctx = canvas.getContext('2d');
    plan.width = stage.clientWidth;
    plan.height = stage.clientHeight;
    canvas.width = Math.max(1, Math.round(plan.width * ratio));
    canvas.height = Math.max(1, Math.round(plan.height * ratio));
    plan.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw();
  }

  /* --------------------------------------------------------- target list */

  async function loadTargets() {
    try {
      const payload = await app.api('/api/targets');
      plan.targets = payload.targets;
    } catch (error) {
      console.error(error);
      return;
    }
    renderTargets();
  }

  function renderTargets() {
    const host = $('planTargetList');
    const planned = new Set(plan.planEntries.map((e) => e.targetId));
    $('planTargetCount').textContent = String(plan.targets.length);
    host.innerHTML = '';
    if (!plan.targets.length) {
      host.innerHTML = '<p class="muted small">Nothing saved yet. Frame something '
        + 'and press <b>Save as target</b>.</p>';
      return;
    }

    for (const target of plan.targets) {
      const row = document.createElement('div');
      row.className = `target-row${plan.selectedTarget === target.id ? ' active' : ''}`
        + (planned.has(target.id) ? ' planned' : '');
      // Dragged into tonight rather than added by a button, because the
      // question "what am I shooting tonight" is answered by moving things
      // from one list to the other, and that is what the gesture should be.
      row.draggable = true;
      row.dataset.targetId = target.id;
      row.addEventListener('dragstart', (event) => {
        plan.dragTarget = target.id;
        event.dataTransfer.effectAllowed = 'copy';
        // Firefox will not start a drag without something on the transfer.
        event.dataTransfer.setData('text/plain', target.id);
        row.classList.add('dragging');
      });
      row.addEventListener('dragend', () => {
        plan.dragTarget = null;
        row.classList.remove('dragging');
      });
      row.innerHTML = '<div class="name"></div><div class="kind"></div>'
        + '<div class="meta"></div><div class="actions"></div>';
      row.querySelector('.name').textContent = target.name;
      row.querySelector('.kind').textContent = target.type === 'mosaic'
        ? `${target.rows}×${target.columns}` : 'single';
      // Where it came from. A framing that appeared in the list on its own is
      // otherwise a mystery, and this one did: it was accepted on the Collab
      // tab, not drawn here.
      if (target.collab && target.collab.projectName) {
        const chip = document.createElement('span');
        chip.className = 'tag collab-tag';
        chip.textContent = 'collab';
        chip.title = `From “${target.collab.projectName}”`;
        row.querySelector('.kind').appendChild(chip);
      }
      row.querySelector('.meta').textContent =
        `${formatRa(raDegrees(target.ra))} ${formatDec(target.dec)}  PA ${target.rotation}°`
        + (target.type === 'mosaic' ? `  ${target.extent.width.toFixed(2)}°×${target.extent.height.toFixed(2)}°` : '');

      const actions = row.querySelector('.actions');
      const button = (label, className, handler) => {
        const element = document.createElement('button');
        element.className = `btn small ${className}`;
        element.textContent = label;
        element.addEventListener('click', (event) => { event.stopPropagation(); handler(); });
        actions.appendChild(element);
      };

      button('Load', 'ghost', () => {
        plan.selectedTarget = target.id;
        // A saved framing carries its own angle, which is a decision already
        // made — so it counts as set by hand and the camera angle leaves it be.
        setRotation(target.rotation);
        plan.rows = target.rows;
        plan.columns = target.columns;
        plan.overlap = target.overlap;
        plan.align = target.align === 'aligned' ? 'aligned' : 'fixed';
        $('planRows').value = target.rows;
        $('planCols').value = target.columns;
        $('planOverlap').value = Math.round(target.overlap * 100);
        $('planAlign').value = plan.align;
        frame({ name: target.name, ra: raDegrees(target.ra), dec: target.dec });
        renderTargets();
      });
      button('Delete', 'danger ghost', async () => {
        const ok = await app.confirmAction(
          `Delete “${target.name}” from the target list?`,
          { title: 'Delete target', confirmLabel: 'Delete', danger: true });
        if (!ok) return;
        try {
          await app.api(`/api/targets/${target.id}`, 'DELETE');
        } catch (error) { app.toast(error.message, 'error'); return; }
        await loadTargets();
      });

      row.addEventListener('click', () => {
        plan.selectedTarget = target.id;
        renderTargets();
      });
      host.appendChild(row);
    }
  }

  /* --------------------------------------------------------- tonight */

  /** Tonight's plan, as a list you drop things into.
   *
   *  The same plan the Plan tab shows and the sequencer runs — this is a second
   *  view of it, not a second copy. Everything here goes through the plan's own
   *  endpoints, so there is nothing to keep in step.
   */
  function renderTonight() {
    const host = $('planTonightList');
    if (!host) return;
    const byId = new Map(plan.targets.map((t) => [t.id, t]));
    $('planTonightCount').textContent = String(plan.planEntries.length);
    host.innerHTML = '';

    if (!plan.planEntries.length) {
      host.innerHTML = '<p class="muted small dock-empty">Drag a target or a '
        + 'collaboration chunk here.</p>';
      return;
    }

    for (const entry of plan.planEntries) {
      const target = byId.get(entry.targetId);
      const row = document.createElement('div');
      row.className = 'tonight-row';
      row.innerHTML = '<div class="name"></div>'
        + '<div class="times"></div>'
        + '<button class="btn small ghost danger remove">Remove</button>';
      const name = row.querySelector('.name');
      name.textContent = entry.name;
      if (target && target.collab && target.collab.projectName) {
        const chip = document.createElement('span');
        chip.className = 'tag collab-tag';
        chip.textContent = 'collab';
        chip.title = `From “${target.collab.projectName}”`;
        name.appendChild(chip);
      }

      const times = row.querySelector('.times');
      const from = document.createElement('input');
      const to = document.createElement('input');
      for (const [box, key] of [[from, 'startAt'], [to, 'endAt']]) {
        box.type = 'time';
        box.className = 'mono';
        box.value = entry[key] ? clockValue(entry[key]) : '';
        box.title = key === 'startAt' ? 'Start no earlier than'
          : 'Stop no later than';
        box.addEventListener('change', () => saveTimes(entry, from, to));
      }
      const dash = document.createElement('span');
      dash.className = 'muted';
      dash.textContent = '–';
      times.append(from, dash, to);
      if (entry.timesPinned) {
        const pin = document.createElement('span');
        pin.className = 'tag';
        pin.textContent = 'pinned';
        times.appendChild(pin);
      }

      row.querySelector('.remove').addEventListener('click', async () => {
        try {
          await app.api(`/api/plan/entries/${entry.id}`, 'DELETE');
        } catch (error) { app.toast(error.message, 'error'); return; }
        await loadPlan();
        renderTargets();
      });
      host.appendChild(row);
    }
  }

  /** A clock time from a moment, for the time boxes. */
  function clockValue(at) {
    const when = new Date(at * 1000);
    return `${String(when.getHours()).padStart(2, '0')}:`
      + `${String(when.getMinutes()).padStart(2, '0')}`;
  }

  /** ...and back. A time before noon belongs to the small hours of the night
   *  that started this evening, which is the same rule the rest of the program
   *  anchors to: at nine in the evening, "01:00" is four hours away. */
  function momentFrom(text) {
    const parts = String(text || '').split(':');
    if (parts.length !== 2) return null;
    const hour = Number(parts[0]);
    const minute = Number(parts[1]);
    if (!Number.isFinite(hour) || !Number.isFinite(minute)) return null;
    const now = new Date();
    const evening = new Date(now);
    if (now.getHours() < 12) evening.setDate(evening.getDate() - 1);
    const when = new Date(evening);
    if (hour < 12) when.setDate(when.getDate() + 1);
    when.setHours(hour, minute, 0, 0);
    return Math.round(when.getTime() / 1000);
  }

  async function saveTimes(entry, from, to) {
    const body = {};
    const start = momentFrom(from.value);
    const end = momentFrom(to.value);
    if (start === null) body.clearStart = true; else body.startAt = start;
    if (end === null) body.clearEnd = true; else body.endAt = end;
    try {
      await app.api(`/api/plan/entries/${entry.id}/times`, 'POST', body);
    } catch (error) {
      app.toast(error.message, 'error');
    }
    await loadPlan();
  }

  async function loadPlan() {
    try {
      const data = await app.api('/api/plan');
      plan.planEntries = ((data.plan || {}).entries || [])
        .filter((entry) => (entry.kind || 'target') === 'target');
      plan.night = data.night || null;
    } catch (error) {
      console.error(error);
      return;
    }
    renderTonight();
  }

  async function addToTonight(targetId) {
    if (!targetId) return;
    if (plan.planEntries.some((entry) => entry.targetId === targetId)) {
      app.toast('Already in tonight', 'info');
      return;
    }
    try {
      const answer = await app.api('/api/plan/entries', 'POST', { targetId });
      if (answer.clamped) {
        app.toast('Added — trimmed to the hours you said you could give',
          'warn');
      }
    } catch (error) {
      app.toast(error.message, 'error');
      return;
    }
    await loadPlan();
    renderTargets();
  }

  function bindDock() {
    const drop = $('planTonightList');
    if (!drop) return;
    drop.addEventListener('dragover', (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
      drop.classList.add('over');
    });
    drop.addEventListener('dragleave', () => drop.classList.remove('over'));
    drop.addEventListener('drop', (event) => {
      event.preventDefault();
      drop.classList.remove('over');
      const id = plan.dragTarget
        || event.dataTransfer.getData('text/plain');
      addToTonight(id);
    });

  }

  async function saveTarget() {
    if (!plan.centre) { app.toast('Frame something first', 'error'); return; }
    if (!plan.field || !plan.field.width) {
      app.toast('Set the focal length in Site & Optics first', 'error');
      return;
    }
    const suggestion = plan.target ? plan.target.name : 'Untitled';
    const panels = plan.rows * plan.columns;
    const name = await app.askForText(
      panels > 1
        ? `Name for this ${plan.rows}×${plan.columns} mosaic (${panels} panels)`
        : 'Name for this target',
      suggestion, { title: 'Save as target' });
    if (name === null) return;

    try {
      const payload = await app.api('/api/targets', 'POST', {
        name: name.trim() || suggestion,
        ra: raHours(plan.centre.ra),
        dec: plan.centre.dec,
        rotation: plan.rotation,
        panelWidth: plan.field.width,
        panelHeight: plan.field.height,
        rows: plan.rows,
        columns: plan.columns,
        overlap: plan.overlap,
        align: plan.align,
        survey: $('planSurvey').value,
      });
      plan.selectedTarget = payload.target.id;
      app.toast(`Saved “${payload.target.name}”`, 'success');
      $('planTargets').hidden = false;
      $('btnPlanTargets').classList.add('active');
      await loadTargets();
      resize();
      // Framed for a collaboration: hand the new target straight back to the
      // Collab tab, the way a drawn region is handed back for a mosaic.
      if (app.state.collabWantsTarget) {
        app.state.collabWantsTarget = false;
        app.state.collabTarget = payload.target;
        app.showTab('collab');
        app.toast(`“${payload.target.name}” is the collaboration’s target`, 'success');
      }
    } catch (error) {
      app.toast(error.message, 'error');
    }
  }

  /* ------------------------------------------------------------- search */

  let searchTimer = null;

  function bindSearch() {
    const input = $('planSearch');
    const results = $('planResults');

    input.addEventListener('input', () => {
      clearTimeout(searchTimer);
      const query = input.value.trim();
      if (!query) { results.hidden = true; return; }
      searchTimer = setTimeout(() => runSearch(query), 220);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { results.hidden = true; input.blur(); }
      if (event.key === 'Enter') {
        const first = results.querySelector('.sky-result');
        if (first) first.click();
      }
    });
    input.addEventListener('blur', () => setTimeout(() => { results.hidden = true; }, 160));
  }

  async function runSearch(query) {
    const results = $('planResults');
    let found;
    try {
      found = await app.api(`/api/catalog/search?q=${encodeURIComponent(query)}`);
    } catch (error) {
      results.innerHTML = '<div class="sky-result"><div class="meta">Search failed.</div></div>';
      results.hidden = false;
      return;
    }

    results.innerHTML = '';
    if (!found.results.length) {
      results.innerHTML = '<div class="sky-result"><div class="meta">Nothing found.</div></div>';
      results.hidden = false;
      return;
    }
    for (const raw of found.results) {
      const entry = { ...raw, ra: raDegrees(raw.ra) };
      const row = document.createElement('div');
      row.className = 'sky-result';
      row.innerHTML = '<div class="id"></div><div class="alt"></div><div class="meta"></div>';
      row.querySelector('.id').textContent = entry.id;
      row.querySelector('.alt').textContent = entry.size ? `${entry.size}′` : '';
      row.querySelector('.meta').textContent = [
        entry.name !== entry.id ? entry.name : null, entry.type, entry.constellation,
      ].filter(Boolean).join(' · ');

      row.addEventListener('mousedown', (event) => event.preventDefault());
      row.addEventListener('click', () => {
        results.hidden = true;
        $('planSearch').value = entry.id;
        // Open at a field that comfortably contains the object *and* leaves
        // room to move the framing about it.
        $('planFov').value = showFov(defaultViewFov(entry.size || 0));
        frame(entry);
      });
      results.appendChild(row);
    }
    results.hidden = false;
  }

  /* -------------------------------------------------------- interaction */

  function bindStage() {
    const canvas = $('planCanvas');
    const stage = canvas.parentElement;
    let lastX = 0, lastY = 0;

    canvas.addEventListener('mousedown', (event) => {
      if (event.button !== 0) return;
      // Drawing a region takes the drag over from moving the framing. The two
      // cannot share it: a drag that both moved the framing and drew a box
      // would do neither predictably.
      if (plan.regionMode) {
        if (!plan.imageCentre) return;
        const rect = canvas.getBoundingClientRect();
        const px = event.clientX - rect.left, py = event.clientY - rect.top;
        plan.regionDrag = { x0: px, y0: py, x1: px, y1: py };
        event.preventDefault();
        return;
      }
      if (!plan.centre) return;
      plan.dragging = true;
      lastX = event.clientX; lastY = event.clientY;
      stage.classList.add('dragging');
    });

    window.addEventListener('mouseup', () => {
      if (plan.regionDrag) {
        const drawn = regionFromDrag(plan.regionDrag);
        plan.regionDrag = null;
        // A click rather than a drag leaves whatever was there. Wiping a
        // carefully drawn region because somebody clicked the picture would be
        // the most annoying thing in here.
        if (drawn) plan.region = drawn;
        updateRegionBar();
        draw();
      }
      plan.dragging = false;
      stage.classList.remove('dragging');
    });

    canvas.addEventListener('mousemove', (event) => {
      const rect = canvas.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;

      if (plan.regionDrag) {
        plan.regionDrag.x1 = px;
        plan.regionDrag.y1 = py;
        const live = regionFromDrag(plan.regionDrag);
        if (live) {
          $('planRegionText').textContent =
            `${live.width.toFixed(2)}° × ${live.height.toFixed(2)}°`;
        }
        draw();
        return;
      }

      if (plan.dragging && plan.centre) {
        // Drag the framing across the picture; the picture itself stays put.
        // Through the picture's own axes, so the framing follows the cursor
        // rather than setting off at whatever angle the frame was shot at.
        const [east, north] = screenToOffset(event.clientX - lastX,
          event.clientY - lastY);
        lastX = event.clientX; lastY = event.clientY;
        const base = skyToOffset(plan.imageCentre.ra, plan.imageCentre.dec,
          plan.centre.ra, plan.centre.dec) || [0, 0];
        const moved = offsetToSky(plan.imageCentre.ra, plan.imageCentre.dec,
          base[0] + east, base[1] + north);
        plan.centre = { ra: moved[0], dec: moved[1] };
        updateReadout();
        draw();
        return;
      }

      const position = unproject(px, py);
      if (position) {
        $('planHudCursor').textContent =
          `${formatRa(position[0])}  ${formatDec(position[1])}`;
      }
    });

    canvas.addEventListener('dblclick', (event) => {
      const rect = canvas.getBoundingClientRect();
      const position = unproject(event.clientX - rect.left, event.clientY - rect.top);
      if (!position) return;
      plan.centre = { ra: position[0], dec: position[1] };
      updateReadout();
      draw();
    });

    canvas.addEventListener('wheel', (event) => {
      event.preventDefault();
      const current = Number($('planFov').value) || 3;
      const next = Math.max(0.05, Math.min(90, current * Math.exp(event.deltaY * 0.0012)));
      $('planFov').value = showFov(next);
      plan.touchedFov = true;
      scheduleReload();
    }, { passive: false });

    new ResizeObserver(resize).observe($('planWrap'));
  }

  /* Zooming means two different things depending on what is being shown.
   *
   * On a survey cutout it means "fetch a wider picture", because the picture is
   * generated to fit. On one of your own frames there is nothing to fetch — the
   * frame covers the sky it covers — so it means "stand further back", and the
   * frame stays put at its true angular size while more empty sky appears
   * around it. Sending that through the survey loader is what made zooming out
   * of a reference frame quietly replace it with DSS2. */
  function applyViewFov() {
    const fov = Math.max(0.05, Math.min(90, Number($('planFov').value) || 3));
    plan.viewFov = fov;
    draw();
  }

  let reloadTimer = null;
  function scheduleReload() {
    if (plan.reference) { applyViewFov(); return; }
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(loadImage, 350);
  }

  /* ----------------------------------------------------------- controls */

  function bindControls() {
    $('planRotation').addEventListener('input', (event) => setRotation(event.target.value));
    $('planRotationValue').addEventListener('change', (event) => setRotation(event.target.value));

    $('btnPlanMatchRotator').addEventListener('click', () => {
      const rotator = (app.state.status && app.state.status.devices.rotator) || {};
      if (!rotator.connected) { app.toast('No rotator connected', 'error'); return; }
      setRotation(rotator.position);
    });

    // The angle follows the camera on its own until the dial is turned; this is
    // how to get back to it afterwards, without reloading the page.
    $('btnPlanMatchCamera').addEventListener('click', () => {
      if (!plan.field || !Number.isFinite(plan.field.rotation)) {
        app.toast('No camera angle yet — plate solve a frame, or set one in '
          + 'Site & Optics', 'error');
        return;
      }
      plan.touchedRotation = false;
      setRotation(plan.field.rotation, { fromOperator: false });
      app.toast(`Framing set to ${plan.rotation.toFixed(1)}° `
        + `(${plan.field.source === 'solve' ? 'from the last plate solve'
          : 'from Site & Optics'})`, 'success');
    });

    const setGrid = () => {
      plan.rows = Math.max(1, Math.min(20, Number($('planRows').value) || 1));
      plan.columns = Math.max(1, Math.min(20, Number($('planCols').value) || 1));
      plan.overlap = Math.max(0, Math.min(0.8, (Number($('planOverlap').value) || 0) / 100));
      plan.align = $('planAlign').value === 'aligned' ? 'aligned' : 'fixed';
      // A bigger mosaic needs a wider view to stay on screen beside the frame
      // it is being planned against.
      refitReference();
      updateCoverage();
      draw();
    };
    ['planRows', 'planCols', 'planOverlap', 'planAlign'].forEach((id) => {
      $(id).addEventListener('change', setGrid);
      $(id).addEventListener('input', setGrid);
    });

    $('btnPlanLoad').addEventListener('click', loadImage);
    // Changing the survey always fetches; it is the whole point of the control.
    $('planSurvey').addEventListener('change', () => {
      plan.reference = null;
      plan.imageRotation = 0;
      plan.imageFlipped = false;
      updateReferenceBar();
      loadImage();
    });
    $('planFov').addEventListener('change', () => {
      plan.touchedFov = true;
      if (plan.reference) applyViewFov(); else loadImage();
    });
    // Retry whichever sort of picture was being shown.
    $('btnPlanRetry').addEventListener('click',
      () => (plan.reference ? openFits() : loadImage()));

    $('btnPlanFits').addEventListener('click', openFits);
    $('btnPlanFitsClear').addEventListener('click', () => {
      plan.reference = null;
      plan.imageRotation = 0;
      plan.imageFlipped = false;
      updateReferenceBar();
      loadImage();
    });
    $('btnPlanReferenceGoto').addEventListener('click', () => {
      const info = plan.reference;
      if (!info) return;
      plan.centre = { ra: raDegrees(info.ra), dec: info.dec };
      // Match the angle it was shot at, which is the point of framing against
      // it: the same field, the same way up. Deliberate, so it holds against
      // the camera angle.
      setRotation(info.rotation);
      updateFieldReadout();
    });

    $('btnPlanTargets').addEventListener('click', () => {
      const panel = $('planTargets');
      panel.hidden = !panel.hidden;
      $('btnPlanTargets').classList.toggle('active', !panel.hidden);
      resize();
      if (panel.hidden) return;
      // Anything joined elsewhere becomes a target here, so the list is the
      // whole truth rather than whatever this machine happened to see.
      app.api('/api/collab/adopt', 'POST').catch(() => {})
        .then(() => loadTargets());
      loadPlan();
    });

    $('btnPlanSave').addEventListener('click', saveTarget);

    // The whole list at once. A season's worth of tries, or somebody
    // else's list on a shared PC, is not worth deleting one at a time.
    $('btnClearTargets').addEventListener('click', async () => {
      const count = plan.targets.length;
      if (!count) { app.toast('The target list is already empty'); return; }
      const ok = await app.confirmAction(
        `Clear all ${count} target${count === 1 ? '' : 's'}? They come out of `
        + 'the plan too. What has been shot on them is kept, and a collaboration '
        + 'you have joined can be added back from the Collab tab.',
        { title: 'Clear the target list', confirmLabel: 'Clear all', danger: true });
      if (!ok) return;
      try {
        const answer = await app.api('/api/targets', 'DELETE');
        app.toast(`${answer.removed} target${answer.removed === 1 ? '' : 's'} removed`, 'success');
      } catch (error) { app.toast(error.message, 'error'); return; }
      plan.selectedTarget = null;
      await loadTargets();
    });

    $('btnPlanRegion').addEventListener('click', () => setRegionMode(!plan.regionMode));

    $('btnPlanRegionClear').addEventListener('click', () => {
      plan.region = null;
      plan.regionDrag = null;
      updateRegionBar();
      draw();
    });

    $('btnPlanRegionUse').addEventListener('click', () => {
      if (!plan.region) return;
      // Handed over rather than posted: the region is half of a project, and
      // the rules it will be judged by are the other half. The Collab tab asks
      // for those, with the shape already filled in.
      app.state.collabRegion = {
        ...plan.region,
        name: plan.target ? plan.target.name : '',
      };
      setRegionMode(false);
      app.showTab('collab');
      app.toast('Region sent to the Collab tab', 'success');
    });

    $('btnPlanGoto').addEventListener('click', async () => {
      if (!plan.centre) { app.toast('Frame something first', 'error'); return; }
      const mount = (app.state.status && app.state.status.devices.mount) || {};
      if (!mount.connected) { app.toast('Connect a mount first', 'error'); return; }
      const rotator = (app.state.status && app.state.status.devices.rotator) || {};
      const panels = panelCentres();
      const first = panels.length > 1 ? panels[0] : { ra: plan.centre.ra, dec: plan.centre.dec };
      const what = panels.length > 1 ? 'panel 1 of the mosaic' : 'this framing';

      const ok = await app.confirmAction(
        `Slew to ${what} — RA ${formatRa(first.ra)}, Dec ${formatDec(first.dec)}. `
        + (rotator.connected
          ? `The rotator will turn to position angle ${plan.rotation.toFixed(1)}°.`
          : 'No rotator is connected, so the angle will not be applied.'),
        { title: 'Go to framing', confirmLabel: 'Slew', danger: true });
      if (!ok) return;

      app.send('/api/solve', 'POST', {
        mode: 'goto',
        ra: raHours(first.ra),
        dec: first.dec,
        rotation: plan.rotation,
      }, 'Going to the framing');
    });
  }

  /** The Planetarium hands a selection over here. */
  window.planner = {
    frame(hours, decDeg, name, sizeArcmin) {
      app.showTab('planner');
      $('planFov').value = showFov(defaultViewFov(sizeArcmin || 0));
      $('planSearch').value = name || '';
      resize();
      frame({ name: name || 'position', ra: raDegrees(hours), dec: decDeg });
    },
  };

  /** What the Collab tab needs from here: turn the drawing mode on, so that
      "Draw it in the Planner" actually arrives ready to draw. */
  window.astroPlanner = {
    drawRegion() {
      resize();
      setRegionMode(true);
    },
    /** Frame one target for a collaboration: ordinary framing, and Save
        target brings it back to the Collab tab already chosen. */
    frameTarget() {
      resize();
      setRegionMode(false);
      app.state.collabWantsTarget = true;
      app.toast('Frame the target, then press Save target', 'info', 6000);
    },
  };

  function init() {
    bindControls();
    bindSearch();
    bindStage();
    bindDock();
    app.onStatus((status, tab) => {
      if (tab !== 'planner') return;
      resize();
      // The dock is only worth refreshing while it is open, and only on
      // arrival: rebuilding it on every tick would take the cursor out of a
      // time box somebody is typing into.
      if (!$('planTargets').hidden && !plan.planEntries.length) loadPlan();
      // Re-fetch as the night goes on, not only the first time. The field used
      // to be loaded once and kept for the life of the page, so solving and
      // syncing the rig updated the camera angle everywhere except the one
      // place you were looking at it.
      if (!plan.field || Date.now() - fieldCheckedAt > FIELD_POLL_MS) loadField();
      if (!plan.targets.length) loadTargets();
    });
  }

  document.addEventListener('DOMContentLoaded', init);
}());
