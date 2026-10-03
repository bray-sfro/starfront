/* The Calibrate tab: building the master frames, and saying what they cover.
 *
 * Two things live here. A *recipe* is the list of sets to shoot — thirty darks
 * at 300 seconds, twenty flats through each filter — saved by name so the same
 * library gets rebuilt the same way every season. The *library* is what came of
 * that: the masters on disk, and whether they actually describe the frames this
 * rig is taking tonight.
 *
 * The second half is the one worth having on screen. "No master flat" and "the
 * only master flat is fourteen months old" are both refusals, and they call for
 * very different actions on a night that has already started.
 */
'use strict';

(function () {
  const app = window.astro;
  const $ = app.$;

  const cal = {
    loaded: null,      // the last /api/calibration payload
    recipeId: null,    // which saved recipe is being edited, if any
    sets: [],          // the sets as they are on screen
    dirty: false,
    running: false,
  };

  const TYPE_LABELS = {
    bias: 'Bias', dark: 'Dark', flat: 'Flat', darkflat: 'Dark for the flats',
  };

  /* --------------------------------------------------------------- helpers */

  function duration(seconds) {
    if (!seconds || seconds <= 0) return '0m';
    const m = Math.round(seconds / 60);
    if (m < 60) return `${m}m`;
    return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
  }

  function ago(timestamp) {
    if (!timestamp) return '—';
    const days = (Date.now() / 1000 - timestamp) / 86400;
    if (days < 1) return 'today';
    if (days < 2) return 'yesterday';
    if (days < 60) return `${Math.round(days)} days ago`;
    return `${Math.round(days / 30)} months ago`;
  }

  const bytes = (n) => (!n ? '0' : n > 1e9 ? `${(n / 1e9).toFixed(1)} GB`
    : `${Math.round(n / 1e6)} MB`);

  /** Roughly how long the recipe takes, using the same arithmetic the plan does. */
  function estimateSeconds() {
    const overhead = 15;
    return cal.sets.reduce((total, spec) => {
      let exposure = Number(spec.exposure) || 0;
      if (spec.frameType === 'flat' && spec.autoExposure && !exposure) exposure = 1;
      return total + (Number(spec.count) || 0) * (exposure + overhead);
    }, 0);
  }

  function markDirty() {
    cal.dirty = true;
    renderEstimate();
  }

  function renderEstimate() {
    const frames = cal.sets.reduce((t, s) => t + (Number(s.count) || 0), 0);
    $('calEstimate').textContent = cal.sets.length
      ? `${cal.sets.length} set(s) · ${frames} frames · about `
        + `${duration(estimateSeconds())}${cal.dirty ? ' · unsaved' : ''}`
      : 'nothing in this recipe yet';
  }

  /* ----------------------------------------------------------- the recipe */

  function blankSet(kind) {
    return {
      frameType: kind,
      count: kind === 'bias' ? 50 : 25,
      exposure: kind === 'dark' ? 300 : 0,
      binning: 1,
      gain: null,
      offset: null,
      filter: '',
      allFilters: false,
      source: 'panel',
      autoExposure: kind === 'flat',
      followsFlat: kind === 'darkflat',
      brightness: null,
    };
  }

  /** One editable row. Built as nodes rather than markup so a filter name
      containing a quote cannot break the form. */
  function setRow(spec, index) {
    const row = document.createElement('div');
    row.className = 'cal-set';

    const kind = document.createElement('span');
    kind.className = `cal-set-kind cal-${spec.frameType}`;
    kind.textContent = TYPE_LABELS[spec.frameType] || spec.frameType;
    row.appendChild(kind);

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

    const number = (key, attrs, onChange) => {
      const input = document.createElement('input');
      input.type = 'number';
      Object.assign(input, attrs);
      input.value = spec[key] === null || spec[key] === undefined ? '' : spec[key];
      input.addEventListener('change', () => {
        const raw = input.value.trim();
        spec[key] = raw === '' ? null : Number(raw);
        if (onChange) onChange();
        markDirty();
      });
      return input;
    };

    field('Frames', number('count', { min: 1, max: 500, step: 1 }));

    if (spec.frameType === 'bias') {
      const note = document.createElement('span');
      note.className = 'cal-note muted small';
      note.textContent = 'shortest the camera can do';
      row.appendChild(note);
    } else if (spec.frameType === 'flat') {
      // A panel is a lamp on the front of the telescope; the sky is twilight,
      // which needs the mount and fades while you are using it.
      const source = document.createElement('select');
      source.appendChild(new Option('Flat panel', 'panel'));
      source.appendChild(new Option('Twilight sky', 'sky'));
      source.value = spec.source === 'sky' ? 'sky' : 'panel';
      source.addEventListener('change', () => {
        spec.source = source.value;
        markDirty();
        renderSets();
      });
      field('Light source', source);

      // No exposure box: a flat always measures its own. What reaches the
      // sensor depends on the panel, the filter and the optics, and a sky
      // flat re-measures every frame as the twilight changes.
      spec.autoExposure = true;
      const note = document.createElement('span');
      note.className = 'cal-note muted small';
      note.textContent = spec.source === 'sky'
        ? 'exposure measured for every frame' : 'exposure measured';
      row.appendChild(note);
    } else if (spec.frameType === 'darkflat') {
      const exposure = field('Exposure (s)',
        number('exposure', { min: 0, max: 600, step: 0.1 }));
      const follow = document.createElement('input');
      follow.type = 'checkbox';
      follow.checked = !!spec.followsFlat;
      const wrap = document.createElement('label');
      wrap.className = 'inline compact';
      wrap.appendChild(follow);
      wrap.appendChild(document.createTextNode(' Match the flats'));
      wrap.title = 'Use whatever exposure the flats in this run settled on. '
        + 'That is the whole point of a dark for the flats, and it is not '
        + 'known until the flats have been taken.';
      follow.addEventListener('change', () => {
        spec.followsFlat = follow.checked;
        exposure.disabled = follow.checked;
        markDirty();
      });
      exposure.disabled = follow.checked;
      row.appendChild(wrap);
    } else {
      field('Exposure (s)', number('exposure', { min: 0, max: 3600, step: 1 }));
    }

    if (spec.frameType === 'flat' || spec.frameType === 'darkflat') {
      const select = document.createElement('select');
      const names = (cal.loaded && cal.loaded.filters) || [];
      const options = names.includes(spec.filter) || !spec.filter
        ? names : [spec.filter, ...names];
      // One row can stand for the whole wheel: picking this keeps a
      // seven-filter setup to a single line instead of seven.
      select.appendChild(new Option('every filter', '*'));
      select.appendChild(new Option('no filter', ''));
      for (const name of options) select.appendChild(new Option(name, name));
      select.value = spec.allFilters ? '*' : (spec.filter || '');
      select.addEventListener('change', () => {
        spec.allFilters = select.value === '*';
        spec.filter = spec.allFilters ? '' : select.value;
        markDirty();
        renderSets();
      });
      field('Filter', select);

      if (spec.allFilters) {
        const note = document.createElement('span');
        note.className = 'cal-note muted small';
        note.textContent = names.length
          ? `${names.length} filters: ${names.join(', ')}`
          : 'no filters known — set them in Equipment';
        row.appendChild(note);
      }
    }

    const bin = document.createElement('select');
    for (const value of [1, 2, 3, 4]) {
      bin.appendChild(new Option(`${value} × ${value}`, String(value)));
    }
    bin.value = String(spec.binning || 1);
    bin.addEventListener('change', () => {
      spec.binning = Number(bin.value);
      markDirty();
    });
    field('Binning', bin);

    field('Gain', number('gain', { min: 0, max: 100000, step: 1, placeholder: 'as set' }));
    field('Offset', number('offset', { min: 0, max: 100000, step: 1, placeholder: 'as set' }));

    const spacer = document.createElement('span');
    spacer.className = 'spacer';
    row.appendChild(spacer);

    const up = document.createElement('button');
    up.className = 'btn small ghost';
    up.textContent = '↑';
    up.title = 'Shoot this set earlier. Order matters: the flats have to be '
      + 'taken before the darks that match them.';
    up.disabled = index === 0;
    up.addEventListener('click', () => {
      cal.sets.splice(index - 1, 0, cal.sets.splice(index, 1)[0]);
      markDirty();
      renderSets();
    });
    row.appendChild(up);

    const remove = document.createElement('button');
    remove.className = 'btn small ghost danger';
    remove.textContent = 'Remove';
    remove.addEventListener('click', () => {
      cal.sets.splice(index, 1);
      markDirty();
      renderSets();
    });
    row.appendChild(remove);

    return row;
  }

  function renderSets() {
    const host = $('calSets');
    host.innerHTML = '';
    if (!cal.sets.length) {
      const empty = document.createElement('p');
      empty.className = 'muted small';
      empty.textContent = 'Nothing in this recipe yet. Add a set below, or press '
        + 'Suggest to build one from the camera and filter wheel that are '
        + 'connected now.';
      host.appendChild(empty);
    } else {
      cal.sets.forEach((spec, index) => host.appendChild(setRow(spec, index)));
    }
    renderEstimate();
  }

  function loadRecipe(recipe) {
    cal.recipeId = recipe && recipe.id ? recipe.id : null;
    cal.sets = JSON.parse(JSON.stringify((recipe && recipe.sets) || []));
    $('calName').value = (recipe && recipe.name) || '';
    cal.dirty = false;
    $('calRecipe').value = cal.recipeId || '';
    renderSets();
  }

  /* ---------------------------------------------------------- the library */

  function renderLibrary(payload) {
    const lib = payload.library || {};
    $('calLibraryDir').textContent = lib.root || '—';
    $('calLibraryDir').title = lib.mastersDir || '';

    const counts = lib.counts || {};
    const total = lib.total || 0;
    $('calLibraryCount').textContent = total ? `${total}` : 'empty';
    $('calLibraryNote').textContent = total
      ? Object.entries(counts).map(([kind, n]) => `${n} ${kind}`).join(' · ')
        + `  ·  ${bytes(lib.bytes)}`
      : (lib.writable ? 'No masters yet. Build some below.'
        : 'That folder cannot be written to.');
    $('calLibraryNote').classList.toggle('warn-text', !lib.writable);

    const host = $('calMasters');
    host.innerHTML = '';
    const masters = lib.masters || [];
    if (!masters.length) return;

    for (const master of masters) {
      const row = document.createElement('div');
      row.className = 'cal-master';

      const kind = document.createElement('span');
      kind.className = `cal-set-kind cal-${master.type}`;
      kind.textContent = master.type;
      row.appendChild(kind);

      const what = document.createElement('div');
      what.className = 'cal-master-what';
      const bits = [];
      if (master.type !== 'bias') bits.push(`${master.exposure}s`);
      if (master.filter) bits.push(master.filter);
      if (master.binning > 1) bits.push(`bin${master.binning}`);
      if (master.temperature !== null && master.temperature !== undefined) {
        bits.push(`${master.temperature}°C`);
      }
      const line = document.createElement('b');
      line.textContent = bits.join('  ');
      what.appendChild(line);
      const detail = document.createElement('span');
      detail.className = 'muted small';
      detail.textContent = `${master.frames} frames · ${ago(master.created)}`
        + (master.telescope ? ` · ${master.telescope}` : '');
      what.appendChild(detail);
      row.appendChild(what);

      const remove = document.createElement('button');
      remove.className = 'btn small ghost danger';
      remove.textContent = '✕';
      remove.title = 'Delete this master';
      remove.addEventListener('click', async () => {
        const ok = await app.confirmAction(
          `Delete ${line.textContent.trim()}? The file is removed from the `
          + 'library folder. The frames it was built from are not touched.',
          { title: 'Delete master', confirmLabel: 'Delete', danger: true });
        if (!ok) return;
        await app.send(`/api/calibration/library/${master.id}`, 'DELETE', null,
          'Master deleted');
        reloadAfterLibraryChange();
      });
      row.appendChild(remove);

      host.appendChild(row);
    }
  }

  function fillSettings(settings) {
    if (!settings) return;
    $('calApplyTo').value = settings.applyTo || 'survey';
    $('calMatchTemp').value = settings.matchTemperatureC ?? 2;
    $('calMatchExp').value = settings.matchExposurePercent ?? 5;
    $('calFlatAge').value = settings.maxFlatAgeDays ?? 30;
    $('calDarkAge').value = settings.maxDarkAgeDays ?? 180;
    $('calStackMethod').value = settings.stackMethod || 'sigma';
    $('calSigma').value = settings.sigmaLow ?? 3;
    $('calFlatAdu').value = settings.flatTargetAdu ?? 25000;
    $('calPanelBrightness').value = settings.flatPanelBrightness ?? 50;
    $('calAutoBright').checked = settings.flatAutoBrightness !== false;
    $('calFlatMinBright').value = settings.flatMinBrightness ?? 5;
    $('calKeepSubs').checked = settings.keepSubs !== false;
    $('calAutoCover').checked = settings.autoCover !== false;
    $('calFlatMinExp').value = settings.flatMinExposure ?? 0.000032;
    $('calFlatMaxExp').value = settings.flatMaxExposure ?? 30;
    $('calSkyPointing').value = settings.skyFlatPointing || 'zenith';
    $('calSkyAltitude').value = settings.skyFlatAltitude ?? 80;
    $('calSkyMeridian').value = settings.skyFlatMeridianOffset ?? 0;
    $('calSkyDither').value = settings.skyFlatDitherArcmin ?? 2;
    $('calSkyAccept').value = settings.skyFlatAcceptPercent ?? 40;
    $('calSkyTracking').checked = settings.skyFlatTracking === true;
    updateSkyControls();
  }

  /** Grey out the controls that cannot apply to the choices made. */
  function updateSkyControls() {
    // The altitude only means anything for the anti-solar point; the meridian
    // offset only means anything if the mount is left tracking.
    const antisolar = $('calSkyPointing').value === 'antisolar';
    const tracking = $('calSkyTracking').checked;
    $('calSkyAltitude').disabled = !antisolar;
    $('calSkyMeridian').disabled = antisolar || !tracking;
  }

  /** Where sky flats would point right now, said in plain terms. */
  async function loadFlatSpot() {
    const node = $('calFlatSpot');
    if (!node) return;
    let spot;
    try {
      spot = await app.api('/api/calibration/flatspot');
    } catch (error) {
      node.textContent = error.message;
      node.className = 'small mono warn-text';
      return;
    }
    node.className = 'small mono';
    node.textContent =
      `now: ${spot.label}, ${spot.altitude.toFixed(0)}° up`
      + `  ·  RA ${spot.ra.toFixed(3)}h Dec ${spot.dec.toFixed(2)}°`
      + `  ·  sun ${spot.sunAltitude.toFixed(1)}° (${spot.half})`
      + (spot.mount ? '' : '  ·  NO MOUNT CONNECTED');
    node.classList.toggle('warn-text', !spot.mount);
  }

  async function saveSettings() {
    const sigma = Number($('calSigma').value) || 3;
    try {
      await app.api('/api/calibration/settings', 'POST', {
        applyTo: $('calApplyTo').value,
        matchTemperatureC: Number($('calMatchTemp').value),
        matchExposurePercent: Number($('calMatchExp').value),
        maxFlatAgeDays: Number($('calFlatAge').value),
        maxDarkAgeDays: Number($('calDarkAge').value),
        stackMethod: $('calStackMethod').value,
        sigmaLow: sigma,
        sigmaHigh: sigma,
        flatTargetAdu: Number($('calFlatAdu').value),
        flatPanelBrightness: Math.round(Number($('calPanelBrightness').value)),
        flatAutoBrightness: $('calAutoBright').checked,
        flatMinBrightness: Math.round(Number($('calFlatMinBright').value)),
        keepSubs: $('calKeepSubs').checked,
        autoCover: $('calAutoCover').checked,
        skyFlatPointing: $('calSkyPointing').value,
        skyFlatAltitude: Number($('calSkyAltitude').value),
        skyFlatMeridianOffset: Number($('calSkyMeridian').value),
        skyFlatDitherArcmin: Number($('calSkyDither').value),
        skyFlatAcceptPercent: Number($('calSkyAccept').value),
        skyFlatTracking: $('calSkyTracking').checked,
        flatMinExposure: Number($('calFlatMinExp').value),
        flatMaxExposure: Number($('calFlatMaxExp').value),
      });
    } catch (error) {
      app.toast(error.message, 'error');
      return;
    }
    app.toast('Calibration settings saved', 'success');
    load();
    loadMatch();
    loadCoverage();
    loadFlatSpot();
  }

  /* ---------------------------------------------------- masters for tonight */

  /** Is there a valid, current set of masters for what tonight will shoot?
   *
   *  One row per thing the night needs - a bias, a dark at each default
   *  exposure, a flat through each filter - and each row is one of three
   *  words: ok, out of date, missing. Those are the three things a person
   *  can do something about, and the heading says which the night is in.
   */
  async function loadCoverage() {
    const head = $('calCoverageHead');
    const rows = $('calCoverageRows');
    if (!head || !rows) return;
    let result;
    try {
      result = await app.api(app.rigQuery('/api/calibration/coverage'));
    } catch (error) {
      head.textContent = error.message;
      head.className = 'cal-coverage-head';
      rows.innerHTML = '';
      return;
    }
    const words = { ok: 'Ready', stale: 'Out of date', missing: 'Incomplete',
      unknown: 'Nothing to check' };
    head.className = `cal-coverage-head ${result.state}`;
    head.textContent = `${words[result.state] || result.state} — ${result.summary}`;
    head.title = `for ${result.telescope}`;

    rows.innerHTML = '';
    for (const row of result.rows || []) {
      const line = document.createElement('div');
      line.className = `cal-coverage-row ${row.state}`;
      const state = document.createElement('span');
      state.className = 'state';
      state.textContent = row.state === 'ok' ? 'ok'
        : (row.state === 'stale' ? 'out of date' : 'missing');
      line.appendChild(state);
      const what = document.createElement('span');
      what.textContent = row.label;
      if (row.state === 'ok' && row.master) {
        what.title = `${row.master} · ${row.ageDays} days old`;
      } else {
        const why = document.createElement('span');
        why.className = 'why';
        why.textContent = ` — ${row.detail}`;
        what.appendChild(why);
      }
      line.appendChild(what);
      rows.appendChild(line);
    }
  }

  /* ------------------------------------------------ bringing a master in */

  const imp = { found: null };

  function openImport() {
    imp.found = null;
    $('calImportFound').textContent = 'Choose a file and press Read it.';
    $('calImportType').value = '';
    for (const id of ['calImportFilter', 'calImportExposure', 'calImportTemp',
      'calImportGain', 'calImportOffset', 'calImportScope']) {
      $(id).value = '';
    }
    $('calImportBin').value = '1';
    const scope = (cal.loaded && cal.loaded.telescopes) || [];
    if (scope.length === 1) $('calImportScope').value = scope[0].name;
    $('calImportDialog').showModal();
  }

  async function readImport() {
    const path = $('calImportPath').value.trim();
    if (!path) { app.toast('Type or choose a file first', 'error'); return; }
    let found;
    try {
      found = await app.api('/api/calibration/library/inspect', 'POST', { path });
    } catch (error) {
      $('calImportFound').textContent = error.message;
      app.toast(error.message, 'error');
      return;
    }
    imp.found = found;
    const bits = [found.format, `${found.width}×${found.height}`];
    if (found.frames) bits.push(`${found.frames} frames`);
    if (found.software) bits.push(found.software);
    bits.push(found.type ? `says it is a ${found.type}` : 'does not say what it is');
    $('calImportFound').textContent = bits.join(' · ');
    $('calImportType').value = found.type || '';
    $('calImportFilter').value = found.filter || '';
    $('calImportExposure').value = found.exposure ? found.exposure : '';
    $('calImportTemp').value = found.temperature === null || found.temperature === undefined
      ? '' : found.temperature;
    $('calImportGain').value = found.gain === null || found.gain === undefined ? '' : found.gain;
    $('calImportOffset').value = found.offset === null || found.offset === undefined
      ? '' : found.offset;
    $('calImportBin').value = String(found.binning || 1);
    if (found.telescope) $('calImportScope').value = found.telescope;
  }

  async function doImport() {
    const path = $('calImportPath').value.trim();
    if (!path) { app.toast('Type or choose a file first', 'error'); return; }
    const num = (id) => {
      const raw = $(id).value.trim();
      return raw === '' ? undefined : Number(raw);
    };
    const body = {
      path,
      type: $('calImportType').value || undefined,
      filter: $('calImportFilter').value.trim() || undefined,
      exposure: num('calImportExposure'),
      temperature: num('calImportTemp'),
      gain: num('calImportGain'),
      offset: num('calImportOffset'),
      binning: Number($('calImportBin').value) || 1,
      telescope: $('calImportScope').value.trim() || undefined,
    };
    if (!body.type) { app.toast('Say what the file is: a bias, dark or flat', 'error'); return; }
    let answer;
    try {
      answer = await app.api('/api/calibration/library/import', 'POST', body);
    } catch (error) { app.toast(error.message, 'error'); return; }
    app.toast(`Added ${(answer.master || {}).id || 'the master'} to the library`, 'success');
    $('calImportDialog').close();
    reloadAfterLibraryChange();
  }

  /* Anything that changes the library changes what tonight has. */
  function reloadAfterLibraryChange() {
    load();
    loadCoverage();
    loadMatch();
  }

  /** What would be applied to a frame taken right now, and why. */
  async function loadMatch() {
    const node = $('calMatchNow');
    if (!node) return;
    let result;
    try {
      result = await app.api(app.rigQuery('/api/calibration/match'));
    } catch (error) {
      node.textContent = error.message;
      return;
    }
    node.innerHTML = '';
    for (const kind of ['dark', 'flat', 'bias']) {
      const line = document.createElement('div');
      line.className = result.masters[kind] ? 'cal-match-ok' : 'cal-match-no';
      line.textContent = `${kind}: ${result.reasons[kind]}`;
      node.appendChild(line);
    }
    const scope = document.createElement('div');
    scope.className = 'muted small';
    scope.textContent = `for ${result.telescope} at `
      + `${result.want.exposure || 0}s, bin ${result.want.binning}`
      + (result.want.filter ? `, ${result.want.filter}` : '')
      + (result.want.temperature === null || result.want.temperature === undefined
        ? '' : `, ${result.want.temperature}°C`);
    node.appendChild(scope);
  }

  /* -------------------------------------------------------------- running */

  function renderRun(run) {
    const box = $('calProgress');
    // A run that failed has no results and is no longer running, which used to
    // be indistinguishable from never having pressed the button — the box was
    // hidden, and the reason it failed was written into the element inside it.
    // A failure is the one state that most needs to be on screen.
    const failed = !!(run && run.error);
    if (!run || (!run.running && !failed && !(run.results || []).length)) {
      box.hidden = true;
      cal.running = false;
      $('btnCalAbort').disabled = true;
      $('btnCalRun').disabled = false;
      return;
    }
    box.hidden = false;
    cal.running = !!run.running;
    $('btnCalAbort').disabled = !run.running;
    $('btnCalRun').disabled = !!run.running;

    $('calRunName').textContent = run.recipe || '—';
    const scopes = Object.values(run.telescopes || {}).filter(Boolean);
    $('calRunStage').textContent = run.running
      ? (scopes.join('  ·  ') || run.message || run.state)
      : (run.error || run.message || 'finished');
    $('calRunStage').classList.toggle('warn-text', !!run.error);

    const setPercent = run.sets ? (run.set / run.sets) * 100 : 0;
    $('calSetFill').style.width = `${Math.min(100, setPercent)}%`;
    $('calSetValue').textContent = run.sets ? `${run.set}/${run.sets}` : '—';
    const framePercent = run.frames ? (run.frame / run.frames) * 100 : 0;
    $('calFrameFill').style.width = `${Math.min(100, framePercent)}%`;
    $('calFrameValue').textContent = run.frames ? `${run.frame}/${run.frames}` : '—';

    renderResults(run.results || []);
  }

  function renderResults(results) {
    const host = $('calResults');
    if (!results.length) return;
    host.innerHTML = '';
    for (const item of results) {
      const row = document.createElement('div');
      row.className = item.master ? 'cal-result' : 'cal-result failed';
      const name = document.createElement('b');
      name.textContent = `${item.telescope}: ${item.set}`;
      row.appendChild(name);
      const detail = document.createElement('span');
      detail.className = 'small mono muted';
      if (item.master) {
        const bits = [`${(item.info || {}).frames} frames`,
          (item.info || {}).method];
        if (item.exposure) bits.push(`${Number(item.exposure).toFixed(2)}s`);
        if (item.measuredAdu) bits.push(`${Math.round(item.measuredAdu)} ADU`);
        if (item.darkSubtracted === false) bits.push('not dark-subtracted');
        detail.textContent = bits.filter(Boolean).join(' · ');
      } else {
        detail.textContent = item.detail || 'failed';
      }
      row.appendChild(detail);
      host.appendChild(row);
    }
  }

  function currentRecipeBody() {
    return {
      name: $('calName').value.trim() || 'Calibration',
      sets: cal.sets.map((spec) => ({
        frameType: spec.frameType,
        count: Math.max(1, Math.round(Number(spec.count) || 1)),
        exposure: Math.max(0, Number(spec.exposure) || 0),
        binning: Math.max(1, Math.round(Number(spec.binning) || 1)),
        gain: spec.gain === null || spec.gain === '' ? null : Math.round(spec.gain),
        offset: spec.offset === null || spec.offset === '' ? null
          : Math.round(spec.offset),
        filter: spec.filter || '',
        allFilters: !!spec.allFilters,
        source: spec.source === 'sky' ? 'sky' : 'panel',
        autoExposure: !!spec.autoExposure,
        followsFlat: !!spec.followsFlat,
        brightness: spec.brightness === null || spec.brightness === ''
          ? null : Math.round(spec.brightness),
      })),
    };
  }

  async function saveRecipe() {
    if (!cal.sets.length) {
      app.toast('There is nothing in this recipe to save', 'error');
      return null;
    }
    try {
      const result = await app.api('/api/calibration/recipes', 'POST',
        { ...currentRecipeBody(), id: cal.recipeId || undefined });
      cal.recipeId = result.recipe.id;
      cal.dirty = false;
      app.toast(`Saved ${result.recipe.name}`, 'success');
      await load();
      return result.recipe;
    } catch (error) {
      app.toast(error.message, 'error');
      return null;
    }
  }

  async function runNow() {
    if (!cal.sets.length) {
      app.toast('There is nothing in this recipe to run', 'error');
      return;
    }
    const flats = cal.sets.some((s) => s.frameType === 'flat');
    const darks = cal.sets.some((s) => s.frameType !== 'flat');
    const warning = [
      flats ? 'the flat panel will come on' : '',
      darks ? 'the cover must be closed for the darks' : '',
    ].filter(Boolean).join(', and ');
    const ok = await app.confirmAction(
      `Shoot ${cal.sets.length} set(s), about ${duration(estimateSeconds())} of `
      + `frames? ${warning ? `Make sure ${warning}.` : ''}`,
      { title: 'Run calibration', confirmLabel: 'Run' });
    if (!ok) return;

    try {
      await app.api('/api/calibration/run', 'POST', {
        ...currentRecipeBody(),
        recipeId: cal.dirty ? undefined : (cal.recipeId || undefined),
        rigId: $('calScope').value || undefined,
      });
    } catch (error) {
      app.toast(error.message, 'error');
      return;
    }
    app.toast('Calibration run started', 'success');
  }

  async function addToPlan() {
    // A task in the plan refers to a saved recipe, so an unsaved one is saved
    // first rather than quietly running something that is not on disk.
    let recipeId = cal.recipeId;
    if (!recipeId || cal.dirty) {
      const saved = await saveRecipe();
      if (!saved) return;
      recipeId = saved.id;
    }
    try {
      await app.api('/api/plan/calibration', 'POST', {
        recipeId,
        rigId: $('calScope').value || undefined,
      });
    } catch (error) {
      app.toast(error.message, 'error');
      return;
    }
    app.toast('Added to the plan — set its time on the Plan tab', 'success');
    app.showTab('plan');
  }

  /* ---------------------------------------------------------------- wiring */

  async function load() {
    let payload;
    try {
      payload = await app.api('/api/calibration');
    } catch (error) {
      app.toast(error.message, 'error');
      return;
    }
    cal.loaded = payload;

    const select = $('calRecipe');
    const previous = cal.recipeId;
    select.innerHTML = '';
    select.appendChild(new Option('New recipe', ''));
    for (const recipe of payload.recipes || []) {
      select.appendChild(new Option(
        `${recipe.name} (${recipe.sets.length} sets)`, recipe.id));
    }
    select.value = previous || '';

    const scope = $('calScope');
    const chosen = scope.value;
    scope.innerHTML = '';
    scope.appendChild(new Option('Every telescope', ''));
    for (const rig of payload.telescopes || []) {
      scope.appendChild(new Option(rig.name, rig.id));
    }
    scope.value = chosen;
    // With one telescope there is nothing to choose between.
    scope.hidden = (payload.telescopes || []).length < 2;

    renderLibrary(payload);
    fillSettings((payload.library || {}).settings);
    renderRun(payload.run);
    // Re-rendering the rows picks up a filter wheel that has since connected.
    renderSets();
  }

  /** The library alone, after its folder may have moved. Leaves the recipe
      and this tab's own settings form as they are, edits and all. */
  async function refreshLibrary() {
    let payload;
    try {
      payload = await app.api('/api/calibration');
    } catch (error) {
      return;
    }
    cal.loaded = payload;
    renderLibrary(payload);
    loadMatch();
    loadCoverage();
  }

  function bind() {
    $('calRecipe').addEventListener('change', async () => {
      const id = $('calRecipe').value;
      if (!id) { loadRecipe(null); return; }
      if (cal.dirty) {
        const ok = await app.confirmAction(
          'Switch recipes? The changes to this one have not been saved.',
          { title: 'Unsaved changes', confirmLabel: 'Switch' });
        if (!ok) { $('calRecipe').value = cal.recipeId || ''; return; }
      }
      const recipe = (cal.loaded.recipes || []).find((r) => r.id === id);
      loadRecipe(recipe);
    });

    $('btnCalNew').addEventListener('click', () => loadRecipe(null));

    $('btnCalSuggest').addEventListener('click', async () => {
      try {
        const suggested = await app.api('/api/calibration/recipes/suggested');
        loadRecipe({ name: suggested.name, sets: suggested.sets });
        cal.dirty = true;
        renderEstimate();
        app.toast(suggested.setpoint === null || suggested.setpoint === undefined
          ? 'Built from what is connected now'
          : `Built from what is connected now — the darks will only be valid at `
            + `${suggested.setpoint}°C`, 'info');
      } catch (error) {
        app.toast(error.message, 'error');
      }
    });

    $('calName').addEventListener('input', markDirty);
    $('btnCalSave').addEventListener('click', saveRecipe);

    $('btnCalDelete').addEventListener('click', async () => {
      if (!cal.recipeId) { loadRecipe(null); return; }
      const ok = await app.confirmAction(
        `Delete the recipe ${$('calName').value}? The masters it built stay in `
        + 'the library.',
        { title: 'Delete recipe', confirmLabel: 'Delete', danger: true });
      if (!ok) return;
      await app.send(`/api/calibration/recipes/${cal.recipeId}`, 'DELETE', null,
        'Recipe deleted');
      loadRecipe(null);
      load();
    });

    $('btnCalAddSet').addEventListener('click', () => {
      cal.sets.push(blankSet($('calAddType').value));
      markDirty();
      renderSets();
    });

    $('btnCalAllFilters').addEventListener('click', () => {
      const names = (cal.loaded && cal.loaded.filters) || [];
      if (!names.length) {
        app.toast('No filters are known — set them in Equipment first', 'error');
        return;
      }
      // One row, not one per filter: the wheel is walked when the set is
      // shot, so the recipe stays readable however many filters there are.
      if (cal.sets.some((s) => s.frameType === 'flat' && s.allFilters)) {
        app.toast('There is already a set of flats for every filter', 'info');
        return;
      }
      cal.sets.push({ ...blankSet('flat'), allFilters: true, filter: '' });
      cal.dirty = true;
      renderSets();
      app.toast(`Flats for all ${names.length} filters, in one line`, 'success');
    });

    $('btnCalImport').addEventListener('click', openImport);
    $('btnCalImportRead').addEventListener('click', readImport);
    $('btnCalImportGo').addEventListener('click', doImport);
    $('btnCalImportBrowse').addEventListener('click', async () => {
      try {
        const chosen = await app.choosePath('pick_master', {
          title: 'Import a master frame', start: $('calImportPath').value || '',
          files: ['.fit', '.fits', '.fts', '.xisf'] });
        if (chosen) { $('calImportPath').value = chosen; readImport(); }
      } catch (error) { app.toast(String(error), 'error'); }
    });
    $('btnCalRun').addEventListener('click', runNow);
    $('btnCalToPlan').addEventListener('click', addToPlan);
    $('btnCalAbort').addEventListener('click', async () => {
      const ok = await app.confirmAction(
        'Stop the calibration run? The set being shot is abandoned; masters '
        + 'already built are kept.',
        { title: 'Stop calibration', confirmLabel: 'Stop', danger: true });
      if (!ok) return;
      app.send('/api/calibration/abort', 'POST', null, 'Stopping');
    });

    $('btnCalSaveSettings').addEventListener('click', saveSettings);
    $('calApplyTo').addEventListener('change', saveSettings);
    $('calSkyPointing').addEventListener('change', updateSkyControls);
    $('calSkyTracking').addEventListener('change', updateSkyControls);

    $('btnCalLibraryDir').addEventListener('click', () => {
      const button = $('btnEquipment');
      if (button) button.click();
      const field = $('calRoot');
      if (field) { field.focus(); field.select(); }
    });
  }

  function init() {
    bind();
    loadRecipe(null);
    let matchedAt = 0;
    app.onStatus((status, tab) => {
      if (status && status.calibration) renderRun(status.calibration);
      if (tab !== 'calibrate') return;
      if (!cal.loaded) load();
      // What the library covers depends on the filter in the wheel and the
      // sensor temperature, which both move — but not fast enough to ask on
      // every status frame.
      if (Date.now() - matchedAt > 10000) {
        matchedAt = Date.now();
        loadMatch();
        loadCoverage();
        // The flat spot moves with the sky, and the Sun's altitude is the
        // thing that says whether it is worth starting yet.
        loadFlatSpot();
      }
    });
    // The library is worth knowing about before the tab is opened: it decides
    // whether tonight's frames get calibrated at all.
    load();
    // A new library folder chosen in Settings shows here straight away.
    let libraryDir = null;
    app.onSettings((settings) => {
      const dir = (settings.calibration || {}).libraryDirectory || '';
      if (dir !== libraryDir && cal.loaded) refreshLibrary();
      libraryDir = dir;
    });
  }

  document.addEventListener('DOMContentLoaded', init);
}());
