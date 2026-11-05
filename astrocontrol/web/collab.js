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
    // Cards opened with a double-click, by project id, and what each has
    // fetched about who contributed: {at, data} or {at, error}.
    expanded: new Set(),
    people: new Map(),
    lastRefresh: 0,
    // What has happened lately across the open collaborations: the feed
    // behind the live strip and the "today" figures on each card.
    activity: null,
  };

  /* How often the open collaborations are re-read while the tab is showing.
     Progress moves as frames are reported, a few times an hour, so a
     minute is plenty and a page left open overnight keeps up. */
  const REFRESH_SECONDS = 60;

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

    const body = { name, notes: ($('colPNotes').value || '').trim(),
                   collectFiles: !!($('colPCollect') && $('colPCollect').checked) };
    // Editing keeps the region it has unless a new one was chosen; starting
    // needs one either way.
    const editing = col.editing;
    // Where the lights are stacked. A new collaboration always says so (the
    // form starts on the server's default); an edit sends it only when it
    // was changed, so a binding nobody touched is never rewritten.
    const wanted = stackingChoice();
    if (wanted.error) {
      stackMessage(wanted.error, true);
      app.toast(wanted.error, 'error');
      return;
    }
    stackMessage('');
    if (!editing || stackingChanged(wanted.value)) {
      body.stacking = wanted.value;
    }
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
      // A colour camera's data is its own band, taken only when the
      // project lists it: ticking a box puts the band among the filters.
      acceptOscRgb: $('colROscRgb').checked,
      acceptOscNarrowband: $('colROscNb').checked,
      acceptColour: $('colROscRgb').checked || $('colROscNb').checked,
      colourMaxMoon: colourMoon === null ? null : colourMoon / 100,
      maxBortle: num('colRMaxBortle'),
    };
    if ($('colROscRgb').checked && !('OSC-RGB' in filters)) filters['OSC-RGB'] = null;
    if ($('colROscNb').checked && !('OSC-Ha+OIII' in filters)) filters['OSC-Ha+OIII'] = null;
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
      if ($('colPCollect')) $('colPCollect').checked = false;
      resetStacking();
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
  /** A person's picture as a small round face, or their initial. */
  function face(person, extra = '', title = '') {
    const cls = `col-face${extra ? ` ${extra}` : ''}`;
    const name = person.ownerName || person.person || person.name || '?';
    const hint = title || name;
    return person.avatar
      ? `<img class="${cls}" src="${esc(person.avatar)}" alt="" title="${esc(hint)}" loading="lazy">`
      : `<span class="${cls} blank" title="${esc(hint)}">${esc(name.slice(0, 1).toUpperCase())}</span>`;
  }

  /** One face per person among a list of telescopes, the shooting ones ringed. */
  function faces(telescopes) {
    const seen = new Map();
    for (const t of telescopes) {
      const key = t.ownerName || t.name || t.id;
      const shooting = ['imaging', 'exposing'].includes(t.state || '');
      const held = seen.get(key);
      if (!held) {
        seen.set(key, { ...t, shooting, hint: `${t.ownerName || t.name}${t.ownerName ? ` · ${t.name}` : ''}${t.target ? ` — ${t.target}` : ''}` });
      } else if (shooting && !held.shooting) {
        held.shooting = true;
      }
    }
    return [...seen.values()].map((p) => face(p, p.shooting ? 'shooting' : '', p.hint)).join('');
  }

  /** The night as it is happening: who is on the sky, who is shooting,
   *  and the frames landing, scrolling past. The list of collaborations
   *  below is the ledger; this is the reason to open the tab. */
  function drawLive() {
    const node = $('colLive');
    if (!node) return;
    const status = col.state || {};
    const who = status.presence || {};
    if (!status.configured || who.online === undefined) { node.hidden = true; return; }
    const online = (who.telescopes || []).filter((t) => t.online);
    const shooting = online.filter((t) => ['imaging', 'exposing'].includes(t.state || ''));
    const people = who.people || 0;
    const feed = ((col.activity || {}).events || []).slice(0, 14);
    const now = Number((col.activity || {}).serverTime) || (Date.now() / 1000);
    const lines = feed.map((e) => {
      const whoDid = esc(e.person || e.telescope || 'somebody');
      if (e.kind === 'frames') {
        return `<span class="col-ticker-item">${esc(ago(now - e.at))} · <b>${whoDid}</b> put `
          + `${e.frames} × ${esc(e.filter)} on <b>${esc(e.projectName)}</b> (${(e.seconds / 3600).toFixed(1)} h)</span>`;
      }
      return `<span class="col-ticker-item">${esc(ago(now - e.at))} · <b>${whoDid}</b> joined `
        + `<b>${esc(e.projectName)}</b>${e.telescope ? ` with ${esc(e.telescope)}` : ''}</span>`;
    });
    const count = `${online.length} telescope${online.length === 1 ? '' : 's'} on the sky`
      + (people ? ` · ${people} ${people === 1 ? 'person' : 'people'}` : '')
      + (shooting.length ? ` · <b class="shooting-count">${shooting.length} shooting right now</b>` : '');
    const quiet = !online.length && !lines.length;
    node.hidden = false;
    node.className = `col-live${shooting.length ? ' busy' : ''}`;
    node.innerHTML = `
      <div class="col-live-head"><span class="live-dot"></span><b>Live</b>
        <span class="col-live-count">${quiet ? 'quiet for the moment — the first telescope out tonight starts the party' : count}</span></div>
      ${online.length ? `<div class="col-faces">${faces(online)}</div>` : ''}
      ${lines.length ? `<div class="col-ticker" title="The latest frames and joins across every collaboration">
          <div class="col-ticker-track" style="animation-duration:${Math.max(40, lines.length * 7)}s">${lines.join('')}${lines.join('')}</div>
        </div>` : ''}`;
  }

  function drawBrowse() {
    const box = $('colBrowse');
    if (!box) return;
    const status = col.state || {};
    const projects = status.open || [];
    const joined = new Set((status.tasks || []).map((task) => task.project));
    if ($('colOpenCount')) $('colOpenCount').textContent = String(projects.length);
    drawLive();
    const who = status.presence || {};
    const today = (col.activity || {}).projects || {};

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
      const osc = [];
      if (wants.acceptColour !== false && wants.acceptOscRgb !== false) osc.push('OSC-RGB');
      if (wants.acceptColour !== false && wants.acceptOscNarrowband !== false) osc.push('OSC narrowband');
      if (!osc.length) limits.push('mono cameras only');
      else {
        limits.push(`takes ${osc.join(' and ')}`);
        if (wants.colourMaxMoon !== null && wants.colourMaxMoon !== undefined) {
          limits.push(`colour cameras with the Moon under ${Math.round(wants.colourMaxMoon * 100)}%`);
        }
      }
      if (wants.maxBortle) limits.push(`Bortle ${wants.maxBortle} or darker`);
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
      // The bar is how much of the *field* is at the goal depth, when the
      // server says: on a mosaic a total of hours means nothing, since ten
      // hours on one panel of fifteen is one fifteenth of the field done.
      const goals = project.goals || {};
      const got = project.collected || {};
      const progress = project.progress || {};
      const names = [...new Set([...Object.keys(goals),
        ...Object.keys(got)])].sort();
      const depth = names.map((name) => {
        const want = Number(goals[name] || 0);
        const have = Number(got[name] || 0);
        const prog = progress[name];
        // Done is the field's depth against the goal, averaged over the
        // region with every point capped at its goal: ten hours on one panel
        // of fifteen is a fifteenth done, and one hundred means every part
        // of the sky has its goal.
        const share = prog ? Number(prog.average || 0) * 100
          : (want > 0 ? Math.min(100, have / want * 100) : (have ? 100 : 0));
        const text = prog
          ? `${share.toFixed(0)}% done · ${have.toFixed(1)}h collected`
          : `${have.toFixed(1)}${want ? ` / ${want.toFixed(0)}` : ''}h`;
        const hint = prog
          ? ` title="${(Number(prog.atGoal || 0) * 100).toFixed(0)}% of the field at the full ${
            want.toFixed(0)}h; the thinnest part has ${(Number(prog.thinnest || 0) * 100).toFixed(0)}%"`
          : '';
        return `<span class="plan-filter-name">${esc(name)}</span>
          <div class="meter${share >= 100 ? ' met' : ''}"><div class="meter-fill"
               style="width:${share.toFixed(0)}%"></div></div>
          <span class="mono small"${hint}>${esc(text)}</span>`;
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
      // Who is on it right now, and what the last day brought: the pulse of
      // the thing, above the ledger. A card with telescopes shooting on it
      // glows; the button to join one that is moving tonight glows too.
      const onIt = (who.telescopes || []).filter((t) => t.online && t.project === project.id);
      const shooting = onIt.filter((t) => ['imaging', 'exposing'].includes(t.state || ''));
      const day = today[project.id] || {};
      const prog = project.progress || {};
      const thin = Object.entries(prog)
        .map(([name, p]) => [name, Number((p || {}).average || 0)])
        .sort((a, b) => a[1] - b[1])[0];
      const nowText = shooting.length
        ? `<b>${shooting.length} shooting right now</b>${onIt.length > shooting.length ? ` · ${onIt.length - shooting.length} more on it` : ''}`
        : (onIt.length ? `${onIt.length} telescope${onIt.length === 1 ? '' : 's'} on it right now` : 'nobody on it right now');
      const chips = [
        day.hoursToday ? `<span class="col-chip today">+${Number(day.hoursToday).toFixed(1)} h in the last day${
          day.peopleToday ? ` · ${day.peopleToday} ${day.peopleToday === 1 ? 'person' : 'people'}` : ''}</span>` : '',
        thin && Object.keys(prog).length > 1
          ? `<span class="col-chip needs" title="The filter the field is thinnest in: tonight's deals go there first">needs ${esc(thin[0])} most</span>` : '',
      ].filter(Boolean).join('');
      const live = `<div class="col-now">${onIt.length ? `<div class="col-faces">${faces(onIt)}</div>` : ''}
        <span>${nowText}</span>${chips}</div>`;

      const action = fit.ok === false
        // Why, not just no. "Your scale is fine, your Ha is too wide" is
        // actionable; a greyed-out button is not.
        ? `<span class="small warn-text">${esc(fit.summary)}</span>`
        : `${turn}<button class="btn small primary${!mine && (shooting.length || day.hoursToday) ? ' glow' : ''}" data-col-take="${esc(project.id)}">
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
           <button class="btn small ghost" data-col-redeal="${esc(project.id)}"
                   title="Forget tonight's lists so every telescope is dealt afresh on its next poll - after changing the filters or goals">Re-deal tonight</button>
           <button class="btn small ghost danger" data-col-close="${esc(project.id)}">Close</button>
           ${(project.stacking || {}).backend === 'hub' || (project.stacking || {}).managed
             ? `<button class="btn small ghost danger" data-col-delete="${esc(project.id)}"
                  title="Restorable for seven days; then the raw frames are purged">Delete</button>` : ''}`
        : '';

      const open = col.expanded.has(project.id);
      return `<div class="col-open${mine ? ' joined' : ''}${open ? ' open' : ''}${shooting.length ? ' live' : ''}" data-col-id="${esc(project.id)}"
                   title="Double-click for the depth map and who has contributed">
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
          ${project.filesUrl
            ? `<p class="small">Collects the lights: upload yours at <a href="${esc(project.filesUrl)}" target="_blank" rel="noopener">${esc(project.filesUrl.replace(/^https?:\/\//, '').replace(/#.*$/, ''))}</a></p>` : ''}
          ${limits.length
            ? `<div class="small muted">Takes: ${esc(limits.join(', '))}</div>` : ''}
          ${live}
          ${depth ? `<div class="depth-grid">${depth}</div>` : ''}
          ${open
            ? `<div class="col-depth" data-col-depth="${esc(project.id)}"></div>
               <div class="col-detail" data-col-detail="${esc(project.id)}">${renderPeople(project.id)}</div>`
            : '<div class="small muted col-hint">double-click for the depth map and who has contributed</div>'}
        </div>
      </div>`;
    }).join('');

    // The sky each one covers, drawn on the survey so a collaboration can be
    // told at a glance - the Veil is the Veil before a word is read.
    box.querySelectorAll('[data-col-sky]').forEach((canvas) => {
      const project = projects.find((p) => p.id === canvas.dataset.colSky);
      if (project) drawSky(canvas, project);
    });

    // An opened card carries the field's depth map: everybody's accepted
    // frames where they really landed, each camera's field drawn at its
    // angle. The Plan tab's own map, so the two never disagree; it keeps
    // what it fetched across redraws, so a card opened a minute ago does
    // not flash "Fetching…" on every refresh.
    box.querySelectorAll('[data-col-depth]').forEach((host) => {
      if (typeof app.buildDepthMap !== 'function') return;
      const id = host.dataset.colDepth;
      const task = (status.tasks || []).find((t) => t.project === id) || {};
      const map = app.buildDepthMap({
        collab: { project: id, share: task.share || [], visit: task.visit || {} },
        target: {},
      });
      host.appendChild(map);
      if (map.show) map.show();
    });

    // Double-click opens a card on who has contributed, as a target in the
    // Planner opens on its framing; a click on a button or a box is not that.
    box.querySelectorAll('[data-col-id]').forEach((card) => {
      card.addEventListener('dblclick', (event) => {
        if (event.target.closest('button, input, label, a')) return;
        const id = card.dataset.colId;
        if (col.expanded.has(id)) col.expanded.delete(id); else col.expanded.add(id);
        window.getSelection && window.getSelection().removeAllRanges();
        drawBrowse();
        if (col.expanded.has(id)) loadPeople(id);
        if (col.expanded.has(id)) {
          const opened = box.querySelector(`[data-col-id="${CSS.escape(id)}"]`);
          if (opened) opened.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }
      });
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
    box.querySelectorAll('[data-col-delete]').forEach((button) => {
      button.addEventListener('click', () => deleteProject(
        projects.find((p) => p.id === button.dataset.colDelete)));
    });
    box.querySelectorAll('[data-col-redeal]').forEach((button) => {
      button.addEventListener('click', () => redealProject(
        projects.find((p) => p.id === button.dataset.colRedeal), button));
    });
  }

  /** Have every telescope on a collaboration dealt afresh. A list dealt
   *  for a night holds for the night; after changing the filters or the
   *  goals this is how the coordinator makes tonight follow. */
  async function redealProject(project, button) {
    if (!project) return;
    const ok = await app.confirmAction(
      `Deal “${project.name}” afresh for tonight? Every telescope on it gets a new `
      + 'list the next time it polls - within ten minutes - and yours right now. '
      + 'A telescope already shooting keeps its list until it polls.',
      { title: 'Re-deal tonight', confirmLabel: 'Re-deal' });
    if (!ok) return;
    button.disabled = true;
    try {
      const answer = await app.api(
        `/api/collab/projects/${encodeURIComponent(project.id)}/redeal`, 'POST');
      col.state = answer.state || col.state;
      const n = Number(answer.forgotten || 0);
      app.toast(`“${project.name}”: ${n} list${n === 1 ? '' : 's'} forgotten — each telescope is dealt `
        + 'afresh on its next poll; yours is on the plan now', 'success', 7000);
      await load(true);
    } catch (error) {
      app.toast(error.message, 'error');
    } finally {
      button.disabled = false;
    }
  }

  /* ------------------------------------------------- who has contributed */

  const ago = (seconds) => {
    const mins = Math.round(seconds / 60);
    if (mins < 2) return 'just now';
    if (mins < 90) return `${mins} min ago`;
    const hours = Math.round(mins / 60);
    if (hours < 36) return `${hours} h ago`;
    return `${Math.round(hours / 24)} days ago`;
  };

  /** The opened part of a card: each person, their picture, their hours. */
  function renderPeople(id) {
    const held = col.people.get(id);
    if (!held) return '<div class="small muted">fetching who has contributed…</div>';
    if (held.error) return `<div class="small warn-text">${esc(held.error)}</div>`;
    const data = held.data || {};
    const people = data.people || [];
    if (!people.length) {
      return '<div class="small muted">Nothing accepted yet from anybody.</div>';
    }
    const now = Number(data.serverTime) || (Date.now() / 1000);
    const rows = people.map((person) => {
      const picture = person.avatar
        ? `<img class="col-avatar" src="${esc(person.avatar)}" alt="" loading="lazy">`
        : `<span class="col-avatar blank">${esc((person.name || '?').slice(0, 1).toUpperCase())}</span>`;
      const filters = Object.entries(person.byFilter || {})
        .map(([name, hours]) => `<span class="col-chip">${esc(name)} ${Number(hours).toFixed(1)}h</span>`)
        .join('');
      const when = person.lastReported ? `last report ${ago(now - person.lastReported)}` : '';
      const scopes = (person.telescopes || []).join(', ');
      return `<div class="col-person">
        ${picture}
        <div class="col-person-body">
          <div><b>${esc(person.name)}</b>${scopes ? ` <span class="small muted">${esc(scopes)}</span>` : ''}</div>
          <div class="col-chips">${filters}</div>
          <div class="small muted">${person.frames ? `${person.frames} frames · ` : ''}${
            person.nights ? `${person.nights} night${person.nights === 1 ? '' : 's'} · ` : ''}${esc(when)}</div>
        </div>
        <div class="col-person-hours mono">${Number(person.hours).toFixed(1)} h</div>
      </div>`;
    }).join('');
    const total = Number(data.hours || 0).toFixed(1);
    return `<div class="small muted col-detail-head">${people.length} ${people.length === 1 ? 'person' : 'people'} · ${total} h accepted${
      data.refused ? ` · ${data.refused} report${data.refused === 1 ? '' : 's'} refused` : ''}</div>${rows}`;
  }

  /** Fetch who has contributed to one collaboration and fill its open card. */
  async function loadPeople(id) {
    try {
      const data = await app.api(`/api/collab/projects/${encodeURIComponent(id)}/people`);
      col.people.set(id, { at: Date.now(), data });
    } catch (error) {
      col.people.set(id, { at: Date.now(), error: error.message || String(error) });
    }
    const box = document.querySelector(`[data-col-detail="${CSS.escape(id)}"]`);
    if (box) box.innerHTML = renderPeople(id);
  }

  /** Re-read the open collaborations and redraw them, keeping what is open.
   *  The create form is left alone: it is the one place on the page somebody
   *  may be in the middle of typing. */
  async function refreshBrowse() {
    col.lastRefresh = Date.now();
    try {
      col.state = await app.api('/api/collab');
    } catch (error) {
      console.error(error);
      return;
    }
    // A ticked-off "turn to angle" box survives the redraw.
    const turned = {};
    document.querySelectorAll('[data-col-turn]').forEach((box) => { turned[box.dataset.colTurn] = box.checked; });
    drawBrowse();
    document.querySelectorAll('[data-col-turn]').forEach((box) => {
      if (box.dataset.colTurn in turned) box.checked = turned[box.dataset.colTurn];
    });
    drawTask();
    for (const id of col.expanded) loadPeople(id);
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
    if ($('colPCollect')) $('colPCollect').checked = !!project.collectFiles;
    loadStacking(project);
    set('colRMinFocal', wants.minFocalLength);
    set('colRMaxFocal', wants.maxFocalLength);
    const takesColour = wants.acceptColour !== false;
    if ($('colROscRgb')) $('colROscRgb').checked = takesColour && wants.acceptOscRgb !== false;
    if ($('colROscNb')) $('colROscNb').checked = takesColour && wants.acceptOscNarrowband !== false;
    set('colRColourMoon', wants.colourMaxMoon === null || wants.colourMaxMoon === undefined
      ? '' : Math.round(wants.colourMaxMoon * 100));
    set('colRMaxBortle', wants.maxBortle);
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
    const was = col.editing;
    col.editing = null;
    col.stackLoaded = null;
    if (was) resetStacking();
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


  /* --------------------------------------- the live stacking fieldset */

  /* Stacking follows the collaboration (T706, the author's rule): the
     collaboration is the only place a project is made. A new one is stacked
     where the server says by default - the person's hub, else Rapid Stacker
     Collab when their account is linked - and this form shows that choice
     and offers off or another. Every save sends the collaboration's fields
     down to the stacking project; nothing is ever edited on the stacking side.
     A cloud project is made and kept by the server through the Collab API;
     an old collaboration's pasted join code is kept as it is, read-only. */

  const stackBackend = () => {
    if ($('stkCloud') && $('stkCloud').checked) return 'cloud';
    if ($('stkHub') && $('stkHub').checked) return 'hub';
    return '';
  };

  function stackMessage(text, bad = false) {
    const node = $('stkMsg');
    if (!node) return;
    node.textContent = text || '';
    node.style.color = bad ? 'var(--warn)' : '';
  }

  /** An old collaboration's pasted code, kept as it is unless converted. */
  const legacyCode = () => {
    const loaded = col.stackLoaded;
    return loaded && loaded.backend === 'cloud' && !loaded.managed ? loaded.joinCode : '';
  };

  /** "Live stacking: on (hub NAME)", in words, for what the form says now. */
  function stackSummary() {
    const backend = stackBackend();
    const cloud = (col.stackOpts || {}).cloud || {};
    if (backend === 'hub') {
      const pick = $('stkHubPick');
      const hub = (col.hubs || []).find((h) => h.id === (pick && pick.value));
      return `Live stacking: on (hub ${hub ? hub.name : (pick && pick.value) || '?'})`;
    }
    if (backend === 'cloud') {
      if (legacyCode() && !($('stkConvert') && $('stkConvert').checked)) {
        return 'Live stacking: on (Rapid Stacker Collab, the pasted join code it already has)';
      }
      return cloud.linked
        ? `Live stacking: on (Rapid Stacker Collab${cloud.user ? `, as ${cloud.user}` : ''})`
        : 'Live stacking: Rapid Stacker Collab, once your account is linked (below)';
    }
    return 'Live stacking: off';
  }

  /** Show the parts of the fieldset the chosen backend needs. */
  function drawStacking() {
    const backend = stackBackend();
    if ($('stkCodeBox')) $('stkCodeBox').hidden = !(backend === 'cloud' && legacyCode());
    if ($('stkHubBox')) $('stkHubBox').hidden = backend !== 'hub';
    if ($('stkVisBox')) $('stkVisBox').hidden = backend === '';
    if ($('stkSummary')) $('stkSummary').textContent = stackSummary();
  }

  /** What the fieldset says, or the reason it cannot be sent. */
  function stackingChoice() {
    const backend = stackBackend();
    const value = { backend };
    if (backend === 'cloud') {
      const kept = legacyCode();
      if (kept && !$('stkConvert').checked) {
        value.joinCode = kept;              // the old link, read-only: kept as it is
      } else if (!((col.stackOpts || {}).cloud || {}).linked) {
        return { error: 'Link your Rapid Stacker Collab account first (Rapid Stacker '
          + 'Collab account, below): the server makes the project with it.' };
      }
      value.visibility = $('stkVisibility').value || 'unlisted';
    } else if (backend === 'hub') {
      const hubId = $('stkHubPick').value;
      if (!hubId) {
        return { error: 'Choose a hub. Register one under Live stacking hub, below, '
          + 'or ask a hub\'s owner to list you as a coordinator.' };
      }
      value.hubId = hubId;
      value.visibility = $('stkVisibility').value || 'unlisted';
    }
    return { value };
  }

  /** Whether the fieldset differs from what the project had when it was loaded. */
  function stackingChanged(now) {
    const was = col.stackLoaded || { backend: '', hubId: '', visibility: 'unlisted' };
    if (now.backend !== was.backend) return true;
    if (now.backend === 'cloud') {
      if (legacyCode()) return !now.joinCode;          // converting to a server-made project
      return now.visibility !== was.visibility;
    }
    if (now.backend === 'hub') return now.hubId !== was.hubId || now.visibility !== was.visibility;
    return false;
  }

  function setVisibility(value) {
    $('stkVisibility').value = ['public', 'private'].includes(value) ? value : 'unlisted';
  }

  /** A new collaboration's form shows where the server would stack it. */
  function applyStackDefault() {
    if (col.editing || col.stackTouched) return;
    const fallback = (col.stackOpts || {}).default || { backend: '' };
    $('stkNone').checked = fallback.backend === '';
    $('stkCloud').checked = fallback.backend === 'cloud';
    $('stkHub').checked = fallback.backend === 'hub';
    if (fallback.backend === 'hub') drawHubPick(fallback.hubId);
    drawStacking();
  }

  /** The Edit form: where this collaboration is stacked, read from the
   *  server as its owner (the listing tells a telescope less). */
  async function loadStacking(project) {
    const block = project.stacking || {};
    const code = project.stackingJoinCode || '';
    const backend = block.backend || (code ? 'cloud' : '');
    col.stackLoaded = {
      backend, managed: !!block.managed, joinCode: code, hubId: block.hubId || '',
      visibility: block.visibility || 'unlisted',
    };
    showLoaded();
    try {
      const got = await app.api(`/api/collab/projects/${encodeURIComponent(project.id)}/stacking`);
      if (!col.editing || col.editing.id !== project.id) return;
      col.stackLoaded = {
        backend: got.backend, managed: !!got.managed, joinCode: got.joinCode || '',
        hubId: got.hubId || '', visibility: got.visibility || 'unlisted',
      };
      showLoaded();
      drawStackState(got);
    } catch (error) {
      drawStackState(null, error.message);
    }
  }

  function showLoaded() {
    const loaded = col.stackLoaded;
    $('stkNone').checked = loaded.backend === '';
    $('stkCloud').checked = loaded.backend === 'cloud';
    $('stkHub').checked = loaded.backend === 'hub';
    $('stkJoinCode').value = legacyCode();
    $('stkConvert').checked = false;
    setVisibility(loaded.visibility);
    drawHubPick(loaded.hubId);
    drawStacking();
  }

  /** How the stacking project of the collaboration being edited is doing. */
  function drawStackState(got, failed = '') {
    const node = $('stkState');
    if (!node) return;
    if (failed) { node.textContent = `Could not read its stacking state: ${failed}`; return; }
    const lines = [];
    const cloud = got && got.cloud;
    if (got && got.deletion && got.deletion.status === 'deleted') {
      lines.push(`Deleted; restorable until ${String(got.deletion.delete_after || '').slice(0, 10)}.`);
    }
    if (cloud && got.managed) {
      if (cloud.state === 'ok') {
        lines.push(`Rapid Stacker Collab project ${cloud.slug || ''} is in step`
          + `${cloud.remoteStatus ? ` (${cloud.remoteStatus})` : ''}.`);
      } else if (cloud.state === 'pending') {
        lines.push('Rapid Stacker Collab project: being made.');
      } else if (cloud.error) {
        lines.push(`Rapid Stacker Collab: ${cloud.error}`
          + (cloud.state === 'failed' && cloud.nextAt
            ? ` The server tries again at ${new Date(cloud.nextAt * 1000).toLocaleTimeString()}.` : ''));
      }
    } else if (got && got.backend === 'hub' && got.status) {
      lines.push(`Stacking project on ${got.hubName || 'the hub'}: ${got.status}.`);
    }
    node.innerHTML = lines.map((line) => esc(line)).join('<br>')
      + (cloud && cloud.pageUrl ? ` <a href="${esc(cloud.pageUrl)}" target="_blank" rel="noopener">Its page</a>` : '');
  }

  function resetStacking() {
    col.stackLoaded = null;
    col.stackTouched = false;
    $('stkJoinCode').value = '';
    $('stkConvert').checked = false;
    setVisibility('unlisted');
    if ($('stkState')) $('stkState').textContent = '';
    stackMessage('');
    applyStackDefault();
  }

  /** What the server offers this person: the hubs they may bind to, the
   *  default, their Collab link and their deleted collaborations. */
  async function loadHubs() {
    try {
      const answer = await app.api('/api/collab/stacking');
      col.stackOpts = answer;
      col.hubs = answer.hubs || [];
      col.hubsError = '';
    } catch (error) {
      col.hubs = [];
      col.hubsError = error.message;
    }
    drawHubPick();
    drawCloudLink();
    drawDeleted();
    applyStackDefault();
    drawStacking();
  }

  function drawHubPick(select) {
    const pick = $('stkHubPick');
    if (!pick) return;
    const keep = select || pick.value;
    const hubs = col.hubs || [];
    pick.innerHTML = hubs.map((hub) => `<option value="${esc(hub.id)}">${esc(hub.name)}`
      + `${hub.role === 'coordinator' ? ' (you coordinate)' : ''}`
      + `${hub.lanUrl ? ` (${esc(hub.lanUrl)})` : ''}</option>`).join('');
    // A project bound to a hub the list does not show (someone else's) still
    // has to show it, or saving would silently rebind it.
    if (keep && !hubs.some((hub) => hub.id === keep)) {
      pick.insertAdjacentHTML('afterbegin',
        `<option value="${esc(keep)}">${esc(keep)} (not one of yours)</option>`);
    }
    if (keep) pick.value = keep;
    const note = $('stkHubNote');
    if (note) {
      note.textContent = col.hubsError ? `Could not ask the server: ${col.hubsError}`
        : hubs.length ? '' : 'You have no hub to bind to. Set one up under Live '
          + 'stacking hub, below, or ask a hub\'s owner to list you as a coordinator.';
    }
  }

  /** The Rapid Stacker Collab account link: once per person, kept on the server. */
  function drawCloudLink() {
    const cloud = (col.stackOpts || {}).cloud || {};
    const text = $('stkCloudLinkText');
    if (text) {
      text.textContent = cloud.linked
        ? `Linked${cloud.user ? ` as ${cloud.user}` : ''} (${cloud.apiBase || ''})`
          + `${cloud.expiresAt ? `; the token expires ${String(cloud.expiresAt).slice(0, 10)}` : ''}.`
          + (cloud.sealed ? '' : ' This server keeps it unsealed: it has no key to seal it with.')
        : cloud.unreadable
          ? 'The link can no longer be read (the server\'s key changed): link again.'
          : 'Not linked. Without it, collaborations cannot be stacked on Rapid Stacker Collab.';
    }
    if ($('btnStkCloudUnlink')) $('btnStkCloudUnlink').hidden = !cloud.linked;
    if ($('stkCloudBase') && !$('stkCloudBase').value && cloud.apiBase) $('stkCloudBase').value = cloud.apiBase;
  }

  async function linkCloud() {
    const token = ($('stkCloudToken').value || '').trim();
    if (!token) { app.toast('Paste the API token first', 'error'); $('stkCloudToken').focus(); return; }
    $('btnStkCloudLink').disabled = true;
    try {
      const answer = await app.api('/api/collab/stacking/cloud', 'POST',
        { token, apiBase: ($('stkCloudBase').value || '').trim() });
      $('stkCloudToken').value = '';
      col.stackOpts = { ...(col.stackOpts || {}), cloud: answer.cloud };
      app.toast('Rapid Stacker Collab is linked', 'success');
      await loadHubs();
    } catch (error) {
      app.toast(error.message, 'error');
      stackMessage(error.message, true);
    }
    $('btnStkCloudLink').disabled = false;
  }

  async function unlinkCloud() {
    const ok = await app.confirmAction('Forget your Rapid Stacker Collab token on the server? '
      + 'Collaborations already on the cloud keep their projects, but their changes wait '
      + 'until you link again.', { title: 'Unlink Rapid Stacker Collab', confirmLabel: 'Unlink', danger: true });
    if (!ok) return;
    try {
      await app.api('/api/collab/stacking/cloud/unlink', 'POST', {});
      await loadHubs();
    } catch (error) { app.toast(error.message, 'error'); }
  }

  /** Deleted collaborations are off every list; this is where they are restored. */
  function drawDeleted() {
    const box = $('colDeleted');
    if (!box) return;
    const deleted = ((col.stackOpts || {}).deleted || []).filter((d) => d.restorable);
    box.hidden = !deleted.length;
    box.innerHTML = deleted.length
      ? `<h4>Deleted collaborations</h4>${deleted.map((d) => `<div class="row gap small">
          <span class="grow">${esc(d.name)} — restorable until ${esc(String(d.deleteAfter || '').slice(0, 10))}${
            d.note ? `<br><span class="muted">${esc(d.note)}</span>` : ''}</span>
          <button class="btn small" data-col-restore="${esc(d.id)}">Restore</button></div>`).join('')}`
      : '';
    box.querySelectorAll('[data-col-restore]').forEach((button) => {
      button.addEventListener('click', () => restoreProject(button.dataset.colRestore, button));
    });
  }

  async function deleteProject(project) {
    if (!project) return;
    const days = (col.stackOpts || {}).windowDays || 7;
    const ok = await app.confirmAction(
      `Delete “${project.name}”? It comes off everybody's list and its stacking project stops `
      + `taking frames. You can restore it for ${days} days; after that the raw frames are `
      + 'purged. The finished stacks are kept until you remove them.',
      { title: 'Delete collaboration', confirmLabel: 'Delete it', danger: true });
    if (!ok) return;
    try {
      const answer = await app.api(`/api/collab/projects/${encodeURIComponent(project.id)}/delete`, 'POST', {});
      col.state = answer.state || col.state;
      const cloud = (answer.deletion || {}).cloud;
      app.toast(cloud && cloud.state === 'needs_owner' ? cloud.error
        : `“${project.name}” is deleted; restorable until `
          + `${String((answer.deletion || {}).delete_after || '').slice(0, 10)}`, 'success', 8000);
      await load(true);
    } catch (error) { app.toast(error.message, 'error'); }
  }

  async function restoreProject(id, button) {
    button.disabled = true;
    try {
      const answer = await app.api(`/api/collab/projects/${encodeURIComponent(id)}/restore`, 'POST', {});
      col.state = answer.state || col.state;
      app.toast('Restored', 'success');
      await load(true);
    } catch (error) {
      app.toast(error.message, 'error');
      button.disabled = false;
    }
  }

  /* ------------------------------------------ small shared formatting */

  /** A time as "12 s ago": epoch seconds or milliseconds, or an ISO string. */
  function agoText(value) {
    if (value === null || value === undefined || value === '') return '—';
    let ms = typeof value === 'number' ? (value > 1e12 ? value : value * 1000) : Date.parse(value);
    if (!Number.isFinite(ms)) return String(value);
    const seconds = Math.round((Date.now() - ms) / 1000);
    if (seconds < -5) return new Date(ms).toLocaleString();
    return ago(Math.max(0, seconds));
  }

  /** A text field is the browser-mode fallback: a native dialog only exists
   *  inside the desktop window. */
  async function pickFolderInto(id) {
    const native = (window.pywebview && window.pywebview.api) || null;
    if (!native) { $(id).focus(); app.toast('Type the folder path here', 'info'); return; }
    try {
      const chosen = await native.pick_folder($(id).value || '');
      if (chosen) $(id).value = chosen;
    } catch (error) { app.toast(String(error), 'error'); }
  }

  const SIGNING_NOTE = 'Installs are refused until a Starfront release signing '
    + 'key has been generated and built in. Nothing unverified is ever run, so '
    + 'this cannot be worked around from here.';

  /** An install error in words; the placeholder-key refusal said plainly. */
  const installWords = (message) => (/signing key/i.test(message || '')
    ? `${message} — ${SIGNING_NOTE}` : (message || ''));

  function drawProgress(boxId, barId, textId, progress) {
    const box = $(boxId);
    if (!box) return;
    const p = progress || {};
    const show = !!(p.busy || p.error || (p.phase && p.phase !== 'idle'));
    box.hidden = !show;
    if (!show) return;
    $(barId).style.width = `${Math.round(100 * (p.progress || 0))}%`;
    const text = $(textId);
    text.textContent = p.error ? installWords(p.error)
      : `${p.phase}${p.message ? `: ${p.message}` : ''}`;
    text.style.color = p.error ? 'var(--warn)' : '';
  }

  /* -------------------------------------------- the hub on this machine */

  const hubState = { status: null, loaded: false, last: 0, logAt: 0, enrol: null };

  function hubMessage(text, bad = false) {
    const node = $('hubMsg');
    if (!node) return;
    node.textContent = text || '';
    node.style.color = bad ? 'var(--warn)' : '';
  }

  /** The hub pill from the live snapshot: cheap, runs on every tick. */
  function drawHubPill(summary) {
    const pill = $('hubPill');
    if (!pill || !summary) return;
    const running = summary.state === 'running';
    pill.className = `pill ${running ? 'on' : summary.lastError ? 'busy' : ''}`;
    $('hubPillText').textContent = summary.enabled || running
      ? `${summary.state || 'stopped'}${running && summary.workers !== null
        && summary.workers !== undefined ? ` · ${summary.workers} worker${summary.workers === 1 ? '' : 's'}` : ''}`
      : 'off';
    $('hubUrlText').textContent = summary.url || '';
    $('hubPill').title = summary.lastError || '';
    const local = $('volLocalBox');
    if (local) local.hidden = !summary.enabled;
  }

  /** Fill the form from the stored settings (once, or on a deliberate reload). */
  async function loadHubSettings() {
    try {
      const all = await app.api('/api/settings');
      const hub = all.hub || {};
      $('hubEnabled').checked = !!hub.enabled;
      $('hubAutoStart').checked = hub.autoStart !== false;
      $('hubDataRoot').value = hub.dataRoot || '';
      $('hubCatalogue').value = hub.catalogueDir || '';
      $('hubBind').value = hub.bind || '';
      $('hubPublicUrl').value = hub.publicUrl || '';
      hubState.loaded = true;
    } catch (error) { hubMessage(error.message, true); }
  }

  function drawHubStatus(status) {
    hubState.status = status;
    drawHubPill(status);
    drawProgress('hubInstallBox', 'hubInstallBar', 'hubInstallText', status.install);
    const installing = !!(status.install && status.install.busy);
    $('btnHubInstall').disabled = installing;
    $('btnHubInstall').textContent = status.binaryFound ? 'Update hub' : 'Install hub';
    $('btnHubStart').disabled = status.state === 'running' || status.state === 'starting';
    $('btnHubStop').disabled = status.state !== 'running' && status.state !== 'starting';
    $('btnHubCheck').disabled = !!status.checking;
    $('btnHubCheck').textContent = status.checking ? 'Checking…' : 'Check';

    const report = $('hubCheckReport');
    const check = status.check;
    if (check && check.report) {
      report.hidden = false;
      report.textContent = (check.ok ? 'Check passed.\n' : 'Check failed.\n') + check.report;
      report.style.borderColor = check.ok ? 'var(--good)' : 'var(--warn)';
    } else { report.hidden = true; }

    const reg = $('hubRegText');
    if (status.registeredId) {
      reg.textContent = `Registered with ${status.server || 'the server'} as `
        + `“${status.registeredName || status.registeredId}” (${status.registeredId}).`;
      $('btnHubRegister').textContent = 'Register again';
    } else {
      reg.textContent = status.server
        ? `Not registered with ${status.server}. Sign in under Join with Discord first.`
        : 'Not registered. Set a collaboration server and sign in first.';
      $('btnHubRegister').textContent = 'Register with Starfront';
    }
    if (status.lastError) hubMessage(status.lastError, true);
    drawFleet(status);
  }

  /** The fleet table. The hub's status JSON is not fixed yet, so this reads
   *  whichever of the usual fields a worker carries, shows the rest as
   *  key=value, and says plainly when there is nothing to show. */
  function drawFleet(status) {
    const box = $('hubFleet');
    if (!box) return;
    const raw = status.hubStatus && status.hubStatus.workers;
    let workers = [];
    if (Array.isArray(raw)) workers = raw;
    else if (raw && typeof raw === 'object' && Array.isArray(raw.list)) workers = raw.list;
    else if (raw && typeof raw === 'object') {
      workers = Object.entries(raw).filter(([, v]) => v && typeof v === 'object')
        .map(([key, v]) => ({ id: key, ...v }));
    }
    if (!workers.length) {
      const count = status.workers;
      box.innerHTML = `<p class="small muted">${status.state !== 'running'
        ? 'The hub is not running.'
        : count ? `${esc(count)} worker${count === 1 ? '' : 's'} reported, with no detail yet.`
          : 'No worker has joined yet. Make an enrol code, below.'}</p>`;
      return;
    }
    const first = (w, keys) => { for (const k of keys) if (w[k] !== undefined && w[k] !== null) return w[k]; return undefined; };
    const used = new Set(['name', 'worker', 'worker_id', 'id', 'version', 'catalogue', 'busy',
      'reason', 'busy_reason', 'why', 'state', 'last_heartbeat', 'heartbeat_at', 'last_seen', 'seen']);
    const rows = workers.map((w) => {
      const name = first(w, ['name', 'worker', 'worker_id', 'id']);
      const busy = first(w, ['busy']);
      const why = first(w, ['busy_reason', 'reason', 'why', 'state']);
      const cat = first(w, ['catalogue']);
      const extra = Object.entries(w).filter(([k, v]) => !used.has(k) && v !== null
        && typeof v !== 'object').map(([k, v]) => `${k}=${v}`).join(' ');
      return `<tr><td>${esc(name === undefined ? '—' : name)}</td>`
        + `<td class="mono">${esc(first(w, ['version']) || '—')}</td>`
        + `<td>${cat === undefined ? '—' : cat === true ? 'yes' : cat === false ? 'no' : esc(cat)}</td>`
        + `<td>${busy === undefined ? esc(why || '—') : (busy ? 'busy' : 'idle')}`
        + `${why && busy !== undefined ? ` <span class="muted">(${esc(why)})</span>` : ''}</td>`
        + `<td>${esc(agoText(first(w, ['last_heartbeat', 'heartbeat_at', 'last_seen', 'seen'])))}</td>`
        + `<td class="small muted">${esc(extra)}</td></tr>`;
    }).join('');
    box.innerHTML = '<table class="survey-fields"><thead><tr><th>Worker</th><th>Version</th>'
      + '<th>Catalogue</th><th>State</th><th>Last heartbeat</th><th></th></tr></thead>'
      + `<tbody>${rows}</tbody></table>`;
  }

  async function refreshHub() {
    hubState.last = Date.now();
    try { drawHubStatus(await app.api('/api/hub')); } catch (error) { hubMessage(error.message, true); }
  }

  async function hubAction(button, path, body) {
    if (button) button.disabled = true;
    hubMessage('');
    try {
      drawHubStatus(await app.api(path, 'POST', body === undefined ? {} : body));
    } catch (error) {
      hubMessage(installWords(error.message), true);
      app.toast(error.message, 'error');
    }
    if (button) button.disabled = false;
    refreshHub();
  }

  async function saveHub() {
    const values = {
      enabled: $('hubEnabled').checked, autoStart: $('hubAutoStart').checked,
      dataRoot: $('hubDataRoot').value.trim(), catalogueDir: $('hubCatalogue').value.trim(),
      publicUrl: $('hubPublicUrl').value.trim(),
    };
    const bind = $('hubBind').value.trim();
    if (bind) values.bind = bind;
    try {
      drawHubStatus(await app.api('/api/settings/hub', 'POST', values));
      hubMessage('Saved. Restart the hub for folder and address changes to apply.');
    } catch (error) { hubMessage(error.message, true); app.toast(error.message, 'error'); }
  }

  async function newEnrolCode() {
    $('btnHubEnrol').disabled = true;
    try {
      const answer = await app.api('/api/hub/enrol-code', 'POST', {});
      hubState.enrol = answer;
      $('hubEnrolBox').hidden = false;
      $('hubEnrolCode').textContent = answer.code || '';
      const at = answer.expires_at;
      $('hubEnrolExpiry').textContent = at ? `expires ${typeof at === 'number'
        ? new Date((at > 1e12 ? at : at * 1000)).toLocaleTimeString()
        : new Date(at).toLocaleTimeString()}; works once` : 'works once';
    } catch (error) { app.toast(error.message, 'error'); hubMessage(error.message, true); }
    $('btnHubEnrol').disabled = false;
  }

  async function copyEnrolCode() {
    const code = ($('hubEnrolCode').textContent || '').trim();
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      app.toast('Copied', 'success', 2000);
    } catch (error) {
      const range = document.createRange();
      range.selectNodeContents($('hubEnrolCode'));
      const selection = window.getSelection();
      selection.removeAllRanges(); selection.addRange(range);
      app.toast('Select and copy the code', 'info');
    }
  }

  async function loadHubLog() {
    hubState.logAt = Date.now();
    try {
      const answer = await app.api('/api/hub/log?n=200');
      $('hubLog').textContent = (answer.lines || []).map((line) => {
        const when = line.at ? new Date(line.at * 1000).toLocaleTimeString() : '';
        return `${when} ${line.level || ''} ${line.message || line.raw || ''}`.trim();
      }).join('\n') || 'Nothing logged yet.';
    } catch (error) { $('hubLog').textContent = error.message; }
  }

  /* ---------------------------------------------- volunteer this machine */

  const volState = { status: null, loaded: false, last: 0 };

  function volMessage(text, bad = false) {
    const node = $('volMsg');
    if (!node) return;
    node.textContent = text || '';
    node.style.color = bad ? 'var(--warn)' : '';
  }

  function drawVolPill(summary) {
    const pill = $('volPill');
    if (!pill || !summary) return;
    const running = /run|active|start/i.test(summary.service || '') && !/not|stop/i.test(summary.service || '');
    pill.className = `pill ${running ? 'on' : summary.lastError ? 'busy' : ''}`;
    $('volPillText').textContent = !summary.installed ? 'not installed'
      : `${summary.version ? `v${summary.version} · ` : ''}${summary.service || 'installed'}`;
    $('volStatusText').textContent = summary.busy ? `${summary.phase}…` : '';
  }

  function drawVolStatus(status) {
    volState.status = status;
    drawVolPill(status);
    drawProgress('volProgressBox', 'volProgressBar', 'volProgressText', status.progress);
    const busy = !!status.busy;
    for (const id of ['btnVolInstall', 'btnVolUpdate', 'btnVolUninstall', 'btnVolStart', 'btnVolStop']) {
      $(id).disabled = busy;
    }
    $('btnVolInstall').textContent = status.installed ? 'Reinstall' : 'Install';
    $('btnVolUpdate').disabled = busy || !status.installed;
    $('btnVolUninstall').disabled = busy || !status.installed;
    $('btnVolStart').disabled = busy || !status.installed;
    $('btnVolStop').disabled = busy || !status.installed;
    const svc = status.serviceStatus || {};
    const detail = svc.message || svc.detail || svc.status || '';
    $('volStatusText').textContent = busy ? `${status.phase}…` : (detail ? String(detail) : '');
    if (!status.signingKeyConfigured && !status.installed) {
      volMessage(SIGNING_NOTE, true);
    } else if (status.lastError) {
      volMessage(installWords(status.lastError), true);
    }
  }

  function fillVolForm(status) {
    const s = status.settings || {};
    $('volHubUrl').value = status.hubUrl || s.hubUrl || '';
    $('volWhenIdle').checked = (s.idlePolicy || 'when_idle') === 'when_idle';
    $('volPauseSeq').checked = s.pauseWhileSequencing !== false;
    $('volPauseNina').checked = s.pauseWhileNina !== false;
    $('volWindow').value = s.workWindow || '';
    $('volScratch').value = s.scratchDir || '';
    $('volCap').value = s.scratchCapGb || 0;
    volState.loaded = true;
  }

  async function refreshVolunteer(fresh = false) {
    volState.last = Date.now();
    try {
      const status = await app.api(fresh ? '/api/volunteer/status' : '/api/volunteer');
      if (!volState.loaded) fillVolForm(status);
      drawVolStatus(status);
    } catch (error) { volMessage(error.message, true); }
  }

  function volHubUrl() {
    if ($('volLocalHub').checked) {
      return (hubState.status && (hubState.status.url || '')) || '';
    }
    return $('volHubUrl').value.trim();
  }

  async function volAction(path, body) {
    volMessage('');
    try {
      drawVolStatus(await app.api(path, 'POST', body === undefined ? {} : body));
    } catch (error) {
      volMessage(installWords(error.message), true);
      app.toast(error.message, 'error');
    }
  }

  async function saveVolunteer() {
    const cap = Number($('volCap').value || 0);
    const values = {
      idlePolicy: $('volWhenIdle').checked ? 'when_idle' : 'always',
      pauseWhileSequencing: $('volPauseSeq').checked,
      pauseWhileNina: $('volPauseNina').checked,
      workWindow: $('volWindow').value.trim(),
      scratchDir: $('volScratch').value.trim(),
      scratchCapGb: Number.isFinite(cap) && cap >= 0 ? Math.round(cap) : 0,
    };
    if (values.workWindow && !/^\d{1,2}:\d{2}-\d{1,2}:\d{2}$/.test(values.workWindow)) {
      volMessage('The work window is two times, like 22:00-06:00.', true);
      return;
    }
    const url = volHubUrl();
    if (url) values.hubUrl = url;
    try {
      drawVolStatus(await app.api('/api/settings/volunteer', 'POST', values));
      volMessage('Saved.');
    } catch (error) { volMessage(error.message, true); app.toast(error.message, 'error'); }
  }

  function installVolunteer() {
    const url = volHubUrl();
    const code = $('volCode').value.trim();
    if (!url) { volMessage('Say which hub to work for: its address, or tick “Use this machine’s hub”.', true); return; }
    if (!code && !(volState.status && volState.status.workerId)) {
      volMessage('Enter the enrol code the hub’s owner gave you.', true);
      return;
    }
    volAction('/api/volunteer/install', { code, hubUrl: url });
  }

  /** Called from the status tick: keep the pills fresh and, while a section
   *  is open or a job is running, re-read it every few seconds. */
  function tickStacking(status, shown) {
    if (status.hub) drawHubPill(status.hub);
    if (status.volunteer) drawVolPill(status.volunteer);
    if (!shown) return;
    const now = Date.now();
    const hubBusy = !!(hubState.status && (hubState.status.install || {}).busy);
    if (($('hubFold').open || hubBusy) && now - hubState.last > 3000) refreshHub();
    if ($('hubFold').open && now - hubState.logAt > 5000) loadHubLog();
    const volBusy = !!(status.volunteer && status.volunteer.busy);
    if (($('volFold').open || volBusy) && now - volState.last > (volBusy ? 1500 : 5000)) {
      refreshVolunteer(false);
    }
  }

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
    // The feed is a nicety: an older server has none, and the tab must not
    // wait on it or complain about it.
    if ((col.state || {}).configured) {
      try {
        col.activity = await app.api('/api/collab/activity');
      } catch (error) { /* the strip shows what presence alone can say */ }
    }
    col.loading = false;
    loadHubs();
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
    // Opening the Start form puts the cursor in the name.
    on('colStartFold', 'toggle', () => {
      const fold = $('colStartFold');
      if (fold && fold.open && $('colPName')) setTimeout(() => $('colPName').focus(), 150);
    });
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

    // Live stacking on the form.
    for (const id of ['stkNone', 'stkCloud', 'stkHub', 'stkConvert', 'stkHubPick']) {
      on(id, 'change', () => { col.stackTouched = true; drawStacking(); stackMessage(''); });
    }
    on('btnStkHubs', 'click', loadHubs);
    on('btnStkCloudLink', 'click', linkCloud);
    on('btnStkCloudUnlink', 'click', unlinkCloud);

    // The hub on this machine.
    on('hubFold', 'toggle', () => {
      if (!$('hubFold').open) return;
      if (!hubState.loaded) loadHubSettings();
      refreshHub();
      loadHubLog();
    });
    on('btnHubDataBrowse', 'click', () => pickFolderInto('hubDataRoot'));
    on('btnHubCatBrowse', 'click', () => pickFolderInto('hubCatalogue'));
    on('btnHubSave', 'click', saveHub);
    on('btnHubInstall', 'click', () => hubAction($('btnHubInstall'), '/api/hub/install'));
    on('btnHubCheck', 'click', () => hubAction($('btnHubCheck'), '/api/hub/check'));
    on('btnHubStart', 'click', () => hubAction($('btnHubStart'), '/api/hub/start'));
    on('btnHubStop', 'click', () => hubAction($('btnHubStop'), '/api/hub/stop'));
    on('btnHubRegister', 'click', () => {
      const name = $('hubRegName').value.trim() || (hubState.status || {}).registeredName || '';
      if (!name) { hubMessage('Give the hub a name first.', true); $('hubRegName').focus(); return; }
      hubAction($('btnHubRegister'), '/api/hub/register',
        { name, force: !!(hubState.status && hubState.status.registeredId) });
    });
    on('btnHubEnrol', 'click', newEnrolCode);
    on('btnHubEnrolCopy', 'click', copyEnrolCode);
    on('btnHubLog', 'click', loadHubLog);

    // Volunteering this machine.
    on('volFold', 'toggle', () => { if ($('volFold').open) refreshVolunteer(true); });
    on('volLocalHub', 'change', () => {
      const local = $('volLocalHub').checked;
      $('volHubUrl').disabled = local;
      if (local && hubState.status) $('volHubUrl').value = hubState.status.url || '';
    });
    on('btnVolScratchBrowse', 'click', () => pickFolderInto('volScratch'));
    on('btnVolInstall', 'click', installVolunteer);
    on('btnVolUpdate', 'click', () => volAction('/api/volunteer/update'));
    on('btnVolUninstall', 'click', async () => {
      const ok = await app.confirmAction('Remove the volunteer worker from this computer?',
        { title: 'Uninstall', confirmLabel: 'Uninstall', danger: true });
      if (ok) volAction('/api/volunteer/uninstall');
    });
    on('btnVolStart', 'click', () => volAction('/api/volunteer/start'));
    on('btnVolStop', 'click', () => volAction('/api/volunteer/stop'));
    on('btnVolSave', 'click', saveVolunteer);

    drawFilterRows();
    drawTargetPick();
    drawStacking();
  }

  /* Loaded on arrival, not on every status tick: rebuilding these forms two
     and a half times a second would reset a half-typed name, which is a bug
     this program has already had once. */
  let arrived = false;
  app.onStatus((status, tab) => {
    syncSettings(app.state.settings);
    tickStacking(status || {}, tab === 'collab');
    if (tab !== 'collab') { arrived = false; return; }
    if (!arrived) {
      arrived = true;
      drawTargetPick();
      col.lastRefresh = Date.now();
      load(true);
      return;
    }
    // While the tab stays open, the cards follow the server: progress moves
    // as reports land, and a card opened on its contributors keeps up.
    if (!col.loading && Date.now() - col.lastRefresh > REFRESH_SECONDS * 1000) refreshBrowse();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
