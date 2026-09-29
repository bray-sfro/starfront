/* The Collab tab: start a collaboration, and take part in one.
 *
 * Two things, and deliberately only two. What used to be here — enrolling
 * telescopes by hand, dealing chunks out to them, a ledger with override
 * buttons beside every night anybody had contributed — was machinery showing
 * through the front of the program. The server hands the sky out by itself and
 * the progress bars say how it is going, so none of it had to be on screen.
 *
 * Nothing here talks to the collaboration server directly. Every call goes to
 * this program, which holds the credentials and makes the request: the
 * coordinator token can rewrite every project on the server, and a credential
 * in a page is a credential in devtools.
 */
'use strict';

(function () {
  const app = window.astro;
  const $ = app.$;

  const col = {
    state: null,         // the last /api/collab payload
    filters: [],         // the filter rows on the new-collaboration form
    targets: [],         // your own targets, to aim a simple collaboration at
    settingsSynced: false,
    loading: false,
  };

  /* ------------------------------------------------------------- helpers */

  const esc = (text) => String(text === null || text === undefined ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  function num(id) {
    const raw = ($(id) && $(id).value || '').trim();
    if (!raw) return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  }

  const fmtRa = (deg) => {
    if (deg === null || deg === undefined) return '—';
    const hours = ((deg % 360) + 360) % 360 / 15;
    const h = Math.floor(hours);
    const m = Math.floor((hours - h) * 60);
    const s = Math.round(((hours - h) * 60 - m) * 60);
    return `${h}h ${String(m).padStart(2, '0')}m ${String(s).padStart(2, '0')}s`;
  };

  const fmtDec = (deg) => {
    if (deg === null || deg === undefined) return '—';
    const sign = deg < 0 ? '-' : '+';
    const a = Math.abs(deg);
    const d = Math.floor(a);
    const m = Math.round((a - d) * 60);
    return `${sign}${d}° ${String(m).padStart(2, '0')}'`;
  };

  const fmtRegion = (region) => {
    if (!region) return '—';
    return `${fmtRa(region.ra)} ${fmtDec(region.dec)}  `
      + `${Number(region.width).toFixed(2)}° × `
      + `${Number(region.height).toFixed(2)}°`;
  };

  const shape = () => ($('colShapeMosaic') && $('colShapeMosaic').checked
    ? 'mosaic' : 'one');

  /* ------------------------------------------------- the server, in a pill */

  function drawServer() {
    const status = col.state || {};
    const pill = $('colServerPill');
    const text = $('colServerText');
    if (!pill || !text) return;

    // The pill names the server only when it is not the built-in one: for
    // everybody else it is simply "taking part" or not.
    const custom = status.server && status.defaultServer && status.server !== status.defaultServer;
    const where = custom ? `${status.server} — ` : '';
    let label = 'not joined';
    let kind = '';
    // A token the server does not know is a token from another server - the
    // coordinator's old one on their own PC. Joining mints a fresh one.
    const stale = /401/.test(status.error || '');
    if (stale) { label = `${where}not joined here yet`; kind = 'busy'; }
    else if (status.error) { label = `${where}${status.error}`; kind = 'busy'; }
    else if (!status.configured) label = `${where}not joined`;
    else if (status.online) { label = `${where}taking part`; kind = 'on'; }
    else label = `${where}waiting for an answer`;
    pill.className = `pill ${kind}`;
    text.textContent = label;
    drawOnline();
    drawWho();
  }

  /** How many telescopes and people are about right now. */
  function drawOnline() {
    const node = $('colOnline');
    if (!node) return;
    const status = col.state || {};
    const who = status.presence || {};
    if (!status.configured || who.online === undefined) { node.textContent = ''; return; }
    const scopes = who.online || 0;
    const people = who.people || 0;
    node.textContent = `${scopes} telescope${scopes === 1 ? '' : 's'} online`
      + (people && people !== scopes ? ` · ${people} people` : '');
    const names = (who.telescopes || []).filter((t) => t.online)
      .map((t) => t.name + (t.target ? ` — ${t.target}` : '') + (t.state ? ` (${t.state})` : ''));
    node.title = names.length ? names.join('\n') : 'nobody has checked in lately';
    const share = $('colShare');
    if (share && status.sharePosition !== undefined) share.checked = status.sharePosition !== false;
  }

  /** Who this program is on the server, and the buttons that follow from it. */
  function drawWho() {
    const status = col.state || {};
    const person = status.person || {};
    const login = status.login || {};
    const name = $('colWhoName');
    const inBtn = $('btnColLogin');
    const outBtn = $('btnColLogout');
    const note = $('colWhoNote');
    const box = $('colJoinBox');
    if (!name || !inBtn || !outBtn) return;

    const scope = status.telescope || 'this telescope';
    const every = Math.round(status.pollMinutes || 10);
    const enrolled = status.enrolled
      ? `${scope} is enrolled and checks in every ${every} minutes.`
      : `${scope} is not enrolled yet.`;

    if (person.admin) {
      name.textContent = person.signedIn
        ? `Joined as ${person.name} · runs this server` : 'Runs this server';
      inBtn.hidden = !!person.signedIn;
      inBtn.textContent = 'Join with Discord';
      outBtn.hidden = !person.signedIn;
      note.textContent = `${enrolled} You can change or close any collaboration.`;
    } else if (person.signedIn) {
      name.textContent = `Joined as ${person.name || 'you'}`;
      inBtn.hidden = true;
      outBtn.hidden = false;
      note.textContent = enrolled + (person.canStart
        ? ' You can join collaborations below, and start your own.'
        : ' You can join collaborations below; starting one needs a role you do not hold.');
    } else {
      name.textContent = login.pending ? 'Waiting for the browser…' : 'Not joined yet';
      inBtn.hidden = false;
      inBtn.disabled = !!login.pending;
      inBtn.textContent = login.pending ? 'Open the browser again' : 'Join with Discord';
      outBtn.hidden = true;
      const serverOffersIt = status.serverAuth === undefined || status.serverAuth;
      note.textContent = serverOffersIt
        ? (login.pending
          ? 'Finish signing in on the page that opened; this will update by itself.'
          : 'One button: sign in with Discord and this telescope is enrolled. '
            + 'You need to be a member of the Discord server.')
        : 'This server has no Discord sign-in; its owner can give you a token '
          + 'under Advanced.';
    }
    if (box) box.classList.toggle('joined', !!(person.admin || person.signedIn));

    // Starting one needs a person who may.
    const start = $('colStartFold');
    if (start) start.hidden = !person.canStart;
  }

  /** Sign in with Discord: the browser does the talking, this side polls. */
  async function login() {
    const button = $('btnColLogin');
    button.disabled = true;
    try {
      const answer = await app.api('/api/collab/login', 'POST', {});
      if (!answer.opened) {
        app.toast(`Open this in a browser to sign in: ${answer.url}`, 'warn', 12000);
      }
      col.state = { ...(col.state || {}), login: { pending: true, url: answer.url } };
      drawWho();
      // Poll until the browser side is done. Every two seconds is plenty:
      // the person is switching windows and clicking, not racing.
      const started = Date.now();
      while (Date.now() - started < (answer.expiresIn || 600) * 1000) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const poll = await app.api('/api/collab/login/poll');
        if (poll.state === 'done') {
          const who = (poll.user || {}).name || 'you';
          app.toast(poll.enrolled
            ? `Joined as ${who} — ${(poll.enrolled || {}).name || 'this telescope'} is enrolled`
            : `Joined as ${who}`, 'success', 6000);
          col.settingsSynced = false;
          await app.loadSettings();
          syncSettings(app.state.settings);
          await load(true);
          return;
        }
        if (poll.state === 'expired' || poll.state === 'claimed' || poll.state === 'idle') {
          app.toast('The sign-in timed out — try again', 'warn');
          break;
        }
      }
    } catch (error) {
      app.toast(error.message, 'error');
    }
    button.disabled = false;
    await load(true);
  }

  async function logout() {
    try {
      col.state = await app.api('/api/collab/logout', 'POST', {});
      col.settingsSynced = false;
      await app.loadSettings();
      syncSettings(app.state.settings);
      app.toast('Signed out', 'success');
    } catch (error) {
      app.toast(error.message, 'error');
    }
    await load(true);
  }

  /* ------------------------------------------- 1. starting a collaboration */

  function drawTargetPick() {
    const pick = $('colTargetPick');
    const draw = $('btnColDraw');
    const box = $('colRegionBox');
    const text = $('colRegionText');
    const hint = $('colRegionHint');
    if (!pick) return;

    const mosaic = shape() === 'mosaic';
    pick.hidden = mosaic;
    draw.hidden = !mosaic;
    if ($('btnColFrame')) $('btnColFrame').hidden = mosaic;
    // A target just framed in the Planner for this: chosen, no dropdown to
    // find it in.
    if (app.state.collabTarget && !mosaic) {
      const made = app.state.collabTarget;
      app.state.collabTarget = null;
      if (!col.targets.some((t) => t.id === made.id)) col.targets.unshift(made);
      pick.innerHTML = '';
      for (const target of col.targets) pick.appendChild(new Option(target.name, target.id));
      pick.value = made.id;
      if (col.editing) col.regionChanged = true;
    }
    // Settings that only mean something with more than one panel are not
    // shown for a single target: everybody points at the same spot, and
    // "fewest frames per panel a night" is just the night.
    if ($('colRMinFramesBox')) $('colRMinFramesBox').hidden = !mosaic;

    // Editing: the region it already has stays unless a new one is chosen,
    // and the box says so rather than looking empty.
    if (col.editing && mosaic && app.state.collabRegion) col.regionChanged = true;
    if (col.editing && !col.regionChanged) {
      const current = col.editing.region || {};
      box.classList.add('picked');
      text.textContent = `${Number(current.width).toFixed(2)}° × `
        + `${Number(current.height).toFixed(2)}° as it is`;
      hint.textContent = `${fmtRa(current.ra)} ${fmtDec(current.dec)}`
        + '  — kept. Pick a target or draw a new rectangle to change it.';
      if (!mosaic) {
        pick.innerHTML = '';
        pick.appendChild(new Option('keep the sky it covers', ''));
        for (const target of col.targets) pick.appendChild(new Option(target.name, target.id));
      }
      return;
    }

    if (mosaic) {
      const region = app.state.collabRegion;
      box.classList.toggle('picked', !!region);
      if (region) {
        text.textContent = `${region.width.toFixed(2)}° × `
          + `${region.height.toFixed(2)}°`;
        hint.textContent = `${fmtRa(region.ra)} ${fmtDec(region.dec)}`
          + `  — cameras at PA ${Math.round(region.rotation)}°`;
        const name = $('colPName');
        if (name && !name.value && region.name) name.value = region.name;
      } else {
        text.textContent = 'Nothing drawn yet';
        hint.textContent = 'Open the Planner, find the object, and drag out the '
          + 'patch of sky this should cover.';
      }
      return;
    }

    // One target: everything needed was decided when it was framed.
    const chosen = pick.value;
    pick.innerHTML = '';
    if (!col.targets.length) {
      pick.appendChild(new Option('no targets saved yet', ''));
    }
    for (const target of col.targets) {
      pick.appendChild(new Option(target.name, target.id));
    }
    if (chosen) pick.value = chosen;

    const target = col.targets.find((t) => t.id === pick.value);
    box.classList.toggle('picked', !!target);
    if (!target) {
      text.textContent = 'Nothing chosen yet';
      hint.textContent = 'Pick one of your saved targets, or press Frame it in '
        + 'the Planner and save what you frame.';
      return;
    }
    const extent = target.extent || {};
    const across = extent.width || target.panelWidth || 0;
    const down = extent.height || target.panelHeight || 0;
    text.textContent = target.name;
    hint.textContent = `${fmtRa(target.ra * 15)} ${fmtDec(target.dec)}`
      + (across && down
        ? `  — ${across.toFixed(2)}° × ${down.toFixed(2)}°`
        : '  — one field of this camera');
    const name = $('colPName');
    if (name && !name.value) name.value = target.name;
  }

  function drawFilterRows() {
    const box = $('colPFilters');
    if (!box) return;
    if (!col.filters.length) {
      box.innerHTML = '<p class="small muted">Any filter will be accepted, and '
        + 'no depth goal is set.</p>';
      return;
    }
    box.innerHTML = col.filters.map((row, index) => `
      <div class="row gap">
        <input type="text" data-col-filter="name" data-index="${index}"
               value="${esc(row.name)}" placeholder="H" spellcheck="false">
        <input type="number" data-col-filter="bandpass" data-index="${index}"
               value="${row.bandpass === null ? '' : row.bandpass}"
               step="0.5" min="0" placeholder="nm, any">
        <input type="number" data-col-filter="hours" data-index="${index}"
               value="${row.hours === null ? '' : row.hours}"
               step="0.5" min="0" placeholder="hours wanted">
        <button class="btn small ghost danger" data-col-drop="${index}">Remove</button>
      </div>`).join('');

    box.querySelectorAll('[data-col-filter]').forEach((input) => {
      input.addEventListener('input', () => {
        const row = col.filters[Number(input.dataset.index)];
        if (!row) return;
        const field = input.dataset.colFilter;
        if (field === 'name') row.name = input.value;
        else row[field] = input.value === '' ? null : Number(input.value);
      });
    });
    box.querySelectorAll('[data-col-drop]').forEach((button) => {
      button.addEventListener('click', () => {
        col.filters.splice(Number(button.dataset.colDrop), 1);
        drawFilterRows();
      });
    });
  }

  async function createProject() {
    const name = ($('colPName').value || '').trim();
    if (!name) { app.toast('Give it a name', 'error'); return; }

    const body = { name, notes: ($('colPNotes').value || '').trim() };
    // Editing keeps the region it has unless a new one was chosen; starting
    // needs one either way.
    const editing = col.editing;
    const wantsRegion = !editing || col.regionChanged;
    if (wantsRegion && shape() === 'mosaic') {
      const region = app.state.collabRegion;
      if (!region) {
        app.toast('Draw the region in the Planner first', 'error');
        return;
      }
      body.region = {
        ra: region.ra, dec: region.dec,
        width: region.width, height: region.height,
        rotation: region.rotation || 0,
      };
    } else if (wantsRegion) {
      const chosen = $('colTargetPick').value;
      if (!chosen) { app.toast('Choose one of your targets', 'error'); return; }
      body.targetId = chosen;
    }

    const filters = {};
    const goals = {};
    for (const row of col.filters) {
      const filterName = (row.name || '').trim();
      if (!filterName) continue;
      filters[filterName] = row.bandpass === null ? null : row.bandpass;
      if (row.hours) goals[filterName] = row.hours;
    }
    const moon = num('colRMoonIll');
    const colourMoon = num('colRColourMoon');
    body.requirements = {
      minFocalLength: num('colRMinFocal'),
      maxFocalLength: num('colRMaxFocal'),
      maxHfr: num('colRMaxHfr'),
      minFramesPerVisit: num('colRMinFrames') || 10,
      maxGuideRms: num('colRMaxRms'),
      minExposure: num('colRMinExp'),
      maxExposure: num('colRMaxExp'),
      filters,
      // Typed as a percentage because that is how everybody reads it, stored
      // as a fraction because that is what the rules compare against.
      maxMoonIllumination: moon === null ? null : moon / 100,
      minMoonSeparation: num('colRMoonSep'),
      minAltitude: num('colRMinAlt'),
      requireCalibrated: $('colRCalibrated').checked,
      acceptColour: $('colRColour').checked,
      colourMaxMoon: colourMoon === null ? null : colourMoon / 100,
    };
    body.goals = goals;
    // One target or a region to cover. A saved mosaic target picked under
    // "one target" is still a region to cover; a single framing is one spot.
    if (shape() === 'mosaic') body.kind = 'mosaic';
    else if (wantsRegion) {
      const picked = col.targets.find((t) => t.id === $('colTargetPick').value);
      if (picked) body.kind = picked.type === 'single' ? 'single' : 'mosaic';
    }

    try {
      const answer = editing
        ? await app.api(`/api/collab/projects/${encodeURIComponent(editing.id)}/update`,
          'POST', body)
        : await app.api('/api/collab/projects', 'POST', body);
      app.toast(editing
        ? `“${name}” is updated — every rig on it finds out on its next poll`
        : `“${name}” is live — it is in the list below`, 'success', 6000);
      endEdit();
      $('colPName').value = '';
      $('colPNotes').value = '';
      col.filters = [];
      app.state.collabRegion = null;
      drawFilterRows();
      drawTargetPick();
      $('colStartFold').open = false;
      col.state = answer.state || col.state;
      drawBrowse();
      await load(true);
    } catch (error) {
      app.toast(error.message, 'error');
    }
  }

  /* --------------------------------------------- 2. taking part in one */

  /** Every collaboration on the server, yours included.
   *
   *  One button per row, because there is one decision: shoot some of this
   *  tonight. Behind it the program joins, turns the chunk it is given into a
   *  target and puts that target in the plan — three steps only in the sense
   *  that the program does three things.
   */
  function drawBrowse() {
    const box = $('colBrowse');
    if (!box) return;
    const status = col.state || {};
    const projects = status.open || [];
    const joined = new Set((status.tasks || []).map((task) => task.project));
    if ($('colOpenCount')) $('colOpenCount').textContent = String(projects.length);

    if (!status.configured) {
      box.innerHTML = '<p class="small muted">Press <b>Join with Discord</b> '
        + 'above and the collaborations appear here.</p>';
      return;
    }
    if (!projects.length) {
      box.innerHTML = '<p class="small muted">Nothing running on this server '
        + 'yet. Start one above.</p>';
      return;
    }

    box.innerHTML = projects.map((project) => {
      const mine = joined.has(project.id) || project.joined;
      const fit = project.compatibility || {};
      const wants = project.requirements || {};

      const limits = [];
      if (wants.minFocalLength && wants.maxFocalLength) {
        limits.push(`${wants.minFocalLength}–${wants.maxFocalLength} mm`);
      } else if (wants.minFocalLength) limits.push(`${wants.minFocalLength} mm or longer`);
      else if (wants.maxFocalLength) limits.push(`${wants.maxFocalLength} mm or shorter`);
      if (wants.minScale) limits.push(`${wants.minScale}"/px or coarser`);
      if (wants.maxScale) limits.push(`${wants.maxScale}"/px or finer`);
      if (wants.acceptColour === false) limits.push('mono cameras only');
      else if (wants.colourMaxMoon !== null && wants.colourMaxMoon !== undefined) {
        limits.push(`colour cameras with the Moon under ${Math.round(wants.colourMaxMoon * 100)}%`);
      }
      if (wants.maxHfr) limits.push(`stars within ${wants.maxHfr}"`);
      if (wants.maxGuideRms) limits.push(`guiding within ${wants.maxGuideRms}"`);
      // The project's rules for the night, which every rig on it follows.
      if (wants.minAltitude) limits.push(`above ${wants.minAltitude}°`);
      if (wants.minMoonSeparation) limits.push(`Moon ${wants.minMoonSeparation}° or more away`);
      if (wants.maxMoonIllumination !== null && wants.maxMoonIllumination !== undefined) {
        limits.push(`Moon under ${Math.round(wants.maxMoonIllumination * 100)}% lit`);
      }
      const named = Object.entries(wants.filters || {})
        .map(([name, nm]) => (nm ? `${name} ≤${nm}nm` : name)).join(', ');
      if (named) limits.push(named);

      // Depth per filter, across everybody. The question a participant has is
      // "is this nearly done, and does it still need me?", and nothing about
      // their own frames answers it.
      const goals = project.goals || {};
      const got = project.collected || {};
      const names = [...new Set([...Object.keys(goals),
        ...Object.keys(got)])].sort();
      const depth = names.map((name) => {
        const want = Number(goals[name] || 0);
        const have = Number(got[name] || 0);
        const share = want > 0 ? Math.min(100, have / want * 100) : (have ? 100 : 0);
        return `<span class="plan-filter-name">${esc(name)}</span>
          <div class="meter${share >= 100 ? ' met' : ''}"><div class="meter-fill"
               style="width:${share.toFixed(0)}%"></div></div>
          <span class="mono small">${have.toFixed(1)}${
            want ? ` / ${want.toFixed(0)}` : ''}h</span>`;
      }).join('');

      // With a rotator, joining is also a choice about the camera's angle:
      // turn it to the project's, so a single target is framed the way it
      // was framed and a mosaic's panels lie along the project's grid, or
      // leave it where it is. Without one there is nothing to choose.
      const pa = Number((project.region || {}).rotation || 0);
      const turn = (status.rotator && !mine)
        ? `<label class="inline small" title="Turn the rotator to the project's angle">
             <input type="checkbox" data-col-turn="${esc(project.id)}" checked>
             turn to ${pa.toFixed(0)}°</label>`
        : '';
      const action = fit.ok === false
        // Why, not just no. "Your scale is fine, your Ha is too wide" is
        // actionable; a greyed-out button is not.
        ? `<span class="small warn-text">${esc(fit.summary)}</span>`
        : `${turn}<button class="btn small primary" data-col-take="${esc(project.id)}">
             ${mine ? 'Add to tonight' : 'Take part tonight'}</button>`;

      // Yours to change. The server owner may change anything; a member may
      // change what they started, matched by Discord id. The server enforces
      // it either way - this only offers the buttons to somebody who could
      // use them.
      const person = status.person || {};
      const owned = person.admin
        || (person.signedIn && project.ownerId && project.ownerId === person.id);
      const owner = owned
        ? `<button class="btn small ghost" data-col-edit="${esc(project.id)}">Edit</button>
           <button class="btn small ghost danger" data-col-close="${esc(project.id)}">Close</button>`
        : '';

      return `<div class="col-open${mine ? ' joined' : ''}">
        <canvas class="col-sky" data-col-sky="${esc(project.id)}" width="240" height="160"
                title="${esc(project.name)} on the sky survey"></canvas>
        <div class="col-open-body">
          <div class="row gap">
            <b class="grow">${esc(project.name)}</b>
            ${mine ? '<span class="tag">joined</span>' : ''}
            ${owner}
            ${action}
          </div>
          <div class="small mono muted">${esc(fmtRegion(project.region))}${
            project.kind === 'single' ? '  ·  one target' : '  ·  mosaic'}${
            project.coordinator ? `  ·  ${esc(project.coordinator)}` : ''}</div>
          <div class="small col-people" title="${esc((project.participantNames || []).join(', '))}">${
            project.participants
              ? `${project.participants} telescope${project.participants === 1 ? '' : 's'} on it`
                + (project.participantsOnline ? ` · ${project.participantsOnline} online now` : '')
              : 'nobody on it yet'}</div>
          ${project.notes ? `<p class="small">${esc(project.notes)}</p>` : ''}
          ${limits.length
            ? `<div class="small muted">Takes: ${esc(limits.join(', '))}</div>` : ''}
          ${depth ? `<div class="depth-grid">${depth}</div>` : ''}
        </div>
      </div>`;
    }).join('');

    // The sky each one covers, drawn on the survey so a collaboration can be
    // told at a glance - the Veil is the Veil before a word is read.
    box.querySelectorAll('[data-col-sky]').forEach((canvas) => {
      const project = projects.find((p) => p.id === canvas.dataset.colSky);
      if (project) drawSky(canvas, project);
    });

    box.querySelectorAll('[data-col-take]').forEach((button) => {
      button.addEventListener('click', () => take(button, button.dataset.colTake));
    });
    box.querySelectorAll('[data-col-edit]').forEach((button) => {
      button.addEventListener('click', () => beginEdit(
        projects.find((p) => p.id === button.dataset.colEdit)));
    });
    box.querySelectorAll('[data-col-close]').forEach((button) => {
      button.addEventListener('click', () => closeProject(
        projects.find((p) => p.id === button.dataset.colClose)));
    });
  }

  /** A collaboration's patch of sky on the survey, with its rectangle drawn.
   *
   *  The same cutout the Plan tab's framing uses, at thumbnail size, and the
   *  project's north-up rectangle over it. Cached by the browser for a day,
   *  so a list of ten costs ten small fetches once. The picture is the useful
   *  half and the overlay does not need it, so an offline rig still gets the
   *  rectangle over a dark frame.
   */
  const skyCache = new Map();

  function drawSky(canvas, project) {
    const region = project.region || {};
    const width = Number(region.width) || 0;
    const height = Number(region.height) || 0;
    if (!(width > 0 && height > 0) || region.ra === undefined) return;
    const W = canvas.width, H = canvas.height;
    // The picture's width on the sky: the rectangle with room around it,
    // sized so a tall region fits the 3:2 frame too.
    const fov = Math.min(60, Math.max(0.3, Math.max(width, height * W / H) * 1.6));
    const scale = W / fov;
    const raHours = ((Number(region.ra) % 360) + 360) % 360 / 15;
    const url = `/api/survey/image?ra=${raHours.toFixed(6)}&dec=${Number(region.dec).toFixed(6)}`
      + `&fov=${fov.toFixed(4)}&width=${W * 2}&height=${H * 2}`;

    const paint = (image) => {
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#05070d';
      ctx.fillRect(0, 0, W, H);
      if (image) ctx.drawImage(image, 0, 0, W, H);
      const w = width * scale, h = height * scale;
      ctx.save();
      ctx.translate(W / 2, H / 2);
      ctx.fillStyle = 'rgba(126, 231, 165, 0.10)';
      ctx.fillRect(-w / 2, -h / 2, w, h);
      ctx.strokeStyle = 'rgba(0, 0, 0, 0.8)';
      ctx.lineWidth = 3;
      ctx.strokeRect(-w / 2, -h / 2, w, h);
      ctx.strokeStyle = 'rgba(126, 231, 165, 0.95)';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([5, 4]);
      ctx.strokeRect(-w / 2, -h / 2, w, h);
      ctx.restore();
      // A compass, small: north-up east-left is a convention worth stating.
      ctx.fillStyle = 'rgba(219, 227, 238, 0.7)';
      ctx.font = '9px ui-monospace, monospace';
      ctx.textAlign = 'center';
      ctx.fillText('N', W - 12, 11);
      ctx.fillText('E', 8, H - 5);
    };

    paint(skyCache.get(url) || null);
    if (skyCache.has(url)) return;
    const image = new Image();
    image.onload = () => { skyCache.set(url, image); if (canvas.isConnected) paint(image); };
    image.onerror = () => { skyCache.set(url, null); };
    image.src = url;
  }

  /** Whether this program may start collaborations: the Start fold is only
   *  shown when it may, so its visibility is the answer. */
  const canCoordinate = () => !!($('colStartFold') && !$('colStartFold').hidden);

  /* ------------------------------------------ changing one you started */

  /** Open the Start form on an existing collaboration, filled in.
   *
   *  The same form, because it is the same set of decisions — only now the
   *  region is kept as it is unless a new one is chosen, and the button says
   *  what it will do. A coordinator learns as the season goes: the Ha limit
   *  was too strict, the goal was reached, the notes were wrong. Starting a
   *  new collaboration for each of those would lose everything collected.
   */
  function beginEdit(project) {
    if (!project) return;
    col.editing = project;
    col.regionChanged = false;
    const wants = project.requirements || {};
    const set = (id, value) => { if ($(id)) $(id).value = value === null || value === undefined ? '' : value; };
    set('colPName', project.name || '');
    set('colPNotes', project.notes || '');
    set('colRMinFocal', wants.minFocalLength);
    set('colRMaxFocal', wants.maxFocalLength);
    if ($('colRColour')) $('colRColour').checked = wants.acceptColour !== false;
    set('colRColourMoon', wants.colourMaxMoon === null || wants.colourMaxMoon === undefined
      ? '' : Math.round(wants.colourMaxMoon * 100));
    if ($('colShapeMosaic') && $('colShapeOne')) {
      $('colShapeMosaic').checked = project.kind !== 'single';
      $('colShapeOne').checked = project.kind === 'single';
    }
    set('colRMaxHfr', wants.maxHfr);
    set('colRMinFrames', wants.minFramesPerVisit);
    set('colRMaxRms', wants.maxGuideRms);
    set('colRMinExp', wants.minExposure);
    set('colRMaxExp', wants.maxExposure);
    set('colRMoonIll', wants.maxMoonIllumination === null || wants.maxMoonIllumination === undefined
      ? '' : Math.round(wants.maxMoonIllumination * 100));
    set('colRMoonSep', wants.minMoonSeparation);
    set('colRMinAlt', wants.minAltitude);
    if ($('colRCalibrated')) $('colRCalibrated').checked = !!wants.requireCalibrated;
    const goals = project.goals || {};
    col.filters = Object.entries(wants.filters || {}).map(([name, nm]) => ({
      name, bandpass: nm === null || nm === undefined ? null : nm,
      hours: goals[name] === undefined ? null : goals[name],
    }));
    for (const [name, hours] of Object.entries(goals)) {
      if (!col.filters.some((row) => row.name === name)) {
        col.filters.push({ name, bandpass: null, hours });
      }
    }
    drawFilterRows();
    drawTargetPick();
    if ($('btnColCreate')) $('btnColCreate').textContent = 'Save changes';
    const fold = $('colStartFold');
    if (fold) { fold.open = true; fold.scrollIntoView({ block: 'start' }); }
  }

  function endEdit() {
    col.editing = null;
    col.regionChanged = false;
    if ($('btnColCreate')) $('btnColCreate').textContent = 'Start it';
  }

  async function closeProject(project) {
    if (!project) return;
    const ok = await app.confirmAction(
      `Close “${project.name}”? It comes off everybody's list. What has been `
      + 'collected is kept.',
      { title: 'Close collaboration', confirmLabel: 'Close it', danger: true });
    if (!ok) return;
    try {
      const answer = await app.api(
        `/api/collab/projects/${encodeURIComponent(project.id)}/update`, 'POST',
        { status: 'closed' });
      col.state = answer.state || col.state;
      app.toast(`“${project.name}” is closed`, 'success');
      await load(true);
    } catch (error) {
      app.toast(error.message, 'error');
    }
  }

  async function take(button, projectId) {
    button.disabled = true;
    const was = button.textContent;
    button.textContent = 'Working…';
    const turn = document.querySelector(`[data-col-turn="${CSS.escape(projectId)}"]`);
    try {
      const answer = await app.api(
        `/api/collab/projects/${encodeURIComponent(projectId)}/take`, 'POST',
        { matchRotation: turn ? turn.checked : true });
      col.state = answer;
      const target = answer.target || {};
      const panels = (target.rows || 1) * (target.columns || 1);
      app.toast(`“${target.name}” is in tonight's plan`
        + (panels > 1 ? ` as ${target.rows}×${target.columns} panels` : '')
        + '. Set the hours it gets on the Plan tab.',
        'success', 7000);
      drawBrowse();
      drawTask();
    } catch (error) {
      app.toast(error.message, 'error');
      button.disabled = false;
      button.textContent = was;
    }
  }

  /** What this telescope has been asked to do that it did not ask for.
   *
   *  A coordinator can still hand a chunk to a particular rig, and that is the
   *  one case where something arrives needing an answer. Anything joined from
   *  the list above is already accepted and says nothing here.
   */
  function drawTask() {
    const box = $('colTask');
    if (!box) return;
    const status = col.state || {};
    const task = status.task;
    if (!task || task.state !== 'offered') { box.innerHTML = ''; return; }

    const checks = ((status.compatibility || {}).checks || []).map((check) => {
      const mark = check.ok === true ? 'ok' : (check.ok === false ? 'no' : 'unknown');
      const glyph = check.ok === true ? '✓' : (check.ok === false ? '✗' : '?');
      return `<li class="col-check col-${mark}"><b>${glyph}</b> ${esc(check.check)}:
              ${esc(check.detail)}</li>`;
    }).join('');

    box.innerHTML = `<div class="plan-box col-offered">
      <div class="row gap">
        <b class="grow">Offered to this telescope</b>
        <span class="tag">${esc(task.projectName || 'a project')}</span>
      </div>
      <div class="small mono muted">${esc(fmtRegion(task.region))}</div>
      ${task.note ? `<p class="small">${esc(task.note)}</p>` : ''}
      ${checks ? `<ul class="col-checks">${checks}</ul>` : ''}
      <div class="row gap">
        <span class="spacer"></span>
        <button class="btn small primary" data-col-task="accepted">Accept</button>
        <button class="btn small ghost" data-col-task="declined">Decline</button>
      </div></div>`;

    box.querySelectorAll('[data-col-task]').forEach((button) => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          col.state = await app.api('/api/collab/task', 'POST',
            { state: button.dataset.colTask });
          const made = (col.state.adopted || {}).target;
          app.toast(made
            ? `Accepted — “${made.name}” is in your target list`
            : 'Declined', 'success');
          drawTask();
          drawBrowse();
        } catch (error) {
          app.toast(error.message, 'error');
          button.disabled = false;
        }
      });
    });
  }

  /* ------------------------------------------------------------- settings */

  /* There are no settings on this tab. The server is built in, joining
     enrols the telescope, the check-in is every ten minutes, and how long a
     collaboration gets is decided on its box on the Plan tab. Anybody who
     runs their own server edits settings.json by hand. */
  function syncSettings() {}

  /* ------------------------------------------------------------- loading */

  async function load(force = false) {
    if (col.loading && !force) return;
    col.loading = true;
    try {
      col.state = await app.api('/api/collab');
    } catch (error) {
      console.error(error);
    }
    try {
      const listing = await app.api('/api/targets');
      col.targets = listing.targets || [];
    } catch (error) {
      console.error(error);
    }
    col.loading = false;
    drawServer();
    drawBrowse();
    drawTask();
    drawTargetPick();
  }

  /* ---------------------------------------------------------------- wiring */

  function bind() {
    const on = (id, event, handler) => {
      const node = $(id);
      if (node) node.addEventListener(event, handler);
    };
    on('colShare', 'change', async () => {
      const share = $('colShare').checked;
      try {
        await app.api('/api/settings/collab', 'POST', { sharePosition: share });
        app.toast(share ? 'The others can see where this telescope points'
          : 'This telescope no longer shares its position', 'info');
      } catch (error) { app.toast(error.message, 'error'); }
    });

    on('btnColLogin', 'click', login);
    on('btnColLogout', 'click', logout);
    on('btnColCreate', 'click', createProject);
    on('btnColAddFilter', 'click', () => {
      col.filters.push({ name: '', bandpass: null, hours: null });
      drawFilterRows();
    });
    on('btnColRefresh', 'click', async () => {
      const button = $('btnColRefresh');
      button.disabled = true;
      endEdit();
      try {
        await app.api('/api/collab/poll', 'POST');
      } catch (error) {
        app.toast(error.message, 'error');
      }
      await load(true);
      button.disabled = false;
    });
    on('colShapeOne', 'change', drawTargetPick);
    on('colShapeMosaic', 'change', drawTargetPick);
    on('colTargetPick', 'change', () => {
      // Choosing a target while editing is choosing a new region.
      if (col.editing && $('colTargetPick').value) col.regionChanged = true;
      drawTargetPick();
    });
    on('btnColDraw', 'click', () => {
      app.showTab('planner');
      if (window.astroPlanner && window.astroPlanner.drawRegion) {
        window.astroPlanner.drawRegion();
      }
    });
    on('btnColFrame', 'click', () => {
      app.showTab('planner');
      if (window.astroPlanner && window.astroPlanner.frameTarget) {
        window.astroPlanner.frameTarget();
      }
    });

    drawFilterRows();
    drawTargetPick();
  }

  /* Loaded on arrival, not on every status tick: rebuilding these forms two
     and a half times a second would reset a half-typed name, which is a bug
     this program has already had once. */
  let arrived = false;
  app.onStatus((status, tab) => {
    syncSettings(app.state.settings);
    if (tab !== 'collab') { arrived = false; return; }
    if (arrived) return;
    arrived = true;
    drawTargetPick();
    load(true);
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
