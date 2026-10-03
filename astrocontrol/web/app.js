/* Starfront UI */
'use strict';

const $ = (id) => document.getElementById(id);

/* Everything that can be connected in the Equipment dialog. `tag` is the little
   name chip in the panel heading; the flat panel has no panel of its own yet. */
const KINDS = [
  { key: 'camera', label: 'Camera', tag: 'cam-name' },
  { key: 'filterwheel', label: 'Filter', tag: 'fw-name' },
  { key: 'focuser', label: 'Focuser', tag: 'foc-name' },
  { key: 'rotator', label: 'Rotator', tag: 'rot-name' },
  { key: 'mount', label: 'Mount', tag: 'mnt-name' },
  { key: 'guider', label: 'Guider', tag: 'gui-name' },
  { key: 'flatpanel', label: 'Flat Panel', tag: null },
  // The camera watching the telescope rather than the sky. An ordinary camera
  // to every backend; what differs is only what is done with the frames.
  { key: 'piercam', label: 'Pier Cam', tag: 'pier-name' },
  // The observatory rather than the telescope: what the weather is doing, what
  // is over the telescope, and what is switched on.
  { key: 'safetymonitor', label: 'Safety', tag: null },
  { key: 'dome', label: 'Dome / Roof', tag: 'dome-name' },
  { key: 'switch', label: 'Power', tag: null },
];

const BACKEND_LABELS = { ascom: 'ASCOM', zwo: 'ZWO direct', alpaca: 'Alpaca',
  phd2: 'PHD2' };

/* What the server sends in place of a stored password. Never sent back. */
const MASKED = '***';

/* Slots that exist once however many telescopes are on the mount. They live on
   the master, and the other telescopes' rows do not show them. */
const MASTER_KINDS = ['mount', 'guider', 'piercam', 'safetymonitor', 'dome',
  'switch'];

/* Tabs that give the device panels' 330px back to their own content. Planning
   is reading and arranging, not driving a camera, and the plan boxes carry a
   transit graph, a framing preview and a night-by-night table that all want
   real width. */
const LEFTLESS_TABS = ['plan', 'planner', 'sky', 'collab'];

const state = {
  status: null,
  // Which telescope the camera, focuser, filter and rotator panels drive. Null
  // means the master, which is the only telescope most rigs ever have.
  rig: null,
  equipment: null,
  drivers: {},
  images: [],
  currentId: null,
  currentRecord: null,
  currentStats: null,
  lastLatestId: null,
  cameraSynced: false,
  // The filter names the buttons were last built from, so a rename rebuilds
  // them. Null means "not connected", which is a different thing from "no
  // filters".
  filterNames: null,
  outputSynced: false,
  // The pier-cam frame currently on screen, so a new one is fetched only when
  // the server says there is one.
  pierToken: null,
  // Which filter-offset run's results have already been pulled into the form.
  offsetRunFinished: null,
  settings: null,
  // The rectangle of sky drawn in the Planner and waiting to become a
  // collaboration project. Lives here rather than in either tab because it is
  // handed from one to the other.
  collabRegion: null,
  tab: 'image',
  solveResult: null,
  // Panels the operator can call up from the topbar; off until asked for.
  panels: { observatory: false, piercam: false, stretch: false, session: false,
    run: false, log: false },
  // The device panels down the left. The camera panel is always there because
  // it is what a frame is taken with; the rest are called up when wanted, so
  // the window while frames come in is an image and not a cockpit.
  devicePanels: { focuser: false, rotator: false, mount: false, guider: false },
  // What the rig is doing, for the indicator under the topbar.
  activity: { kind: '', since: 0 },
};

/* The planetarium lives in its own file and only ever reads what arrives on the
   status socket, so it subscribes here rather than opening a second one. */
const statusListeners = [];

/* Tabs that show a setting of their own - the calibration library's folder -
   hear about a save here, rather than holding what they read at startup. */
const settingsListeners = [];

/* pywebview injects this bridge when we run as a desktop window; in a plain
   browser it is absent and Browse… falls back to `pickPath`. */
const nativeApi = () => (window.pywebview && window.pywebview.api) || null;

/* ------------------------------------------------------------------ api */

async function api(path, method = 'GET', body = null) {
  const options = { method, headers: {} };
  if (body !== null) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = { detail: text }; }
  if (!response.ok) {
    const message = (payload && payload.detail) || `${response.status} ${response.statusText}`;
    throw new Error(typeof message === 'string' ? message : JSON.stringify(message));
  }
  return payload;
}

/* ------------------------------------------------------------- telescopes */

/* Nearly every device call is about one telescope. Rather than thread an id
   through every call site, these append it to the path — and append nothing at
   all while the master is selected, so a single-telescope rig makes exactly the
   requests it always did. */

function rigQuery(path, rigId = state.rig) {
  if (!rigId || rigId === masterRigId()) return path;
  return path + (path.includes('?') ? '&' : '?') + `rig=${encodeURIComponent(rigId)}`;
}

const masterRigId = () => (state.status && state.status.masterRig) || null;

/** Every telescope's live state, master first. */
function rigList() {
  return (state.status && state.status.rigs) || [];
}

/** The selected telescope's slice of the status payload. */
function currentRig() {
  const all = rigList();
  if (!all.length) return null;
  return all.find((rig) => rig.id === state.rig) || all.find((rig) => rig.role === 'master')
    || all[0];
}

const isMasterSelected = () => {
  const rig = currentRig();
  return !rig || rig.role === 'master';
};

/** The slots a telescope actually has: only the master carries mount and guider. */
const kindsFor = (rig) =>
  KINDS.filter((kind) => !MASTER_KINDS.includes(kind.key)
    || !rig || rig.role === 'master');

function selectRig(rigId) {
  if (state.rig === rigId) return;
  state.rig = rigId;
  // The panels are about to describe a different camera and focuser, so
  // anything cached about the old one has to go.
  state.cameraSynced = false;
  state.filterNames = null;
  state.outputSynced = false;
  buildConnectRows();
  refreshDrivers();
  loadSettings();
  if (state.status) applyStatus(state.status);
}

/** Fire an API call and surface any driver error as a toast. */
function send(path, method, body, okMessage) {
  return api(path, method, body)
    .then((result) => { if (okMessage) toast(okMessage, 'success'); return result; })
    .catch((error) => {
      toast(error.message, 'error');
      // So a caller that also reports failures does not toast the same thing
      // twice.
      if (error && typeof error === 'object') error.reported = true;
      throw error;
    });
}

/* ----------------------------------------------------------------- asking */

/* WebView2 handles window.confirm and window.prompt itself: both return the
   default straight away without ever showing the operator anything, so a
   native confirm() silently answers "yes". Everything that needs an actual
   answer goes through here. */

let askResolve = null;

function closeAsk(value) {
  const dialog = $('askDialog');
  if (dialog.open) dialog.close();
  const resolve = askResolve;
  askResolve = null;
  if (resolve) resolve(value);
}

function ask({ title = 'Confirm', message = '', confirmLabel = 'OK',
  danger = false, input = null } = {}) {
  // Never leave an earlier question hanging if a second one is asked.
  if (askResolve) closeAsk(input === null ? false : null);

  $('askTitle').textContent = title;
  $('askMessage').textContent = message;
  $('askConfirm').textContent = confirmLabel;
  $('askConfirm').classList.toggle('danger', !!danger);
  $('askConfirm').classList.toggle('primary', !danger);

  const wantsText = input !== null;
  $('askInputRow').hidden = !wantsText;
  if (wantsText) $('askInput').value = input;

  $('askDialog').showModal();
  if (wantsText) { $('askInput').focus(); $('askInput').select(); }
  else $('askConfirm').focus();

  return new Promise((resolve) => { askResolve = resolve; });
}

const confirmAction = (message, options = {}) =>
  ask({ message, ...options }).then((answer) => answer !== false && answer !== null);

const askForText = (message, value = '', options = {}) =>
  ask({ message, input: value, confirmLabel: 'Save', ...options });

function bindAsk() {
  $('askCancel').addEventListener('click', (event) => {
    event.preventDefault();
    closeAsk($('askInputRow').hidden ? false : null);
  });
  $('askConfirm').addEventListener('click', (event) => {
    event.preventDefault();
    closeAsk($('askInputRow').hidden ? true : $('askInput').value);
  });
  $('askInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); $('askConfirm').click(); }
  });
  // Escape closes a <dialog> without any button being pressed.
  $('askDialog').addEventListener('close', () => {
    if (askResolve) closeAsk($('askInputRow').hidden ? false : null);
  });
}

/* --------------------------------------------------------------- browsing */

/* Browse… in a plain browser. The desktop window has Windows' own dialog; a
   browser's file chooser hides the path and lives on whatever device the page
   is open on, while the folder that matters is on the capture PC. So the
   server lists that machine's folders and this walks them. */

const pick = { resolve: null, files: null, path: null, parent: null, selected: null };

function closePick(value) {
  const dialog = $('pickDialog');
  if (dialog.open) dialog.close();
  const resolve = pick.resolve;
  pick.resolve = null;
  if (resolve) resolve(value);
}

function pickRow(kind, name, onPick, onOpen) {
  const row = document.createElement('li');
  row.className = kind;
  row.setAttribute('role', 'option');
  const icon = document.createElement('span');
  icon.className = 'pick-icon';
  icon.textContent = kind === 'file' ? '·' : (kind === 'drive' ? '◆' : '▸');
  const label = document.createElement('span');
  label.textContent = name;
  row.append(icon, label);
  row.title = name;
  row.addEventListener('click', () => onPick(row));
  if (onOpen) row.addEventListener('dblclick', onOpen);
  return row;
}

function updatePickConfirm() {
  const button = $('pickConfirm');
  if (pick.files) {
    button.textContent = 'Open';
    button.disabled = !pick.selected;
  } else {
    button.textContent = 'Use this folder';
    button.disabled = !pick.path;
  }
}

async function showPickFolder(path) {
  const query = new URLSearchParams({ path: path || '' });
  if (pick.files) query.set('files', pick.files.join(','));
  let listing;
  try {
    listing = await api(`/api/browse?${query}`);
  } catch (error) {
    $('pickNote').textContent = error.message;
    return;
  }
  pick.path = listing.path;
  pick.parent = listing.parent;
  pick.selected = null;
  $('pickPath').value = listing.path || '';
  $('pickPath').placeholder = listing.path ? '' : 'This PC';
  $('pickUp').disabled = !listing.parent;
  $('pickNote').textContent = listing.truncated
    ? 'Only the first entries are shown; type the rest of the path to go further.' : '';

  const list = $('pickList');
  list.replaceChildren();
  const select = (row, full) => {
    list.querySelectorAll('.selected').forEach((other) => other.classList.remove('selected'));
    row.classList.add('selected');
    pick.selected = full;
    updatePickConfirm();
  };
  const join = (name) => (listing.path ? listing.path.replace(/[\\/]$/, '')
    + (listing.path.includes('/') && !listing.path.includes('\\') ? '/' : '\\') + name : name);

  for (const name of listing.dirs) {
    const full = join(name);
    // One click opens a folder; there is nothing to select inside the drive list.
    list.append(pickRow(listing.path ? 'dir' : 'drive', name, () => showPickFolder(full)));
  }
  for (const name of listing.files) {
    const full = join(name);
    list.append(pickRow('file', name, (row) => select(row, full),
      () => closePick(full)));
  }
  if (!list.children.length) {
    const empty = document.createElement('li');
    empty.className = 'empty';
    empty.textContent = pick.files ? 'No matching files or folders here' : 'No folders here';
    list.append(empty);
  }
  list.scrollTop = 0;
  updatePickConfirm();
}

/** Choose a path on the capture PC from inside the page.

    `files`: extensions to offer (`['.fits', '.fit']`, or `['*']`), or absent
    to choose a folder. Resolves to the full path, or null if cancelled. */
function pickPath({ title = 'Choose a folder', start = '', files = null } = {}) {
  if (pick.resolve) closePick(null);
  pick.files = files;
  $('pickTitle').textContent = title;
  $('pickNote').textContent = '';
  $('pickList').replaceChildren();
  $('pickDialog').showModal();
  showPickFolder(start);
  return new Promise((resolve) => { pick.resolve = resolve; });
}

/** Browse…: Windows' dialog in the desktop window, the page's own in a browser.
    `nativeMethod` names the desktop bridge call (`pick_folder`, `pick_file`…). */
async function choosePath(nativeMethod, options = {}) {
  const native = nativeApi();
  if (native && native[nativeMethod]) return native[nativeMethod](options.start || '');
  return pickPath(options);
}

function bindPick() {
  $('pickUp').addEventListener('click', (event) => {
    event.preventDefault();
    if (pick.parent) showPickFolder(pick.parent);
  });
  $('pickPath').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); showPickFolder($('pickPath').value); }
  });
  $('pickCancel').addEventListener('click', (event) => {
    event.preventDefault();
    closePick(null);
  });
  $('pickConfirm').addEventListener('click', (event) => {
    event.preventDefault();
    closePick(pick.files ? pick.selected : pick.path);
  });
  $('pickDialog').addEventListener('close', () => {
    if (pick.resolve) closePick(null);
  });
}

/* A script error in the desktop window reaches nothing: there is no console to
   look at, and the page just quietly stops updating. Sending it to the session
   log puts it where the operator is already looking, which is the whole reason
   /api/log exists. */
function reportBrokenScript(what, error) {
  const detail = (error && (error.stack || error.message)) || String(error);
  console.error(what, error);
  try {
    fetch('/api/log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level: 'error', message: `${what}: ${detail}`.slice(0, 300) }),
    }).catch(() => {});
  } catch { /* if even this throws, there is nothing left to try */ }
}

function watchForScriptErrors() {
  window.addEventListener('error', (event) => {
    // Failed images and other resource loads bubble here too; they are not it.
    if (event.target !== window && event.target && event.target.tagName) return;
    reportBrokenScript(
      `Script error at ${(event.filename || '?').split('/').pop()}:${event.lineno}`,
      event.error || event.message);
  });
  window.addEventListener('unhandledrejection', (event) => {
    reportBrokenScript('Unhandled promise rejection', event.reason);
  });
}

function toast(message, kind = 'info', ms = 4200) {
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.textContent = message;
  $('toasts').appendChild(node);
  setTimeout(() => node.remove(), ms);
}

/* ------------------------------------------------------- equipment panel */

function buildConnectRows() {
  const host = $('connectList');
  host.innerHTML = '';
  for (const kind of kindsFor(currentRig())) {
    const row = document.createElement('div');
    row.className = 'connect-row';
    // PHD2 is not chosen from a driver list; it is an address we dial.
    const picker = kind.key === 'guider'
      ? '<input type="text" id="guiderAddress" value="127.0.0.1:4400" spellcheck="false">'
      : `<select data-driver-select="${kind.key}"></select>`;
    // PHD2 has no driver settings to pin — its "driver" is a socket address.
    const setup = kind.key === 'guider' ? '<span></span>'
      : `<button class="btn ghost icon" data-setup="${kind.key}"
                 title="Which device this driver should use">⚙</button>`;
    row.innerHTML =
      `<label>${kind.label}</label>${picker}${setup}` +
      `<button class="btn" data-connect="${kind.key}">Connect</button>`;
    host.appendChild(row);
  }
  host.querySelectorAll('[data-connect]').forEach((button) => {
    // Connecting is slow enough to look like nothing happened — PHD2 alone is
    // allowed 45s to start and 150s to bring its equipment up — so the button
    // says what it is doing. And anything thrown in here is reported: a click
    // handler that rejects quietly is a Connect button that does nothing at all
    // and never says why, which is the worst way for this to fail at 2am.
    button.addEventListener('click', async () => {
      if (button.disabled) return;
      const was = button.textContent;
      button.disabled = true;
      button.textContent = 'Working…';
      try {
        await toggleConnection(button.dataset.connect);
      } catch (error) {
        // `send` has already toasted its own failures; this catches the rest.
        if (!(error && error.reported)) toast(String(error && error.message || error), 'error');
      } finally {
        button.disabled = false;
        button.textContent = was;
        updateConnectButtons(currentDevices());
      }
    });
  });
  host.querySelectorAll('[data-setup]').forEach((button) => {
    button.addEventListener('click', () => openDeviceSetup(button.dataset.setup));
  });
  preselectRemembered();
}

/* Show the driver each slot last connected to, so opening the dialog on a rig
   that is switched off still says what it is made of. */
function preselectRemembered() {
  const telescope = (state.equipment && state.equipment.telescopes
    .find((rig) => rig.id === (state.rig || masterRigId()))) || null;
  if (!telescope) return;
  for (const [kind, spec] of Object.entries(telescope.devices || {})) {
    if (kind === 'guider') {
      const field = $('guiderAddress');
      if (field && !field.disabled) field.value = spec.driverId;
      continue;
    }
    const select = document.querySelector(`[data-driver-select="${kind}"]`);
    if (!select) continue;
    let wanted = [...select.options].find((option) =>
      option.value.startsWith(`${spec.backend}|${spec.driverId}|`));
    if (!wanted) {
      // A ProgID set by hand in the device setup dialog, or one whose driver is
      // not registered on this machine. It is still this telescope's choice, so
      // it belongs on the list rather than silently falling back to another.
      wanted = new Option(`${spec.name || spec.driverId}  (set by hand)`,
        `${spec.backend}|${spec.driverId}|${spec.name || spec.driverId}`);
      select.appendChild(wanted);
    }
    select.value = wanted.value;
  }
}

/* ------------------------------------------------- telescopes and profiles */

async function loadEquipment() {
  try {
    state.equipment = await api('/api/equipment');
  } catch (error) {
    console.error(error);
    return null;
  }
  renderEquipment();
  return state.equipment;
}

function renderEquipment() {
  const data = state.equipment;
  if (!data) return;
  const telescopes = data.telescopes || [];
  const selected = state.rig || data.masterRig;

  // The tab strip inside the dialog.
  const tabs = $('rigTabs');
  tabs.innerHTML = '';
  for (const rig of telescopes) {
    const button = document.createElement('button');
    button.className = `btn small rig-tab${rig.id === selected ? ' active' : ''}`;
    button.textContent = rig.name;
    if (rig.role === 'master') button.classList.add('master');
    button.title = rig.role === 'master'
      ? 'Master: carries the mount and the guider'
      : 'Follows the master: same pointing, focuses and exposes in step';
    button.addEventListener('click', () => { selectRig(rig.id); renderEquipment(); });
    tabs.appendChild(button);
  }

  const current = telescopes.find((rig) => rig.id === selected) || telescopes[0];
  const isMaster = !current || current.role === 'master';
  $('btnRigMaster').disabled = isMaster;
  $('btnRigRemove').disabled = isMaster;
  $('btnRigAdd').disabled = telescopes.length >= 4;
  document.querySelectorAll('[data-role="rig-scope"]').forEach((tag) => {
    tag.textContent = current ? current.name : '—';
    tag.hidden = telescopes.length < 2;
  });
  $('afScopeNote').hidden = telescopes.length < 2;
  $('btnFocusAll').hidden = telescopes.length < 2;

  // The topbar switcher, which only earns its space once there are two.
  const strip = $('rigSwitch');
  strip.hidden = telescopes.length < 2;
  strip.innerHTML = '';
  for (const rig of telescopes) {
    const button = document.createElement('button');
    button.className = `btn small ghost${rig.id === selected ? ' active' : ''}`;
    button.textContent = rig.name;
    button.title = rig.role === 'master' ? 'Master telescope' : 'Follows the master';
    button.addEventListener('click', () => { selectRig(rig.id); renderEquipment(); });
    strip.appendChild(button);
  }

  const profiles = $('profileSelect');
  const active = (data.activeProfile || {}).id || '';
  profiles.innerHTML = '';
  if (!(data.profiles || []).length) {
    profiles.appendChild(new Option('no profiles saved yet', ''));
  }
  for (const profile of data.profiles || []) {
    profiles.appendChild(new Option(
      `${profile.name}  (${(profile.telescopes || []).join(', ')})`, profile.id));
  }
  if (active) profiles.value = active;
  $('btnProfileLoad').disabled = !(data.profiles || []).length;
  $('btnProfileDelete').disabled = !(data.profiles || []).length;
}

/* ------------------------------------------------- coming over from NINA */

/* The importer shows its plan before it writes: every setting that would
   change, from what to what, grouped the way the profile groups them, and
   every N.I.N.A. setting it deliberately does not carry across and why. A
   migration that silently overwrote a working setup would cost more trust
   than it saved, and somebody arriving from N.I.N.A. is exactly the person
   whose trust the program has not yet earned. */

const NINA_SECTIONS = [
  ['site', 'Site'], ['optics', 'Optics'], ['camera', 'Camera and filters'],
  ['sequencer', 'Focus and the run'], ['guiding', 'Guiding'],
  ['solver', 'Plate solving'], ['capture', 'Files'], ['calibration', 'Flats'],
];

const NINA_LABELS = {
  latitude: 'Latitude', longitude: 'Longitude', elevation: 'Elevation (m)',
  useMount: 'Take the site from the mount',
  focalLength: 'Focal length (mm)', pixelSize: 'Pixel size (µm)',
  sensorWidth: 'Sensor width (px)', sensorHeight: 'Sensor height (px)',
  gain: 'Gain', offset: 'Offset', setpoint: 'Cooler setpoint (°C)',
  colour: 'One-shot colour camera', coolAtStart: 'Cool at start',
  warmAtEnd: 'Warm at end', filterNames: 'Filters, in slot order',
  filterOffsets: 'Focus offsets per filter', autofocusFilter: 'Autofocus filter',
  focusExposure: 'Focus exposure (s)', focusStepSize: 'Focus step size',
  focusPoints: 'Focus points', focusFramesPerPoint: 'Frames per point',
  focusAttempts: 'Focus attempts', focusMethod: 'Curve fit',
  focusBacklash: 'Backlash (steps)', useFilterOffsets: 'Use filter offsets',
  parkAtEnd: 'Park at end', meridianFlipEnabled: 'Meridian flip',
  flipAfterMinutes: 'Flip minutes after meridian',
  flipPauseMinutes: 'Pause before meridian (min)', flipSolve: 'Re-centre after flip',
  settleSeconds: 'Settle after slew (s)',
  phd2Path: 'PHD2 path', ditherPixels: 'Dither (px)', ditherRaOnly: 'Dither RA only',
  settlePixels: 'Settle (px)', settleTime: 'Settle time (s)', settleTimeout: 'Settle timeout (s)',
  astapPath: 'ASTAP', searchRadius: 'Search radius (°)', downsample: 'Downsample',
  maxStars: 'Max stars', exposure: 'Solve exposure (s)', attempts: 'Attempts',
  tolerance: 'Centring tolerance (′)', astrometryKey: 'astrometry.net key',
  astrometryUrl: 'astrometry.net URL', rootDirectory: 'Image folder',
  flatTargetAdu: 'Flat target (ADU)', flatTolerancePercent: 'Flat tolerance (%)',
  flatMaxExposure: 'Longest flat (s)', flatMinExposure: 'Shortest flat (s)',
};

const ninaState = { plan: null, chosen: new Set(), devices: true };

function showValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'object') {
    return Object.entries(value).map(([k, v]) => `${k} ${v > 0 ? '+' : ''}${v}`).join(', ');
  }
  if (typeof value === 'boolean') return value ? 'on' : 'off';
  return String(value);
}

async function openNinaImport() {
  const dialog = $('ninaDialog');
  $('ninaPreview').innerHTML = '';
  $('ninaSummary').textContent = '';
  $('btnNinaApply').disabled = true;
  $('ninaNote').textContent = 'Looking for N.I.N.A. profiles…';
  dialog.showModal();
  let listing;
  try {
    listing = await api('/api/nina/profiles');
  } catch (error) {
    $('ninaNote').textContent = error.message;
    return;
  }
  const select = $('ninaProfile');
  select.innerHTML = '';
  if (!listing.found) {
    $('ninaNote').textContent = `No N.I.N.A. profiles were found in ${listing.folder}. `
      + 'N.I.N.A. has to have been run on this PC at least once.';
    return;
  }
  for (const row of listing.profiles) {
    select.appendChild(new Option(`${row.name} — last used ${row.lastUsedText}`, row.id));
  }
  await previewNina();
}

async function previewNina() {
  const id = $('ninaProfile').value;
  if (!id) return;
  $('ninaNote').textContent = 'Reading the profile…';
  $('btnNinaApply').disabled = true;
  let plan;
  try {
    plan = await api(rigQuery(`/api/nina/preview?profile=${encodeURIComponent(id)}`));
  } catch (error) {
    $('ninaNote').textContent = error.message;
    return;
  }
  ninaState.plan = plan;
  ninaState.chosen = new Set(Object.keys(plan.settings || {}));
  ninaState.devices = true;
  const host = $('ninaPreview');
  host.innerHTML = '';

  let changes = 0;
  for (const [section, label] of NINA_SECTIONS) {
    const values = (plan.settings || {})[section];
    if (!values || !Object.keys(values).length) continue;
    const block = document.createElement('div');
    block.className = 'nina-block';
    const head = document.createElement('label');
    head.className = 'inline nina-head';
    const tick = document.createElement('input');
    tick.type = 'checkbox';
    tick.checked = true;
    tick.addEventListener('change', () => {
      if (tick.checked) ninaState.chosen.add(section); else ninaState.chosen.delete(section);
      block.classList.toggle('off', !tick.checked);
      summarise();
    });
    head.appendChild(tick);
    head.appendChild(document.createTextNode(` ${label}`));
    block.appendChild(head);
    const table = document.createElement('div');
    table.className = 'nina-rows';
    for (const [key, value] of Object.entries(values)) {
      const now = ((plan.current || {})[section] || {})[key];
      const same = JSON.stringify(now) === JSON.stringify(value);
      if (!same) changes += 1;
      const row = document.createElement('div');
      row.className = `nina-row${same ? ' same' : ''}`;
      row.innerHTML = '<span class="nina-key"></span><span class="nina-now mono"></span>'
        + '<span class="nina-arrow">→</span><span class="nina-new mono"></span>'
        + '<span class="nina-from small muted"></span>';
      row.querySelector('.nina-key').textContent = NINA_LABELS[key] || key;
      row.querySelector('.nina-now').textContent = showValue(now);
      row.querySelector('.nina-new').textContent = showValue(value);
      row.querySelector('.nina-from').textContent = (plan.from || {})[`${section}.${key}`] || '';
      table.appendChild(row);
    }
    block.appendChild(table);
    host.appendChild(block);
  }

  const devices = Object.entries(plan.devices || {});
  if (devices.length) {
    const block = document.createElement('div');
    block.className = 'nina-block';
    const head = document.createElement('label');
    head.className = 'inline nina-head';
    const tick = document.createElement('input');
    tick.type = 'checkbox';
    tick.checked = true;
    tick.addEventListener('change', () => {
      ninaState.devices = tick.checked;
      block.classList.toggle('off', !tick.checked);
      summarise();
    });
    head.appendChild(tick);
    head.appendChild(document.createTextNode(` Drivers for ${plan.rigName || 'this telescope'}`));
    block.appendChild(head);
    const table = document.createElement('div');
    table.className = 'nina-rows';
    for (const [kind, spec] of devices) {
      const row = document.createElement('div');
      row.className = 'nina-row';
      row.innerHTML = '<span class="nina-key"></span><span class="nina-new mono" style="grid-column: 2 / 5"></span>'
        + '<span class="nina-from small muted"></span>';
      row.querySelector('.nina-key').textContent = kind;
      row.querySelector('.nina-new').textContent = `${spec.name} (${spec.driverId})`;
      row.querySelector('.nina-from').textContent = 'remembered, not connected';
      table.appendChild(row);
    }
    block.appendChild(table);
    host.appendChild(block);
  }

  if ((plan.leftOut || []).length) {
    const block = document.createElement('div');
    block.className = 'nina-block left-out';
    block.innerHTML = '<b class="small">Left where it is, on purpose</b>';
    const list = document.createElement('ul');
    list.className = 'small muted';
    for (const line of plan.leftOut) {
      const item = document.createElement('li');
      item.textContent = line;
      list.appendChild(item);
    }
    block.appendChild(list);
    host.appendChild(block);
  }

  $('ninaNote').textContent = `From ${plan.profile.name}, last used by N.I.N.A. on `
    + `${plan.profile.lastUsed ? plan.profile.lastUsed.slice(0, 10) : '?'}. Rows in grey `
    + 'already match. Nothing is written until you press Import; drivers are '
    + 'remembered, not connected.';
  function summarise() {
    const sections = [...ninaState.chosen].length;
    $('ninaSummary').textContent = `${sections} group${sections === 1 ? '' : 's'}`
      + (ninaState.devices && devices.length ? ' and the drivers' : '')
      + ` · ${changes} setting${changes === 1 ? '' : 's'} differ`;
    $('btnNinaApply').disabled = !sections && !(ninaState.devices && devices.length);
  }
  summarise();
}

async function applyNina() {
  const plan = ninaState.plan;
  if (!plan) return;
  const button = $('btnNinaApply');
  button.disabled = true;
  try {
    const result = await api(rigQuery('/api/nina/import'), 'POST', {
      profile: plan.profile.id,
      sections: [...ninaState.chosen],
      devices: ninaState.devices,
    });
    state.equipment = result.equipment;
    const count = Object.values(result.settings || {}).reduce((n, keys) => n + keys.length, 0);
    toast(`Imported ${count} settings`
      + ((result.devices || []).length ? ` and ${result.devices.length} drivers` : '')
      + ' from N.I.N.A.', 'success', 7000);
    $('ninaDialog').close();
    // Everything on the dialog behind this one came from the settings just
    // replaced, so it is all read again.
    state.filterNames = null;
    await loadSettings();
    await afterEquipmentChange(state.equipment);
  } catch (error) {
    toast(error.message, 'error');
    button.disabled = false;
  }
}

async function afterEquipmentChange(result) {
  state.equipment = result;
  // The selected telescope may have just been removed, or the master moved.
  const ids = new Set((result.telescopes || []).map((rig) => rig.id));
  if (state.rig && !ids.has(state.rig)) state.rig = result.masterRig;
  state.cameraSynced = false;
  state.filterNames = null;
  state.outputSynced = false;
  renderEquipment();
  buildConnectRows();
  await refreshDrivers();
  await loadSettings();
}

async function refreshDrivers(force = false) {
  // The driver lists describe the machine and the network, so they are fetched
  // once and reused for whichever telescope's rows are on screen. The server
  // caches them too: enumerating ASCOM's registry is a slow COM round trip, and
  // this runs every time the dialog opens or the telescope changes.
  await Promise.all(KINDS.filter((k) => k.key !== 'guider').map(async (kind) => {
    try {
      const result = await api(`/api/drivers/${kind.key}${force ? '?refresh=true' : ''}`);
      state.drivers[kind.key] = result.drivers;
    } catch {
      state.drivers[kind.key] = [];
    }
  }));
  for (const kind of KINDS) {
    const select = document.querySelector(`[data-driver-select="${kind.key}"]`);
    if (!select) continue;
    const previous = select.value;
    select.innerHTML = '';
    // An explicit empty slot. A telescope with no rotator wants to say so,
    // rather than have one remembered and fail to connect it every night.
    select.appendChild(new Option('— none —', ''));
    const byBackend = {};
    for (const driver of state.drivers[kind.key] || []) {
      (byBackend[driver.backend] ||= []).push(driver);
    }
    for (const [backend, drivers] of Object.entries(byBackend)) {
      const group = document.createElement('optgroup');
      group.label = BACKEND_LABELS[backend] || backend;
      for (const driver of drivers) {
        const option = document.createElement('option');
        option.value = `${driver.backend}|${driver.id}|${driver.name}`;
        option.textContent = driver.name;
        group.appendChild(option);
      }
      select.appendChild(group);
    }
    if (previous) select.value = previous;
  }
  preselectRemembered();
}

async function toggleConnection(kind) {
  const rig = currentRig();
  const devices = currentDevices();
  const connected = devices[kind] && devices[kind].connected;
  if (connected) {
    await send(rigQuery(`/api/devices/${kind}/disconnect`), 'POST');
    if (kind === 'camera') state.cameraSynced = false;
    if (kind === 'filterwheel') state.filterNames = null;
    await loadEquipment();
    return;
  }
  if (kind === 'guider') {
    // The address field lives in the guider's own connect row, so it is absent
    // if the dialog was rebuilt for a telescope that has no guider slot. Fall
    // back to the default rather than throwing where nobody can see it.
    const field = $('guiderAddress');
    const driverId = ((field && field.value) || '127.0.0.1:4400').trim();
    await send(rigQuery('/api/devices/guider/connect'), 'POST',
      { backend: 'phd2', driverId, name: `PHD2 (${driverId})` });
    await loadEquipment();
    return;
  }
  const select = document.querySelector(`[data-driver-select="${kind}"]`);
  if (!select) return;
  if (!select.value) {
    // "— none —": this telescope has no such device. Forget it so Connect all
    // stops trying, rather than treating it as a mistake.
    const rigId = (rig && rig.id) || masterRigId();
    await send(`/api/equipment/telescopes/${rigId}/devices/${kind}`, 'DELETE');
    toast(`No ${kind} on this telescope`, 'info');
    await loadEquipment();
    return;
  }
  const [backend, driverId, name] = select.value.split('|');
  await send(rigQuery(`/api/devices/${kind}/connect`), 'POST', { backend, driverId, name });
  await loadEquipment();
}

/* --------------------------------------------------- which device, exactly */

/* Three telescopes on one driver.
 *
 * A driver that serves several identical cameras says which one it means in
 * one of two ways: a ProgID per device, or one ProgID with the choice kept in
 * its ASCOM profile — a serial number, a device index, whatever it calls it.
 * This dialog covers both. The ProgID is typed rather than only picked, and
 * the driver's own stored settings can be read, pinned per telescope, and
 * written back in the moment before that telescope connects.
 */

/** What a slot is set to now: the dropdown if it has a pick, else what is remembered. */
function slotChoice(kind) {
  const select = document.querySelector(`[data-driver-select="${kind}"]`);
  if (select && select.value) {
    const [backend, driverId, name] = select.value.split('|');
    return { backend, driverId, name };
  }
  const rig = (state.equipment && (state.equipment.telescopes || [])
    .find((one) => one.id === (state.rig || masterRigId()))) || {};
  return (rig.devices || {})[kind] || null;
}

function openDeviceSetup(kind) {
  const rig = currentRig() || {};
  const choice = slotChoice(kind) || { backend: 'ascom', driverId: '' };
  const label = (KINDS.find((one) => one.key === kind) || {}).label || kind;
  state.deviceSetup = { kind, backend: choice.backend || 'ascom',
    name: choice.name || '', read: false };

  setText('devSetupTitle', `${label} — ${rig.name || 'this telescope'}`);
  $('devSetupId').value = choice.driverId || '';

  const stored = (((state.equipment && (state.equipment.telescopes || [])
    .find((one) => one.id === (state.rig || masterRigId()))) || {})
    .devices || {})[kind] || {};
  renderDeviceSettings([], stored.options || {});
  showDeviceSetupNote(state.deviceSetup.backend);
  $('deviceSetupDialog').showModal();
}

/** Only ASCOM keeps a profile we can write; say so rather than fail later. */
function showDeviceSetupNote(backend) {
  const note = $('devSetupNote');
  const ascom = backend === 'ascom';
  note.hidden = ascom;
  if (backend === 'zwo') {
    note.textContent = 'This device is addressed directly by its serial number, '
      + 'so there is nothing to choose — the ID above already names one '
      + 'specific camera or focuser, and it stays that one across reboots.';
  } else if (!ascom) {
    note.textContent = 'Driver settings can only be read and written for ASCOM '
      + 'drivers. An Alpaca device already carries its device number in its '
      + 'address, so choosing the right one there is enough.';
  }
  $('btnDevSetupOpen').disabled = !ascom;
  $('btnDevSetupRead').disabled = !ascom;
}

/* Which of a driver's settings is the one that names the device.
   Only the driver really knows, so this is a guess used to sort the likely
   ones to the top of a list that can run to twenty-odd rows — never to hide
   anything, and never to pin anything on its own. */
const IDENTITY_WORDS = new Set(['id', 'ids', 'serial', 'sn', 'uuid', 'guid',
  'device', 'camera', 'cam', 'selected', 'instance', 'name']);

/* Matched word by word, not as a substring: SelectedCamID and SerialNumber
   have to be caught, and PreSettingIndex must not be. Bare "index" and
   "number" are deliberately absent — every driver has a setting ending in one. */
const looksLikeIdentity = (name) => String(name)
  .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
  .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
  .split(/[^A-Za-z0-9]+/)
  .some((word) => IDENTITY_WORDS.has(word.toLowerCase()));

/** The driver's settings, with a tick against the ones this telescope pins. */
function renderDeviceSettings(settings, pinned) {
  const host = $('devSetupValues');
  // Anything pinned earlier stays on the list even when the driver is not
  // installed on this machine to be asked, or the setting would vanish on open
  // and be dropped on save.
  const names = [...settings.map((one) => one.name)];
  for (const name of Object.keys(pinned)) {
    if (!names.includes(name)) names.push(name);
  }
  if (!names.length) {
    host.className = 'dev-settings muted-empty';
    host.textContent = settings.length === 0 && Object.keys(pinned).length === 0
      ? 'Press Read its settings to see what the driver keeps for this ID.'
      : 'This driver keeps nothing in its profile — it is told which device to '
        + 'use by its ProgID alone.';
    return;
  }
  const live = new Map(settings.map((one) => [one.name, one.value]));
  // Pinned first, then the ones that read like a device identity, then the
  // rest: a camera driver can keep two dozen values and only one of them says
  // which camera.
  const rank = (name) => (Object.prototype.hasOwnProperty.call(pinned, name) ? 0
    : looksLikeIdentity(name) ? 1 : 2);
  names.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));

  host.className = 'dev-settings';
  host.innerHTML = '';
  for (const name of names) {
    const row = document.createElement('label');
    row.className = `dev-setting${rank(name) < 2 ? ' likely' : ''}`;
    row.innerHTML = '<input type="checkbox"><span class="dev-setting-name"></span>'
      + '<input type="text" class="mono" spellcheck="false">';
    const [tick, label, field] = [row.querySelector('input[type="checkbox"]'),
      row.querySelector('.dev-setting-name'), row.querySelector('input[type="text"]')];
    tick.checked = Object.prototype.hasOwnProperty.call(pinned, name);
    tick.title = 'Write this value into the driver before connecting';
    label.textContent = name;
    label.title = live.has(name) ? `the driver has: ${live.get(name)}`
      : (state.deviceSetup && state.deviceSetup.read)
        ? 'this telescope pins it, but the driver does not keep it'
        : 'pinned by this telescope';
    field.value = tick.checked ? pinned[name] : (live.get(name) ?? '');
    field.dataset.setting = name;
    // Typing a value is a statement that you want it, so it ticks itself.
    field.addEventListener('input', () => { tick.checked = true; });
    host.appendChild(row);
  }
}

function readDeviceSettings() {
  const options = {};
  for (const row of $('devSetupValues').querySelectorAll('.dev-setting')) {
    const tick = row.querySelector('input[type="checkbox"]');
    const field = row.querySelector('input[type="text"]');
    if (tick.checked) options[field.dataset.setting] = field.value;
  }
  return options;
}

function bindDeviceSetup() {
  $('devSetupId').addEventListener('input', () => {
    // A ProgID typed by hand is an ASCOM one; an Alpaca id comes from a scan.
    if (state.deviceSetup) state.deviceSetup.name = '';
  });

  $('btnDevSetupRead').addEventListener('click', async () => {
    const { kind } = state.deviceSetup || {};
    const driverId = $('devSetupId').value.trim();
    if (!kind || !driverId) { toast('Give the driver ID first', 'error'); return; }
    try {
      const result = await api(rigQuery(
        `/api/drivers/${kind}/settings?driverId=${encodeURIComponent(driverId)}`));
      state.deviceSetup.read = true;
      renderDeviceSettings(result.settings || [],
        { ...(result.pinned || {}), ...readDeviceSettings() });
      if (!(result.settings || []).length) {
        toast('The driver keeps nothing in its profile', 'info');
      }
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  $('btnDevSetupOpen').addEventListener('click', async () => {
    const { kind } = state.deviceSetup || {};
    const driverId = $('devSetupId').value.trim();
    if (!kind || !driverId) { toast('Give the driver ID first', 'error'); return; }
    try {
      await api(rigQuery(`/api/drivers/${kind}/setup`), 'POST', { driverId });
      toast('The driver\'s setup window is open — it may be behind this one',
        'success', 7000);
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  $('btnDevSetupSave').addEventListener('click', async () => {
    const setup = state.deviceSetup;
    if (!setup) return;
    const driverId = $('devSetupId').value.trim();
    if (!driverId) { toast('Give the driver ID first', 'error'); return; }
    const rigId = (currentRig() || {}).id || masterRigId();
    try {
      await send(`/api/equipment/telescopes/${rigId}/devices/${setup.kind}`,
        'POST', {
          backend: setup.backend || 'ascom',
          driverId,
          name: setup.name || driverId,
          options: readDeviceSettings(),
        });
    } catch {
      return;                       // `send` has already said what went wrong
    }
    $('deviceSetupDialog').close();
    await loadEquipment();
    buildConnectRows();
    await refreshDrivers();
    toast('Saved. It is used the next time this device connects.', 'success');
  });
}

/* ------------------------------------------------------------- rendering */

function fmtHours(value) {
  if (value === null || value === undefined) return '—';
  const total = ((value % 24) + 24) % 24;
  const h = Math.floor(total);
  const m = Math.floor((total - h) * 60);
  const s = ((total - h) * 60 - m) * 60;
  return `${String(h).padStart(2, '0')}h ${String(m).padStart(2, '0')}m ${s.toFixed(1).padStart(4, '0')}s`;
}

function fmtDegrees(value) {
  if (value === null || value === undefined) return '—';
  const sign = value < 0 ? '-' : '+';
  const abs = Math.abs(value);
  const d = Math.floor(abs);
  const m = Math.floor((abs - d) * 60);
  const s = ((abs - d) * 60 - m) * 60;
  return `${sign}${String(d).padStart(2, '0')}° ${String(m).padStart(2, '0')}' ${s.toFixed(1).padStart(4, '0')}"`;
}

function setText(id, value) {
  const node = $(id);
  if (node) node.textContent = value;
}

function updatePills(devices) {
  const host = $('devicePills');
  host.innerHTML = '';
  for (const kind of kindsFor(currentRig())) {
    const device = devices[kind.key] || {};
    let cls = device.connected ? 'on' : '';
    if (device.connected && (device.slewing || device.moving || device.exposing)) cls = 'on busy';
    const pill = document.createElement('span');
    pill.className = `pill ${cls}`;
    pill.innerHTML = `<span class="dot"></span>${kind.label}`;
    host.appendChild(pill);
  }
}

/** The device map for whichever telescope the Equipment dialog is showing. */
function currentDevices() {
  const rig = currentRig();
  return (rig && rig.devices) || (state.status && state.status.devices) || {};
}

function updateConnectButtons(devices) {
  devices = devices || {};
  for (const kind of kindsFor(currentRig())) {
    const button = document.querySelector(`[data-connect="${kind.key}"]`);
    const select = document.querySelector(`[data-driver-select="${kind.key}"]`);
    if (!button) continue;
    const device = devices[kind.key] || {};
    button.textContent = device.connected ? 'Disconnect' : 'Connect';
    button.classList.toggle('danger', !!device.connected);
    if (select) select.disabled = !!device.connected;
    if (kind.key === 'guider' && $('guiderAddress')) {
      $('guiderAddress').disabled = !!device.connected;
    }
    if (!kind.tag) continue;
    const tag = document.querySelector(`[data-role="${kind.tag}"]`);
    if (tag) tag.textContent = device.connected ? device.name : '—';
  }
}

/** mm:ss, or h:mm:ss past an hour. */
function clockSpan(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return '—';
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (v) => String(v).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/* The banner across the top: is it working, and how far through. Driven from
   the sequencer's own counters rather than inferred from the camera, so it says
   the same thing the run is actually doing. */
function updateSequenceBanner(status) {
  const banner = $('seqBanner');
  if (!banner) return;
  const sequence = (status && status.sequence) || {};
  if (!sequence.running) { banner.hidden = true; return; }
  banner.hidden = false;
  banner.classList.toggle('paused', !!sequence.paused);

  const capture = (currentRig() && currentRig().capture) || status.capture || {};
  setText('seqTargetName', sequence.message ? sequence.message.split(':')[0] : '—');

  let stage = sequence.state || '';
  if (sequence.paused) stage = 'paused — finishing the current frame';
  else if (sequence.stopping) stage = 'stopping';
  setText('seqStage', stage);

  // Progress through the exposure in flight.
  const total = capture.exposureSeconds || 0;
  const elapsed = capture.elapsed;
  const exposing = capture.state === 'exposing' && total > 0 && elapsed !== null
    && elapsed !== undefined;
  const share = exposing ? Math.min(1, elapsed / total) : (capture.busy ? 1 : 0);
  $('seqFrameFill').style.width = `${share * 100}%`;
  setText('seqFrameLabel', exposing ? 'exposure' : (capture.state || 'frame'));
  setText('seqFrameValue', exposing
    ? `${clockSpan(elapsed)} / ${clockSpan(total)}   −${clockSpan(capture.remaining)}`
    : (capture.state === 'idle' ? '—' : capture.state));

  // Progress through this target's frames.
  const done = sequence.frame || 0;
  const frames = sequence.frameCount || 0;
  const slots = sequence.slots || 0;
  const slot = sequence.slot || 0;
  const targetShare = slots ? Math.min(1, slot / slots) : 0;
  $('seqTargetFill').style.width = `${targetShare * 100}%`;
  setText('seqTargetValue', slots
    ? `${slot} / ${slots}` + (sequence.panels > 1
      ? `   panel ${sequence.panel}/${sequence.panels}` : '')
    : (frames ? `${done} / ${frames}` : '—'));

  setText('seqFilter', sequence.filter || '—');
  const guider = (status.devices || {}).guider || {};
  setText('seqGuide', guider.connected
    ? (guider.guiding ? (guider.rmsTotal ? `${guider.rmsTotal.toFixed(2)}"` : 'guiding')
      : (guider.state || 'not guiding'))
    : '—');
  const guideNode = $('seqGuide');
  if (guideNode) {
    guideNode.classList.toggle('alert',
      !!guider.connected && !guider.guiding && !sequence.paused);
  }
  setText('seqElapsed', sequence.started
    ? clockSpan((Date.now() / 1000) - sequence.started) : '—');
  $('seqBannerPause').textContent = sequence.paused ? 'Resume' : 'Pause';
  $('seqBannerPause').classList.toggle('primary', !!sequence.paused);
}

function updateCamera(camera, capture) {
  const connected = !!camera.connected;
  $('btnCapture').disabled = !connected || capture.busy;
  $('btnLoop').disabled = !connected;
  $('btnAbort').disabled = !capture.busy;
  $('btnLoop').classList.toggle('active', !!capture.loop);

  // Timed by the server from when the exposure was commanded. The camera's own
  // `elapsed` comes from ASCOM's optional PercentCompleted, which plenty of
  // drivers report as 0 for the whole exposure — a progress bar that never
  // moves through a five-minute sub is worse than none.
  const total = capture.exposureSeconds
    || (capture.request && capture.request.exposure) || 0;
  const elapsed = capture.elapsed !== null && capture.elapsed !== undefined
    ? capture.elapsed : (camera.elapsed || 0);
  let percent = 0;
  if (capture.state === 'exposing' && total > 0) percent = Math.min(100, (elapsed / total) * 100);
  else if (capture.busy) percent = 100;
  $('expProgress').style.width = `${percent}%`;

  let label = capture.state;
  if (capture.state === 'exposing' && total > 0) {
    label = `exposing  ${clockSpan(elapsed)} / ${clockSpan(total)}`
      + (capture.remaining !== null && capture.remaining !== undefined
        ? `   −${clockSpan(capture.remaining)}` : '');
  }
  setText('expState', capture.error ? `error: ${capture.error}` : label);
  // While running, show which frame we are on; once idle, just the total.
  if (capture.busy) {
    setText('expCounter', capture.loop
      ? `frame ${capture.framesTaken + 1}` : 'exposing');
  } else {
    setText('expCounter', capture.framesTaken
      ? `${capture.framesTaken} frame${capture.framesTaken === 1 ? '' : 's'}` : '');
  }

  if (!connected) {
    state.cameraSynced = false;
    ['ccdTemp', 'ccdTempFull', 'ccdSetpoint', 'coolerPower']
      .forEach((id) => setText(id, '—'));
    $('coolerMeter').style.width = '0%';
    $('btnCooler').disabled = true;
    $('btnSetpoint').disabled = true;
    return;
  }

  $('btnCooler').disabled = !camera.canCool;
  $('btnSetpoint').disabled = !camera.canCool;

  const temperature = camera.temperature;
  const setpoint = camera.setpoint;
  // Twice: once on the camera panel where it is always visible, once inside the
  // Cooling fold beside the setpoint it is being judged against.
  const shown = temperature === null || temperature === undefined
    ? '—' : `${temperature.toFixed(1)} °C`;
  setText('ccdTemp', shown);
  setText('ccdTempFull', shown);
  setText('ccdSetpoint', setpoint === null || setpoint === undefined ? '—' : `${setpoint.toFixed(1)} °C`);
  const power = camera.coolerPower;
  setText('coolerPower', power === null || power === undefined ? '—' : `${power.toFixed(0)} %`);
  $('coolerMeter').style.width = `${Math.max(0, Math.min(100, power || 0))}%`;
  $('btnCooler').textContent = camera.coolerOn ? 'Cooler off' : 'Cooler on';
  $('btnCooler').classList.toggle('active', !!camera.coolerOn);

  // Sensor temperature far from the setpoint is worth noticing before a run.
  const tempNode = $('ccdTemp');
  if (tempNode && camera.coolerOn && temperature !== null && setpoint !== null) {
    tempNode.classList.toggle('alert', Math.abs(temperature - setpoint) > 1.0);
  } else if (tempNode) {
    tempNode.classList.remove('alert');
  }

  if (!state.cameraSynced) {
    state.cameraSynced = true;
    const [gainMin, gainMax] = camera.gainRange || [0, 0];
    const [offsetMin, offsetMax] = camera.offsetRange || [0, 0];
    const gain = $('gain');
    gain.min = gainMin; gain.max = Math.max(gainMax, gainMin); gain.value = camera.gain ?? gainMin;
    gain.disabled = gainMax <= gainMin;
    const offset = $('offset');
    offset.min = offsetMin; offset.max = Math.max(offsetMax, offsetMin);
    offset.value = camera.offset ?? offsetMin;
    offset.disabled = offsetMax <= offsetMin;
    const binning = $('binning');
    binning.innerHTML = '';
    for (let b = 1; b <= (camera.maxBin || 1); b += 1) {
      binning.appendChild(new Option(`${b} × ${b}`, String(b)));
    }
    binning.value = String(camera.binning || 1);
    setText('gainValue', gain.value);
    setText('offsetValue', offset.value);
    if (camera.sensor) {
      const [w, h] = camera.sensor;
      setText('frameInfo', `${w} × ${h}  ·  ${(camera.pixelSizeUm || 0).toFixed(2)} µm`);
    }
  }
}

function updateFilterWheel(wheel) {
  const host = $('filterButtons');
  if (!wheel.connected) {
    if (state.filterNames !== null) {
      host.className = 'filter-grid muted-empty';
      host.textContent = 'Not connected';
      state.filterNames = null;
    }
    return;
  }
  const names = wheel.names || [];
  // Keyed on the names, not on how many there are. Renaming seven filters to
  // seven other names leaves the count alone, so the buttons used to keep the
  // labels they were first built with — for the life of the page.
  const signature = JSON.stringify(names);
  if (state.filterNames !== signature) {
    state.filterNames = signature;
    host.className = 'filter-grid';
    host.innerHTML = '';
    names.forEach((name, index) => {
      const button = document.createElement('button');
      button.className = 'btn';
      button.textContent = name;
      button.dataset.filter = String(index);
      button.addEventListener('click', () => send(rigQuery('/api/filterwheel/position'), 'POST', { position: index }));
      host.appendChild(button);
    });
  }
  host.querySelectorAll('[data-filter]').forEach((button) => {
    const index = Number(button.dataset.filter);
    button.classList.toggle('active', index === wheel.target);
    button.classList.toggle('primary', wheel.moving && index === wheel.target);
  });
}

/* The Autofocus buttons are gated on the cameras actually being in use, not on
   the sequencer's thread being alive: a paused sequence has let go of them, and
   a stopped one goes on warming them down for minutes afterwards. */
function updateFocusButtons(status) {
  const rig = currentRig();
  const focuser = ((rig && rig.devices) || status.devices || {}).focuser || {};
  const sequence = status.sequence || {};
  const busy = !!(rig && rig.capture && rig.capture.busy);
  const focusing = !!(rig && rig.focus && rig.focus.running);
  const blocked = sequence.imaging || busy || focusing;

  const button = $('btnAutofocus');
  if (button) {
    button.disabled = !focuser.connected || blocked;
    button.classList.toggle('active', focusing);
    button.textContent = focusing ? 'Focusing…' : 'Autofocus';
    button.title = !focuser.connected ? 'No focuser connected'
      : sequence.imaging ? 'The sequencer is imaging — pause it first'
        : busy ? 'An exposure is still finishing' : 'Run a focus sweep now';
  }
  const all = $('btnAutofocusAll');
  if (all) {
    all.hidden = rigList().length < 2 || focusing;
    all.disabled = blocked;
  }
  // While a sweep is running, Abort is the only thing worth offering.
  const abort = $('btnAutofocusAbort');
  if (abort) abort.hidden = !isFocusRunning();
  const inDialog = $('btnFocusNow');
  if (inDialog) inDialog.disabled = blocked;
}

function updateFocuser(focuser) {
  if (!focuser.connected) {
    ['focPos', 'focPosMini', 'focTarget', 'focTemp']
      .forEach((id) => setText(id, '—'));
    return;
  }
  setText('focPos', focuser.position);
  // Also on the camera panel, so the focuser panel can stay shut.
  setText('focPosMini', focuser.position);
  setText('focTarget', focuser.moving ? focuser.target : '—');
  setText('focTemp', focuser.temperature === null || focuser.temperature === undefined
    ? '—' : `${focuser.temperature.toFixed(1)} °C`);
  const input = $('focTargetInput');
  if (focuser.maxStep) input.max = focuser.maxStep;
  if (document.activeElement !== input && input.value === '') input.value = focuser.position;
}

function updateRotator(rotator) {
  const connected = !!rotator.connected;
  ['btnRotMove', 'btnRotHalt', 'btnRotSync'].forEach((id) => { $(id).disabled = !connected; });
  document.querySelectorAll('[data-rotate-step]').forEach((button) => {
    button.disabled = !connected;
  });
  if (!connected) {
    ['rotPos', 'rotMech', 'rotState'].forEach((id) => setText(id, '—'));
    return;
  }
  setText('rotPos', `${rotator.position.toFixed(2)}°`);
  setText('rotMech', rotator.mechanical === null || rotator.mechanical === undefined
    ? '—' : `${rotator.mechanical.toFixed(2)}°`);
  setText('rotState', rotator.moving
    ? `moving → ${(rotator.target ?? 0).toFixed(1)}°` : 'idle');
  const stateNode = $('rotState');
  if (stateNode) stateNode.classList.toggle('alert', !!rotator.moving);

  const input = $('rotTargetInput');
  if (document.activeElement !== input && input.value === '') {
    input.value = rotator.position.toFixed(1);
  }
  // Syncing the rotator only means anything once a solve has measured the angle.
  $('btnRotSync').disabled = !state.solveResult;
}

function updateMount(mount) {
  if (!mount.connected) {
    ['mntRa', 'mntDec', 'mntAlt', 'mntAz', 'mntLst'].forEach((id) => setText(id, '—'));
    $('btnTracking').classList.remove('active');
    return;
  }
  setText('mntRa', fmtHours(mount.ra));
  setText('mntDec', fmtDegrees(mount.dec));
  setText('mntAlt', mount.altitude === null || mount.altitude === undefined
    ? '—' : `${mount.altitude.toFixed(1)}°`);
  setText('mntAz', mount.azimuth === null || mount.azimuth === undefined
    ? '—' : `${mount.azimuth.toFixed(1)}°`);
  setText('mntLst', fmtHours(mount.lst));

  const altNode = $('mntAlt');
  if (altNode) altNode.classList.toggle('alert', (mount.altitude ?? 90) < 20);

  $('btnTracking').classList.toggle('active', !!mount.tracking);
  $('btnTracking').textContent = mount.tracking ? 'Tracking on' : 'Tracking off';
  $('btnPark').textContent = mount.atPark ? 'Parked' : 'Park';
  $('btnPark').classList.toggle('active', !!mount.atPark);
}

function updateGuider(guider) {
  const ids = ['guiState', 'guiRms', 'guiRmsMini', 'guiRmsRa', 'guiRmsDec', 'guiSnr'];
  const connected = !!guider.connected;
  const task = guider.task || {};
  // Starting to guide takes minutes — PHD2 has to find a star, sometimes
  // calibrate, then settle. While it is working, Guide and Dither are disabled
  // and say so; Stop stays live, because a guide that will never find a star is
  // exactly when it is wanted.
  ['btnGuide', 'btnDither'].forEach((id) => {
    $(id).disabled = !connected || !!task.busy;
  });
  $('btnGuideStop').disabled = !connected;
  $('btnGuide').textContent = task.busy ? 'Working…' : 'Guide';
  $('btnGuide').title = task.busy ? task.what || 'the guider is busy' : '';
  if (!connected) {
    ids.forEach((id) => setText(id, '—'));
    return;
  }

  // Arcseconds when PHD2 has told us the pixel scale, pixels otherwise.
  const unit = guider.pixelScale ? '"' : ' px';
  const fmt = (value) => (value === null || value === undefined
    ? '—' : `${value.toFixed(2)}${unit}`);
  const rms = guider.pixelScale ? guider.rmsTotal : guider.rmsTotalPx;
  const ra = guider.pixelScale ? guider.rmsRa : guider.rmsRaPx;
  const dec = guider.pixelScale ? guider.rmsDec : guider.rmsDecPx;

  let label = guider.state || 'Unknown';
  if (guider.settling) {
    const distance = guider.settling.distance;
    label = distance === null || distance === undefined
      ? 'Settling' : `Settling ${distance.toFixed(2)}px`;
  }
  // What was asked for outranks what PHD2 last said it was doing: while a
  // `guide` is finding a star, PHD2 still reports "Stopped", and a state line
  // saying Stopped for two minutes after pressing Guide is what "it isn't
  // working" looks like.
  if (task.busy) {
    const waited = task.since ? Math.round(Date.now() / 1000 - task.since) : 0;
    label = `${label} — working${waited > 3 ? ` ${waited}s` : ''}`;
  } else if (task.error) {
    label = task.error;
  }
  setText('guiState', label);
  setText('guiRms', fmt(rms));
  setText('guiRmsMini', fmt(rms));
  setText('guiRmsRa', fmt(ra));
  setText('guiRmsDec', fmt(dec));
  setText('guiSnr', guider.snr === null || guider.snr === undefined
    ? '—' : guider.snr.toFixed(1));

  // Losing the star or drifting badly is worth seeing without reading the log.
  const stateNode = $('guiState');
  if (stateNode) {
    stateNode.classList.toggle('alert',
      guider.state === 'LostLock' || guider.state === 'Paused'
      || !!guider.starLost || !!task.error);
  }
  const rmsNode = $('guiRms');
  if (rmsNode) rmsNode.classList.toggle('alert', rms !== null && rms !== undefined && rms > 2);

  $('btnGuide').classList.toggle('active', !!guider.guiding);
}

/* ------------------------------------------------------------- observatory */

/* The building, not the telescope. Weather first and largest, because it is the
   one thing here that decides whether any of the rest should be happening. */
function updateObservatory(status) {
  const panel = $('panel-observatory');
  if (!panel) return;
  const safety = status.safety || {};
  const devices = (status.devices || {});
  const dome = devices.dome || {};
  const sw = devices.switch || {};

  const banner = $('safetyBanner');
  banner.className = 'safety-banner';
  if (!safety.connected) {
    banner.textContent = safety.enabled === false
      ? 'Weather watching is switched off' : 'No safety monitor connected';
    banner.classList.add('unknown');
  } else if (safety.safe === false) {
    const held = safety.unsafeFor;
    const grace = safety.graceSeconds || 0;
    // Before the grace period is up it has not acted yet, and saying so is the
    // difference between "it is about to shut down" and "it has".
    const pending = held !== null && held !== undefined && held < grace;
    banner.textContent = pending
      ? `UNSAFE — ${Math.max(0, Math.round(grace - held))}s before it acts`
      : `UNSAFE${safety.error ? ` — ${safety.error}` : ''}`;
    banner.classList.add(pending ? 'pending' : 'unsafe');
  } else if (safety.safe === true) {
    banner.textContent = 'Safe';
    banner.classList.add('safe');
  } else {
    banner.textContent = 'Waiting for the safety monitor';
    banner.classList.add('unknown');
  }

  const haveDome = !!dome.connected;
  $('domeRow').hidden = !haveDome;
  $('domeControls').hidden = !haveDome;
  if (haveDome) {
    setText('domeShutter', dome.shutterState || '—');
    setText('domeAz', dome.azimuth === null || dome.azimuth === undefined
      ? (dome.atPark ? 'parked' : '—') : `${dome.azimuth.toFixed(0)}°`);
    const shutterNode = $('domeShutter');
    if (shutterNode) shutterNode.classList.toggle('alert', dome.shutterState === 'error');
    // Opening is refused while unsafe by the server; disabling it here as well
    // means the button says so rather than the error doing it.
    $('btnRoofOpen').disabled = !dome.canShutter || safety.safe === false;
    $('btnRoofOpen').title = safety.safe === false
      ? 'The safety monitor says it is not safe to open' : '';
    $('btnRoofClose').disabled = !dome.canShutter;
    $('btnDomeSlave').hidden = !dome.canSlave;
    $('btnDomeSlave').classList.toggle('active', !!dome.slaved);
    $('btnDomeSlave').textContent = dome.slaved ? 'Slaved' : 'Slave';
  }

  const channels = sw.connected ? (sw.channels || []) : [];
  $('switchHeading').hidden = !channels.length;
  $('switchList').hidden = !channels.length;
  if (channels.length) renderSwitches(channels);

  $('observatoryNote').hidden = !!(safety.connected || haveDome || channels.length);
}

/* One row per outlet. Rebuilt only when the channel list itself changes, so a
   slider being dragged is not replaced under the cursor four times a second. */
function renderSwitches(channels) {
  const host = $('switchList');
  const signature = JSON.stringify(channels.map((c) => [c.index, c.name, c.boolean,
    c.min, c.max, c.writable]));
  if (host.dataset.signature !== signature) {
    host.dataset.signature = signature;
    host.innerHTML = '';
    for (const channel of channels) {
      const row = document.createElement('div');
      row.className = 'switch-row';
      row.dataset.index = String(channel.index);

      const label = document.createElement('span');
      label.className = 'switch-name';
      label.textContent = channel.name;
      label.title = channel.description || '';
      row.appendChild(label);

      if (channel.boolean) {
        const button = document.createElement('button');
        button.className = 'btn small';
        button.dataset.role = 'toggle';
        button.textContent = '—';
        button.disabled = !channel.writable;
        button.addEventListener('click', () => {
          const on = button.classList.contains('active');
          send('/api/switch', 'POST', { index: channel.index, value: on ? 0 : 1 });
        });
        row.appendChild(button);
      } else {
        const input = document.createElement('input');
        input.type = 'number';
        input.dataset.role = 'value';
        input.min = String(channel.min);
        input.max = String(channel.max);
        input.step = String(channel.step || 1);
        input.disabled = !channel.writable;
        input.addEventListener('change', () => {
          send('/api/switch', 'POST',
            { index: channel.index, value: Number(input.value) });
        });
        row.appendChild(input);
      }
      host.appendChild(row);
    }
  }

  for (const channel of channels) {
    const row = host.querySelector(`[data-index="${channel.index}"]`);
    if (!row) continue;
    const toggle = row.querySelector('[data-role="toggle"]');
    if (toggle) {
      const on = Number(channel.value) >= 0.5;
      toggle.textContent = channel.value === null ? '?' : on ? 'On' : 'Off';
      toggle.classList.toggle('active', on);
      toggle.classList.toggle('danger', on);
    }
    const value = row.querySelector('[data-role="value"]');
    // Not while it is being typed into.
    if (value && document.activeElement !== value && channel.value !== null) {
      value.value = String(channel.value);
    }
  }
}

/* ---------------------------------------- the camera watching the telescope */

/* The feed is a plain <img> re-pointed whenever the server says it has a new
   frame, rather than a stream. At about a frame a second that is simpler, it
   survives a dropped link without any reconnect logic, and it costs nothing
   while the panel is closed because nothing is fetched at all. */
function updatePierCam(piercam) {
  const panel = $('panel-piercam');
  if (!panel) return;
  const image = $('piercamImage');
  const empty = $('piercamEmpty');
  const showing = !panel.hidden;

  const tag = document.querySelector('[data-role="pier-name"]');
  if (tag) tag.textContent = piercam.name || '—';

  if (!piercam.connected) {
    image.hidden = true;
    image.removeAttribute('src');
    state.pierToken = null;
    empty.hidden = false;
    empty.textContent = 'No pier camera connected';
    ['piercamExposure', 'piercamGain', 'piercamLevel'].forEach((id) => setText(id, '—'));
    setText('piercamDetail', 'Connect one on the Equipment rows');
    return;
  }

  if (!piercam.enabled) {
    empty.hidden = false;
    empty.textContent = 'The live view is switched off';
  } else if (!piercam.hasFrame) {
    empty.hidden = false;
    empty.textContent = piercam.error || 'Waiting for the first frame…';
  }

  // Only fetch when the picture has actually changed, and only while the panel
  // is on screen — a closed panel should not be pulling a frame a second.
  if (showing && piercam.token && piercam.token !== state.pierToken) {
    state.pierToken = piercam.token;
    image.onload = () => { image.hidden = false; empty.hidden = true; };
    image.onerror = () => { state.pierToken = null; };
    image.src = `/api/piercam/frame.png?t=${encodeURIComponent(piercam.token)}`;
  }

  const exposure = piercam.exposure;
  setText('piercamExposure', exposure === null || exposure === undefined ? '—'
    : exposure >= 1 ? `${exposure.toFixed(2)}s`
      : `${Math.round(exposure * 1000)}ms`);
  setText('piercamGain', piercam.gain === null || piercam.gain === undefined
    ? '—' : String(piercam.gain));
  setText('piercamLevel', piercam.level === null || piercam.level === undefined
    ? '—' : `${Math.round(piercam.level * 100)}%`);

  // The level is the number that says whether auto-exposure is coping: pinned
  // at 100% is a blown-out frame it cannot pull back from.
  const levelNode = $('piercamLevel');
  if (levelNode && piercam.target) {
    const off = piercam.level === null || piercam.level === undefined
      ? 0 : Math.abs(piercam.level - piercam.target) / piercam.target;
    levelNode.classList.toggle('alert', off > 0.5);
  }

  const bits = [];
  if (piercam.width) bits.push(`${piercam.width}×${piercam.height}`);
  bits.push(piercam.auto ? 'auto' : 'manual');
  if (piercam.target) bits.push(`aiming ${Math.round(piercam.target * 100)}%`);
  if (piercam.age !== null && piercam.age !== undefined) bits.push(`${piercam.age.toFixed(0)}s ago`);
  if (piercam.error) bits.push(piercam.error);
  setText('piercamDetail', bits.join('  ·  '));
  const detail = $('piercamDetail');
  if (detail) detail.classList.toggle('warn-text', !!piercam.error);
}

/* ------------------------------------------------- what the rig is doing */

/* One line that answers "why is nothing happening?".
 *
 * It is worked out from the live device state rather than from the sequencer
 * alone, so a slew started by hand reads exactly the same as one inside a run —
 * and so it still says something on a night that is being driven by hand. The
 * order below is the order of precedence: recovery outranks everything, and a
 * moving mount outranks an exposure because you cannot be doing both.
 */

const ACTIVITIES = {
  recovering: { icon: '⟳', verb: 'Recovering', tone: 'alert' },
  parking: { icon: '⏏', verb: 'Parking', tone: 'busy' },
  homing: { icon: '⌂', verb: 'Homing', tone: 'busy' },
  slewing: { icon: '➜', verb: 'Slewing', tone: 'busy' },
  flipping: { icon: '⇄', verb: 'Meridian flip', tone: 'busy' },
  centring: { icon: '⊕', verb: 'Centring', tone: 'busy' },
  solving: { icon: '⊕', verb: 'Plate solving', tone: 'busy' },
  rotating: { icon: '↻', verb: 'Rotating', tone: 'busy' },
  focusing: { icon: '◎', verb: 'Focusing', tone: 'busy' },
  filter: { icon: '◐', verb: 'Changing filter', tone: 'busy' },
  dithering: { icon: '✳', verb: 'Dithering', tone: 'busy' },
  settling: { icon: '✳', verb: 'Settling the guider', tone: 'busy' },
  guiding: { icon: '✦', verb: 'Starting guiding', tone: 'busy' },
  cooling: { icon: '❄', verb: 'Cooling', tone: 'busy' },
  warming: { icon: '❄', verb: 'Warming up', tone: 'busy' },
  exposing: { icon: '◉', verb: 'Exposing', tone: 'live' },
  downloading: { icon: '↓', verb: 'Downloading', tone: 'live' },
  waiting: { icon: '◷', verb: 'Waiting', tone: 'idle' },
  paused: { icon: '❚❚', verb: 'Paused', tone: 'idle' },
};

/** The one thing worth saying the rig is doing, and the detail under it. */
function currentActivity(status) {
  const sequence = (status && status.sequence) || {};
  const rig = currentRig();
  const devices = (rig && rig.devices) || (status && status.devices) || {};
  const master = (status && status.devices) || {};
  const capture = (rig && rig.capture) || (status && status.capture) || {};
  const solver = (rig && rig.solver) || (status && status.solver) || {};
  const focus = sequence.focus || {};

  if (sequence.recovery) {
    return { kind: 'recovering', detail: sequence.recovery.reason || '' };
  }
  if (sequence.running && sequence.paused) {
    return { kind: 'paused', detail: 'finishing the current frame' };
  }
  // The sequencer names its own stage, and it knows things no device reports —
  // that it is waiting for a target to rise, or waiting out a meridian.
  const staged = {
    slewing: 'slewing', centring: 'centring', rotating: 'rotating',
    focusing: 'focusing', dithering: 'dithering', flipping: 'flipping',
    cooling: 'cooling', waiting: 'waiting', guiding: 'guiding',
    recovering: 'recovering', parking: 'parking', homing: 'homing',
  }[sequence.state];
  if (sequence.running && staged) {
    return { kind: staged, detail: sequence.message || '' };
  }
  // Warming outlives the run it belongs to, so it is reported on its own and
  // not gated on `running` the way every stage above is.
  if (sequence.warming) {
    return { kind: 'warming', detail: 'ramping the cooler off — the sequence has ended' };
  }

  const mount = master.mount || {};
  if (mount.connected && mount.slewing) {
    return { kind: 'slewing', detail: 'the mount is moving' };
  }
  if (solver.busy) {
    return { kind: 'solving', detail: solver.message || solver.state || '' };
  }
  if (focus.running) return { kind: 'focusing', detail: focus.message || '' };
  const rotator = devices.rotator || {};
  if (rotator.connected && rotator.moving) {
    return { kind: 'rotating', detail: 'the rotator is moving' };
  }
  const wheel = devices.filterwheel || {};
  if (wheel.connected && wheel.moving) {
    return { kind: 'filter', detail: '' };
  }
  const guider = master.guider || {};
  if (guider.connected && guider.settling) {
    return { kind: 'settling', detail: 'waiting for the guiding to settle' };
  }
  if (capture.state === 'exposing') {
    return {
      kind: 'exposing',
      detail: frameDetail(status, capture),
      // The exposure has its own countdown rather than an age.
      remaining: capture.remaining,
    };
  }
  if (capture.busy) return { kind: 'downloading', detail: frameDetail(status, capture) };
  if (sequence.running && sequence.state === 'imaging') {
    return { kind: 'exposing', detail: sequence.message || '' };
  }
  return null;
}

/** The frame in flight, in a line: "300s light [H] · M31 - Panel 3 · frame 4/12". */
function frameDetail(status, capture) {
  const sequence = (status && status.sequence) || {};
  const calibration = (status && status.calibration) || {};
  const bits = [];
  const seconds = capture.exposureSeconds || 0;
  const type = capture.frameType || (capture.request || {}).frameType || '';
  let head = seconds ? `${seconds}s` : '';
  if (type) head += ` ${type}`;
  if (capture.filter) head += ` [${capture.filter}]`;
  if (head.trim()) bits.push(head.trim());
  if (calibration.running) {
    // A calibration run: which set, and how far through it.
    if (calibration.setName) bits.push(calibration.setName);
    if (calibration.frames) bits.push(`frame ${calibration.frame}/${calibration.frames}`);
    if (calibration.sets > 1) bits.push(`set ${calibration.set}/${calibration.sets}`);
  } else {
    if (capture.object) bits.push(capture.object);
    if (sequence.running && sequence.frameCount) {
      bits.push(`frame ${sequence.frame}/${sequence.frameCount}`);
    }
    if (sequence.running && sequence.panels > 1) {
      bits.push(`panel ${sequence.panel}/${sequence.panels}`);
    }
  }
  if (capture.telescope && (status.telescopes || []).length > 1) bits.push(capture.telescope);
  return bits.join('  ·  ');
}

function updateActivity(status) {
  const bar = $('activityBar');
  if (!bar) return;
  const now = currentActivity(status);
  if (!now) {
    bar.hidden = true;
    state.activity = { kind: '', since: 0 };
    return;
  }
  // The clock runs from when this stage began, not from when the page noticed.
  if (state.activity.kind !== now.kind) {
    const sequence = (status && status.sequence) || {};
    const since = (sequence.running && sequence.stateSince)
      ? sequence.stateSince * 1000 : Date.now();
    state.activity = { kind: now.kind, since };
  }
  const shape = ACTIVITIES[now.kind] || { icon: '●', verb: now.kind, tone: 'busy' };
  bar.hidden = false;
  bar.className = `activity ${shape.tone}`;
  setText('activityIcon', shape.icon);
  setText('activityVerb', shape.verb);
  setText('activityDetail', now.detail || '');
  setText('activityElapsed', now.remaining !== null && now.remaining !== undefined
    ? `−${clockSpan(now.remaining)}`
    : clockSpan((Date.now() - state.activity.since) / 1000));
}

/* The warnings strip: what is quietly wrong, most serious first. The head
   shows the worst unacknowledged entry and a count; the list underneath has
   every entry with what to do about it and a button to say it has been seen. */
const warningsView = { open: false, lastKey: '' };

function updateWarnings(status) {
  const bar = $('warningsBar');
  if (!bar) return;
  const board = (status && status.warnings) || {};
  const active = board.active || [];
  const shown = active.filter((w) => !w.acknowledged);
  if (!active.length) {
    bar.hidden = true;
    warningsView.open = false;
    return;
  }
  bar.hidden = false;
  const worst = shown[0] || active[0];
  bar.className = `warnings-bar ${worst.level}`;
  setText('warningsMark', worst.level === 'critical' ? '!' : (worst.level === 'warning' ? '△' : 'i'));
  setText('warningsTitle', worst.title);
  setText('warningsDetail', worst.detail || '');
  const counts = board.counts || {};
  const bits = [];
  if (counts.critical) bits.push(`${counts.critical} critical`);
  if (counts.warning) bits.push(`${counts.warning} warning${counts.warning === 1 ? '' : 's'}`);
  if (counts.notice) bits.push(`${counts.notice} notice${counts.notice === 1 ? '' : 's'}`);
  setText('warningsCount', bits.join(' · ') + (warningsView.open ? '  ▴' : '  ▾'));

  // The list is rebuilt only when its contents change, so a button under
  // the pointer is not replaced between mousedown and click.
  const key = JSON.stringify(active.map((w) => [w.key, w.level, w.acknowledged, w.detail]));
  const list = $('warningsList');
  list.hidden = !warningsView.open;
  if (key === warningsView.lastKey) return;
  warningsView.lastKey = key;
  list.innerHTML = '';
  for (const w of active) {
    const row = document.createElement('div');
    row.className = `warning-row ${w.level}${w.acknowledged ? ' seen' : ''}`;
    const head = document.createElement('div');
    head.className = 'warning-row-head';
    const title = document.createElement('b');
    title.textContent = w.title;
    head.appendChild(title);
    const age = document.createElement('span');
    age.className = 'mono small muted';
    age.textContent = `for ${clockSpan(w.ageSeconds || 0)}`;
    head.appendChild(age);
    const spacer = document.createElement('span');
    spacer.className = 'spacer';
    head.appendChild(spacer);
    if (!w.acknowledged) {
      const seen = document.createElement('button');
      seen.className = 'btn small ghost';
      seen.textContent = 'Seen it';
      seen.title = 'Keep it on the list, off the strip, until it changes';
      seen.addEventListener('click', () => {
        api(`/api/warnings/${encodeURIComponent(w.key)}/acknowledge`, 'POST')
          .catch((error) => toast(error.message, 'error'));
      });
      head.appendChild(seen);
    }
    row.appendChild(head);
    if (w.detail) {
      const detail = document.createElement('div');
      detail.className = 'small';
      detail.textContent = w.detail;
      row.appendChild(detail);
    }
    if (w.fix) {
      const fix = document.createElement('div');
      fix.className = 'small warning-fix';
      fix.textContent = w.fix;
      row.appendChild(fix);
    }
    list.appendChild(row);
  }
}

/** Something went wrong and is being put right. Loud, and briefly. */
function updateRecoveryBar(status) {
  const bar = $('recoveryBar');
  if (!bar) return;
  const sequence = (status && status.sequence) || {};
  const recovery = sequence.recovery;
  if (!recovery) { bar.hidden = true; return; }
  bar.hidden = false;
  const kinds = {
    guiding: 'Guide star', pointing: 'Pointing', camera: 'Camera',
  };
  setText('recoveryKind', kinds[recovery.kind] || 'Recovering');
  setText('recoveryReason', recovery.reason || '');
  const total = sequence.rescues || 0;
  setText('recoveryCount', total ? `${total} rescue${total === 1 ? '' : 's'} this run` : '');
}

function updateLog(events) {
  const host = $('log');
  host.innerHTML = '';
  for (const event of events) {
    const line = document.createElement('div');
    line.className = `log-line ${event.level}`;
    const time = new Date(event.time * 1000).toLocaleTimeString([], { hour12: false });
    line.innerHTML = `<time>${time}</time><span></span>`;
    line.querySelector('span').textContent = event.message;
    host.appendChild(line);
  }
}

function applyStatus(status) {
  state.status = status;
  // The panels describe the selected telescope. The mount and the guider come
  // from the master whatever is selected, because there is only one of each.
  const rig = currentRig();
  const devices = (rig && rig.devices) || status.devices;
  const capture = (rig && rig.capture) || status.capture;
  const master = status.devices || {};

  // The telescope list can change from another tab or a loaded profile, so the
  // switcher is kept honest against what the server says is there.
  if (state.equipment && rigList().length
      && rigList().length !== (state.equipment.telescopes || []).length) {
    loadEquipment();
  }

  updatePills(devices);
  updateConnectButtons(devices);
  updateCamera(devices.camera || {}, capture);
  updateFilterWheel(devices.filterwheel || {});
  updateFocuser(devices.focuser || {});
  updateMount(master.mount || {});
  updateGuider(master.guider || {});
  updatePierCam(status.piercam || {});
  updateObservatory(status);
  updateOffsetRun(status.filterOffsets || {});
  updateSolver((rig && rig.solver) || status.solver || {});
  updateRotator(devices.rotator || {});
  updateOutput(capture);
  updateSequenceBanner(status);
  updateActivity(status);
  updateRecoveryBar(status);
  updateWarnings(status);
  updateRunPanel(status);
  updateFocusFloat();
  updateFocusButtons(status);
  updateLog(status.events || []);

  updateRigViews();

  // Every telescope's new frame belongs in the session list, but only the
  // selected one's is pulled into the main view — otherwise a slave finishing
  // its exposure would keep snatching the window away from what you are on.
  const seen = state.rigLatest || (state.rigLatest = {});
  let anyNew = false;
  for (const one of rigList()) {
    const id = ((one.capture || {}).latestImageId) || '';
    if (id && seen[one.id] !== id) {
      seen[one.id] = id;
      anyNew = true;
    }
  }
  const latest = capture.latestImageId;
  const mine = !!latest && latest !== state.lastLatestId;
  if (mine) state.lastLatestId = latest;
  if (mine || anyNew) {
    refreshImages().then(() => { if (mine) selectImage(latest, true); });
  }

  // The tab is part of the payload, not an extra that only `showTab` supplies.
  // Every tab-scoped listener opens `if (tab !== 'mine') return`, so omitting it
  // here meant they fired on a tab *switch* and never again — the planetarium
  // stopped following the clock, and the planner kept the camera field it was
  // first given however many times the rig was solved since. The throttles
  // those listeners already carry are there because per-tick delivery is what
  // they were written for.
  for (const listener of statusListeners) {
    try { listener(status, state.tab); } catch (error) { console.error(error); }
  }
}

/* ------------------------------------------------------- telescope views */

/** A thumbnail per telescope beneath the main view.
 *
 * On a multi-scope rig the interesting question during a run is "is all of it
 * still working", which the one selected telescope's frame cannot answer. Each
 * card follows its own telescope's latest frame; clicking one selects that
 * telescope and opens its frame full size.
 */
function updateRigViews() {
  const host = $('rigViews');
  if (!host) return;
  const all = rigList();
  host.hidden = all.length < 2;
  if (host.hidden) {
    host.innerHTML = '';
    host.dataset.rigs = '';
    return;
  }

  // Rebuilt only when the telescopes themselves change. Every other poll
  // updates the cards in place, or the thumbnails would flicker once a second.
  const ids = all.map((rig) => rig.id).join('|');
  if (host.dataset.rigs !== ids) {
    host.dataset.rigs = ids;
    host.innerHTML = '';
    for (const rig of all) {
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'rig-view';
      card.dataset.rig = rig.id;
      card.innerHTML = '<span class="rig-view-shot">'
        + '<img alt="" hidden><span class="rig-view-empty">no frame yet</span>'
        + '</span><span class="rig-view-name"></span>'
        + '<span class="rig-view-meta mono"></span>';
      card.addEventListener('click', () => {
        selectRig(rig.id);
        const now = rigList().find((one) => one.id === rig.id) || {};
        const shot = (now.capture || {}).latestImageId;
        if (shot) selectImage(shot);
      });
      host.appendChild(card);
    }
  }

  const chosen = (currentRig() || {}).id;
  for (const rig of all) {
    const card = host.querySelector(`.rig-view[data-rig="${rig.id}"]`);
    if (!card) continue;
    const shot = rig.capture || {};
    const camera = (rig.devices || {}).camera || {};
    card.classList.toggle('active', rig.id === chosen);
    card.querySelector('.rig-view-name').textContent = rig.name;

    const img = card.querySelector('img');
    const latest = shot.latestImageId || '';
    if (img.dataset.imageId !== latest) {
      img.dataset.imageId = latest;
      if (latest) img.src = `/api/images/${latest}/render.png?maxDim=320`;
      else img.removeAttribute('src');
      img.hidden = !latest;
    }
    card.querySelector('.rig-view-empty').hidden = !!latest;

    const bits = [];
    if (!camera.connected) bits.push('camera off');
    else if (shot.busy && shot.remaining !== null && shot.remaining !== undefined) {
      bits.push(`${shot.state || 'exposing'} ${Math.round(shot.remaining)}s`);
    } else bits.push(shot.state || 'idle');
    if (shot.framesTaken) bits.push(`${shot.framesTaken} frames`);
    card.querySelector('.rig-view-meta').textContent = bits.join(' · ');
    card.title = shot.error ? shot.error : `Show ${rig.name} full size`;
    card.classList.toggle('faulted', !!shot.error);
  }
}

/* ------------------------------------------------------------ plate solve */

function updateSolver(solver) {
  const tag = $('solverTag');
  tag.textContent = solver.available ? 'ASTAP' : 'not found';
  tag.className = `tag ${solver.available ? 'ready' : 'missing'}`;
  tag.title = solver.executable || 'Install ASTAP, or set its path in Site & Optics.';

  const mount = (state.status && state.status.devices.mount) || {};
  const canRun = !!solver.available && !solver.busy;
  $('btnSolve').disabled = !canRun;
  $('btnSolveSync').disabled = !canRun || !mount.connected;
  $('btnSolveCenter').disabled = !canRun || !mount.connected;
  $('btnSolveAbort').disabled = !solver.busy;

  const node = $('solveState');
  let label = solver.message || solver.state || 'idle';
  if (solver.busy && solver.attempts > 1) label += `  (${solver.attempt}/${solver.attempts})`;
  node.className = 'small mono';
  if (solver.error) {
    label = solver.error;
    node.classList.add('failed');
  } else if (solver.busy) {
    node.classList.add('working');
  } else if (solver.result) {
    node.classList.add('solved');
    label = solver.result.separation === null || solver.result.separation === undefined
      ? `solved in ${solver.result.seconds}s`
      : `solved — ${solver.result.separation.toFixed(2)}' from target`;
  }
  node.textContent = label;

  const result = solver.result;
  state.solveResult = result || null;
  $('btnSolveTarget').disabled = !result;
  if (!result) {
    ['solveRa', 'solveDec', 'solveScale', 'solveRotation', 'solveField']
      .forEach((id) => setText(id, '—'));
    return;
  }
  setText('solveRa', fmtHours(result.ra));
  setText('solveDec', fmtDegrees(result.dec));
  setText('solveScale', `${result.scale.toFixed(2)}"/px`);
  setText('solveRotation', `${result.rotation.toFixed(2)}°${result.flipped ? ' ⇄' : ''}`);
  setText('solveField', `${(result.fovWidth * 60).toFixed(1)}′ × ${(result.fovHeight * 60).toFixed(1)}′`);
}

/* ------------------------------------------------------- the run panel */

/* Two things worth watching while a sequence runs: whether the stars are
   growing (falling out of focus) and whether the telescope is still on target.
   Both are only useful during the night, not the morning after. */

function drawChart(canvas, series, options) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 280;
  const height = canvas.clientHeight || 92;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = '#0b0e15';
  ctx.fillRect(0, 0, width, height);

  const points = series.filter((p) => p.y !== null && p.y !== undefined);
  if (points.length < 1) {
    ctx.fillStyle = '#5c6577';
    ctx.font = '10px ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.fillText(options.empty || 'no data yet', width / 2, height / 2 + 3);
    return;
  }

  // Everything scales with the canvas: the same chart is drawn small in the
  // Run panel and large in the floating window, and 9px labels on a 400-pixel
  // chart are unreadable.
  const big = height > 150;
  const fontSize = big ? 12 : 9;
  const pad = big
    ? { left: 52, right: 14, top: 14, bottom: 30 }
    : { left: 30, right: 6, top: 8, bottom: 14 };
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  let minY = Math.min(...ys);
  let maxY = Math.max(...ys);
  if (maxY - minY < 1e-6) { minY -= 0.5; maxY += 0.5; }
  const padY = (maxY - minY) * 0.15;
  minY -= padY; maxY += padY;
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);

  const px = (x) => (maxX === minX ? pad.left + (width - pad.left - pad.right) / 2
    : pad.left + (x - minX) / (maxX - minX) * (width - pad.left - pad.right));
  const py = (y) => pad.top + (maxY - y) / (maxY - minY) * (height - pad.top - pad.bottom);

  ctx.strokeStyle = '#232936';
  ctx.fillStyle = '#5c6577';
  ctx.font = `${fontSize}px ui-monospace, monospace`;
  ctx.textAlign = 'right';
  ctx.lineWidth = 1;
  // A few gridlines on a big chart, just the two ends on a small one.
  const steps = big ? 5 : 1;
  for (let i = 0; i <= steps; i += 1) {
    const value = (minY + padY) + ((maxY - padY) - (minY + padY)) * (i / steps);
    const y = Math.round(py(value)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillText(value.toFixed(options.decimals ?? 2), pad.left - 5,
      y + fontSize / 3);
  }

  // The x axis matters on a focus curve: it is the focuser position.
  if (big) {
    ctx.textAlign = 'center';
    const ticks = 4;
    for (let i = 0; i <= ticks; i += 1) {
      const value = minX + (maxX - minX) * (i / ticks);
      const x = px(value);
      ctx.strokeStyle = '#1b2029';
      ctx.beginPath();
      ctx.moveTo(x, pad.top); ctx.lineTo(x, height - pad.bottom);
      ctx.stroke();
      ctx.fillStyle = '#5c6577';
      ctx.fillText(Math.round(value).toString(), x, height - pad.bottom + fontSize + 4);
    }
    if (options.xLabel) {
      ctx.fillStyle = '#4a5263';
      ctx.fillText(options.xLabel, (pad.left + width - pad.right) / 2, height - 4);
    }
  }

  if (options.reference !== null && options.reference !== undefined
      && options.reference >= minY && options.reference <= maxY) {
    ctx.strokeStyle = 'rgba(75, 184, 122, 0.7)';
    ctx.setLineDash([4, 3]);
    const y = Math.round(py(options.reference)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  if (options.fitted) {
    ctx.strokeStyle = 'rgba(120, 175, 255, 0.45)';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    for (let i = 0; i <= 60; i += 1) {
      const x = minX + (maxX - minX) * (i / 60);
      const y = options.fitted(x);
      if (y < minY || y > maxY) continue;
      const sx = px(x); const sy = py(y);
      if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
    }
    ctx.stroke();
  }

  // The two straight arms the trend-line fit found, drawn as they are fitted.
  if (options.trends) {
    ctx.setLineDash([5, 4]);
    ctx.lineWidth = big ? 1.6 : 1.1;
    for (const arm of options.trends) {
      if (!arm) continue;
      ctx.strokeStyle = 'rgba(232, 176, 84, 0.75)';
      ctx.beginPath();
      let started = false;
      for (let i = 0; i <= 30; i += 1) {
        const x = minX + (maxX - minX) * (i / 30);
        const y = arm.slope * x + arm.intercept;
        if (y < minY || y > maxY) { started = false; continue; }
        if (!started) { ctx.moveTo(px(x), py(y)); started = true; }
        else ctx.lineTo(px(x), py(y));
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }

  ctx.strokeStyle = options.colour || '#5b8dd9';
  ctx.lineWidth = big ? 2.2 : 1.6;
  ctx.beginPath();
  points.forEach((point, index) => {
    const sx = px(point.x); const sy = py(point.y);
    if (index === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
  });
  ctx.stroke();

  if (options.dots) {
    ctx.fillStyle = options.colour || '#5b8dd9';
    for (const point of points) {
      ctx.beginPath();
      ctx.arc(px(point.x), py(point.y), big ? 4 : 2.4, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  if (options.marker !== null && options.marker !== undefined
      && options.marker >= minX && options.marker <= maxX) {
    ctx.strokeStyle = '#7ee7a5';
    ctx.lineWidth = 1.4;
    const x = Math.round(px(options.marker)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(x, pad.top); ctx.lineTo(x, height - pad.bottom);
    ctx.stroke();
  }
}

function updateRunPanel(status) {
  if (!$('panel-run') || $('panel-run').hidden) return;
  const sequence = (status && status.sequence) || {};
  // The selected telescope's own sweep, not the master's. On a multi-scope rig
  // they all focus at once and each has its own curve; showing the master's
  // under whichever telescope is selected is simply the wrong one.
  const rig = currentRig();
  const focus = (rig && rig.focus) || sequence.focus || {};
  const run = focus.current || focus.last;

  const tag = $('focusTag');
  tag.textContent = focus.running ? 'running' : (run ? 'last run' : '—');
  tag.className = `tag${focus.running ? ' ready' : ''}`;

  drawChart($('focusCurve'),
    (run ? run.points : []).map((p) => ({ x: p.position, y: p.hfd })),
    { dots: true, marker: run ? run.bestPosition : null, decimals: 2,
      empty: 'no focus run yet' });

  if (!run) {
    setText('focusDetail', 'no focus run yet');
  } else if (focus.running) {
    setText('focusDetail', focus.message || 'sweeping…');
  } else if (run.bestPosition !== null && run.bestPosition !== undefined) {
    setText('focusDetail',
      `focused at ${run.bestPosition}`
      + (run.bestHfd ? `, HFD ${run.bestHfd.toFixed(2)}` : '')
      + (run.temperature !== null && run.temperature !== undefined
        ? `, ${run.temperature.toFixed(1)} °C` : '')
      + (run.filter ? `, ${run.filter}` : ''));
  } else {
    setText('focusDetail', run.detail || 'the sweep did not find a minimum');
  }

  // Likewise the star-size trace: three telescopes' subs interleaved into one
  // line is not a trend, it is three trends plotted on top of each other.
  const all = sequence.frames || [];
  const frames = rig ? all.filter((f) => !f.rig || f.rig === rig.id) : all;
  const reference = rig
    ? (sequence.referenceHfrByRig || {})[rig.id]
    : sequence.referenceHfr;
  setText('frameCount', String(frames.length));
  drawChart($('hfrChart'),
    frames.map((f, index) => ({ x: index, y: f.hfr })),
    { colour: '#4bb87a', reference, decimals: 2,
      empty: 'no subs measured yet' });

  const measured = frames.filter((f) => f.hfr !== null && f.hfr !== undefined);
  const latest = measured[measured.length - 1];
  setText('hfrNow', latest ? `${latest.hfr.toFixed(2)} px` : '—');

  const drift = latest && latest.driftPercent !== undefined ? latest.driftPercent : null;
  setText('hfrDrift', drift === null ? '—' : `${drift > 0 ? '+' : ''}${drift.toFixed(0)} %`);
  const driftNode = $('hfrDrift');
  if (driftNode) driftNode.classList.toggle('alert', drift !== null && drift > 25);

  const solved = frames.filter((f) => f.errorArcmin !== null
    && f.errorArcmin !== undefined);
  const lastSolve = solved[solved.length - 1];
  setText('pointingError', lastSolve ? `${lastSolve.errorArcmin.toFixed(1)}'` : '—');
  const pointNode = $('pointingError');
  if (pointNode) pointNode.classList.toggle('alert', !!lastSolve && lastSolve.errorArcmin > 5);

  setText('runDetail', frames.length
    ? `${measured.length} measured · ${solved.length} solved`
      + (latest ? ` · ${latest.stars} stars · ${latest.filter}` : '')
    : 'nothing captured yet');
}

/* ------------------------------------------------- the floating focus window */

/* A focus sweep takes a minute or two and is worth watching while it happens.
   The Run panel has the same curve, but it lives in the right-hand column on
   another tab; this rides over the image where you already are, and clears
   itself once the answer has been on screen long enough to read. */

const focusFloat = {
  shown: false,
  pinned: false,
  wasRunning: false,
  hideTimer: null,
  moved: false,
  // Which telescope's curve to show, once one has been picked by hand. Cleared
  // when a fresh round of sweeps begins, so the next one starts by following
  // the run rather than sticking to last time's choice.
  rig: null,
};

const FOCUS_LINGER_MS = 6000;

function showFocusFloat() {
  const node = $('focusFloat');
  clearTimeout(focusFloat.hideTimer);
  focusFloat.hideTimer = null;
  node.classList.remove('leaving');
  node.hidden = false;
  focusFloat.shown = true;
}

function hideFocusFloat(immediately = true) {
  const node = $('focusFloat');
  clearTimeout(focusFloat.hideTimer);
  focusFloat.hideTimer = null;
  focusFloat.shown = false;
  node.classList.remove('leaving');
  node.hidden = true;
  if (immediately) focusFloat.pinned = false;
}

/** Fade out, unless it was pinned or the sweep started again meanwhile. */
function retireFocusFloat() {
  if (focusFloat.pinned) return;
  clearTimeout(focusFloat.hideTimer);
  focusFloat.hideTimer = setTimeout(() => {
    const node = $('focusFloat');
    node.classList.add('leaving');
    setTimeout(() => {
      // A new run may have started during the fade; do not steal it.
      if (!focusFloat.pinned && !isFocusRunning()) hideFocusFloat(false);
      node.classList.remove('leaving');
    }, 620);
  }, FOCUS_LINGER_MS);
}

function isFocusRunning() {
  return rigList().some((rig) => rig.focus && rig.focus.running);
}

/** Every telescope with a sweep to show, running or just finished. */
function focusScopes() {
  return rigList().filter((rig) => rig.focus
    && (rig.focus.running || rig.focus.current || rig.focus.last));
}

/* The telescope whose curve is on screen.
 *
 * Three telescopes sweep at once and finish at different times, so "whichever
 * is running" used to mean the curve silently changed telescope underneath you
 * as each one landed. A pick made by hand is kept; otherwise it follows the
 * selected telescope while that one is still sweeping, and only then falls back
 * to whoever is. */
function focusSubject() {
  const shown = focusScopes();
  if (!shown.length) return currentRig();
  const chosen = shown.find((rig) => rig.id === focusFloat.rig);
  if (chosen) return chosen;
  const selected = shown.find((rig) => rig.id === (currentRig() || {}).id);
  if (selected && selected.focus.running) return selected;
  return shown.find((rig) => rig.focus.running) || selected || shown[0];
}

/** How one telescope's sweep is going, in two or three words. */
function focusTabState(rig) {
  const focus = rig.focus || {};
  const run = focus.current || focus.last;
  if (focus.running) {
    const points = (run && run.points) ? run.points.length : 0;
    return points ? `${points} pts` : 'starting';
  }
  if (!run) return '—';
  if (run.bestPosition !== null && run.bestPosition !== undefined) {
    return `@${run.bestPosition}`;
  }
  return 'no minimum';
}

/** A tab per telescope, so all three are visible while one is on the chart. */
function renderFocusTabs(subject) {
  const host = $('focusFloatTabs');
  if (!host) return;
  const shown = focusScopes();
  host.hidden = shown.length < 2;
  // With one telescope the tag in the header already says which, and a single
  // tab is just clutter.
  $('focusFloatScope').hidden = rigList().length < 2 || !host.hidden;
  if (host.hidden) {
    host.innerHTML = '';
    return;
  }
  const ids = shown.map((rig) => rig.id).join('|');
  if (host.dataset.rigs !== ids) {
    host.dataset.rigs = ids;
    host.innerHTML = '';
    for (const rig of shown) {
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'focus-tab';
      tab.dataset.rig = rig.id;
      tab.innerHTML = '<span class="focus-tab-name"></span>'
        + '<span class="focus-tab-state mono"></span>';
      tab.addEventListener('click', () => {
        focusFloat.rig = rig.id;
        updateFocusFloat();
      });
      host.appendChild(tab);
    }
  }
  for (const rig of shown) {
    const tab = host.querySelector(`.focus-tab[data-rig="${rig.id}"]`);
    if (!tab) continue;
    const focus = rig.focus || {};
    const run = focus.current || focus.last;
    const failed = !focus.running && run
      && (run.bestPosition === null || run.bestPosition === undefined);
    tab.classList.toggle('active', rig.id === (subject || {}).id);
    tab.classList.toggle('running', !!focus.running);
    tab.classList.toggle('failed', !!failed);
    tab.querySelector('.focus-tab-name').textContent = rig.name;
    tab.querySelector('.focus-tab-state').textContent = focusTabState(rig);
    tab.title = failed ? (run.detail || 'the sweep found no minimum')
      : (focus.running ? (focus.message || 'sweeping') : 'focused');
  }
}

function updateFocusFloat() {
  const running = isFocusRunning();

  // Opens itself when a sweep starts, however it was started — by hand, from
  // the Focuser panel, or by the sequencer deciding it was due.
  if (running && !focusFloat.wasRunning) {
    focusFloat.rig = null;              // a new round: follow it, do not stick
    showFocusFloat();
  }
  if (!running && focusFloat.wasRunning && focusFloat.shown) retireFocusFloat();
  focusFloat.wasRunning = running;
  if (!focusFloat.shown || $('focusFloat').hidden) return;

  const rig = focusSubject();
  const focus = (rig && rig.focus) || {};
  const run = focus.current || focus.last;

  $('focusFloatScope').textContent = rig ? rig.name : '—';
  renderFocusTabs(rig);
  $('focusFloatPin').classList.toggle('active', focusFloat.pinned);
  // Abort stops every sweep, not only the one on the chart, so it stays up
  // while any telescope is still going.
  $('focusFloatAbort').hidden = !running;

  const fits = (run && run.fits) || {};
  const trend = fits.trendlines;
  // The hyperbola the fit settled on, drawn through the points so a bad fit is
  // obvious rather than something you infer from the final number.
  const hyperbolic = fits.hyperbolic;
  const curve = hyperbolic && hyperbolic.width
    ? (x) => hyperbolic.value * Math.sqrt(
      1 + ((x - hyperbolic.position) / hyperbolic.width) ** 2)
    : null;

  drawChart($('focusFloatCurve'),
    (run ? run.points : []).map((p) => ({ x: p.position, y: p.hfd })),
    { dots: true,
      marker: run ? (run.bestPosition ?? (trend && trend.position)) : null,
      decimals: 2,
      xLabel: 'focuser position',
      fitted: curve,
      trends: trend ? [trend.left, trend.right] : null,
      empty: 'no focus run yet' });

  const best = run && run.bestPosition;
  setText('focusFloatPos', best === null || best === undefined ? '—' : String(best));
  setText('focusFloatHfd', run && run.bestHfd ? run.bestHfd.toFixed(2) : '—');
  setText('focusFloatTemp', run && run.temperature !== null && run.temperature !== undefined
    ? `${run.temperature.toFixed(1)} °C` : '—');

  const fitted = Object.entries(fits)
    .filter(([, fit]) => fit)
    .map(([name, fit]) => `${name} ${fit.position.toFixed(0)}`
      + (fit.rSquared ? ` (R² ${fit.rSquared.toFixed(3)})` : ''))
    .join('   ·   ');

  if (!run) setText('focusFloatDetail', 'no focus run yet');
  else if (focus.running) {
    setText('focusFloatDetail',
      `${focus.message || 'sweeping…'}   ${run.points.length} points`
      + (run.attempts > 1 ? `   attempt ${run.attempt}/${run.attempts}` : ''));
  } else if (best !== null && best !== undefined) {
    setText('focusFloatDetail', `focused at ${best}`
      + (run.startHfd ? `, HFD ${run.startHfd.toFixed(2)} → ${run.bestHfd.toFixed(2)}` : '')
      + (run.filter ? `, ${run.filter}` : '')
      + (fitted ? `\n${fitted}` : ''));
  } else {
    setText('focusFloatDetail',
      (run.detail || 'the sweep did not find a minimum') + (fitted ? `\n${fitted}` : ''));
  }
}

/** Drag by the title bar. Once moved, it stays where it was put. */
/** Start a sweep and put the floating window up straight away. */
async function startFocusRun(path) {
  let result;
  try {
    result = await api(path, 'POST');
  } catch (error) {
    toast(error.message, 'error');
    return;
  }
  // A telescope left out of "Focus all" is the thing you most need told: two of
  // three starting otherwise looks like the focusing is broken, when really a
  // focuser or a camera was never connected.
  const skipped = (result && result.skipped) || [];
  if (skipped.length) {
    toast(skipped.map((entry) => entry.reason).join('; '), 'error', 9000);
  }
  // Up immediately rather than on the next status tick, so the button press
  // visibly does something even before the first point is measured.
  focusFloat.rig = null;
  showFocusFloat();
  focusFloat.wasRunning = true;
  updateFocusFloat();
}

function bindFocusFloatDrag() {
  const node = $('focusFloat');
  const head = $('focusFloatHead');
  let offsetX = 0;
  let offsetY = 0;

  const move = (event) => {
    const x = Math.min(window.innerWidth - 60, Math.max(0, event.clientX - offsetX));
    const y = Math.min(window.innerHeight - 40, Math.max(0, event.clientY - offsetY));
    node.style.left = `${x}px`;
    node.style.top = `${y}px`;
    node.style.right = 'auto';
  };
  const stop = () => {
    node.classList.remove('dragging');
    window.removeEventListener('mousemove', move);
    window.removeEventListener('mouseup', stop);
  };

  head.addEventListener('mousedown', (event) => {
    if (event.target.closest('button')) return;
    const box = node.getBoundingClientRect();
    offsetX = event.clientX - box.left;
    offsetY = event.clientY - box.top;
    focusFloat.moved = true;
    node.classList.add('dragging');
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', stop);
    event.preventDefault();
  });

  // Abort every sweep that is running, not just the selected telescope's: they
  // are started together, and stopping half of them is never what is wanted.
  $('focusFloatAbort').addEventListener('click', () =>
    send('/api/focus/abort?all=true', 'POST', null, 'Stopping the focus run'));
  $('focusFloatClose').addEventListener('click', () => hideFocusFloat(true));
  $('focusFloatPin').addEventListener('click', () => {
    focusFloat.pinned = !focusFloat.pinned;
    $('focusFloatPin').classList.toggle('active', focusFloat.pinned);
    // Unpinning something that has already finished should let it go.
    if (!focusFloat.pinned && !isFocusRunning()) retireFocusFloat();
    else clearTimeout(focusFloat.hideTimer);
  });
}

/* ------------------------------------------------------------------- tabs */

function showTab(name) {
  state.tab = name;
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.tab === name);
  });
  document.querySelectorAll('.tab-panel').forEach((panel) => {
    panel.hidden = panel.dataset.panel !== name;
  });
  applyPanelVisibility();
  // Coming back to the Image tab is where a frame captured while it was hidden
  // finally gets the fit it was owed.
  if (name === 'image') repaintViewer();
  for (const listener of statusListeners) {
    try { listener(state.status, name); } catch (error) { console.error(error); }
  }
}

/* Tabs that are in the build but not in the release.
 *
 * The two survey tabs are finished and stay in the source — a sweep of the
 * twilight sky is simply a different program from an evening on one target, and
 * a window offering both looks like it needs a manual. They come back for
 * development with `astro.tabs.<name> = "on"` in local storage. */
function applyOptionalTabs() {
  document.querySelectorAll('[data-optional]').forEach((tab) => {
    let on = false;
    try {
      on = window.localStorage.getItem(`astro.tabs.${tab.dataset.optional}`) === 'on';
    } catch { on = false; }
    tab.hidden = !on;
  });
}

/* The right column holds two sorts of panel: ones tied to a tab, and ones the
   operator turns on from the topbar. When it ends up empty it gives its width
   back to the view, which is the whole point of getting them out of the way. */
function applyPanelVisibility() {
  let visible = 0;

  document.querySelectorAll('[data-only-tab]').forEach((panel) => {
    panel.hidden = panel.dataset.onlyTab !== state.tab;
    if (!panel.hidden) visible += 1;
  });

  document.querySelectorAll('[data-toggle-panel]').forEach((panel) => {
    const on = !!state.panels[panel.dataset.togglePanel];
    panel.hidden = !on;
    if (on) visible += 1;
  });

  document.querySelectorAll('[data-panel-toggle]').forEach((button) => {
    button.classList.toggle('active', !!state.panels[button.dataset.panelToggle]);
  });

  const layout = document.querySelector('.layout');
  layout.classList.toggle('no-right', visible === 0);
  // The camera, focuser, mount and guider panels drive an exposure. The
  // planning tabs are not driving anything, and the space is worth more to
  // them than a d-pad nobody is looking at.
  layout.classList.toggle('no-left', LEFTLESS_TABS.includes(state.tab));
  if (state.tab === 'image') repaintViewer();
  // A panel just turned on should show its contents now, not on the next tick.
  updateRunPanel(state.status);
}

function togglePanel(name) {
  state.panels[name] = !state.panels[name];
  try {
    window.localStorage.setItem('astro.panels', JSON.stringify(state.panels));
  } catch { /* a locked-down profile is not a reason to fail */ }
  applyPanelVisibility();
}

function restorePanels() {
  try {
    const stored = JSON.parse(window.localStorage.getItem('astro.panels') || '{}');
    if (stored && typeof stored === 'object') Object.assign(state.panels, stored);
  } catch { /* start with them all off */ }
  try {
    const stored = JSON.parse(window.localStorage.getItem('astro.devicePanels') || '{}');
    if (stored && typeof stored === 'object') Object.assign(state.devicePanels, stored);
  } catch { /* the camera panel alone, which is the point */ }
}

/* The device panels down the left. A night is spent looking at frames, not at a
   d-pad: the mount, the rotator, the focuser and the guider are all one click
   away and none of them is on screen until it is asked for. */

function applyDevicePanels() {
  document.querySelectorAll('[data-device-panel]').forEach((panel) => {
    panel.hidden = !state.devicePanels[panel.dataset.devicePanel];
  });
  document.querySelectorAll('[data-device-toggle]').forEach((button) => {
    button.classList.toggle('active', !!state.devicePanels[button.dataset.deviceToggle]);
  });
  if (state.tab === 'image') repaintViewer();
}

function toggleDevicePanel(name) {
  state.devicePanels[name] = !state.devicePanels[name];
  try {
    window.localStorage.setItem('astro.devicePanels',
      JSON.stringify(state.devicePanels));
  } catch { /* a locked-down profile is not a reason to fail */ }
  applyDevicePanels();
}

/** Open a device panel because something is about to need it. */
function revealDevicePanel(name) {
  if (state.devicePanels[name]) return;
  toggleDevicePanel(name);
}

/* Settings dialogs are paged rather than one column four screens tall. The rail
   is built from whatever `[data-pref]` sections the dialog contains, so adding a
   page is adding a section and nothing else. */

function buildPrefNav(navId, bodyId) {
  const nav = $(navId);
  const body = $(bodyId);
  if (!nav || !body) return;
  const pages = [...body.querySelectorAll('[data-pref]')];
  nav.innerHTML = '';
  for (const page of pages) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'pref-tab';
    button.textContent = page.dataset.prefLabel || page.dataset.pref;
    button.dataset.prefTarget = page.dataset.pref;
    button.addEventListener('click', () => showPrefPage(navId, bodyId, page.dataset.pref));
    nav.appendChild(button);
  }
  const first = pages.find((page) => !page.hidden) || pages[0];
  if (first) showPrefPage(navId, bodyId, first.dataset.pref);
}

function showPrefPage(navId, bodyId, name) {
  const nav = $(navId);
  const body = $(bodyId);
  if (!nav || !body) return;
  body.querySelectorAll('[data-pref]').forEach((page) => {
    page.hidden = page.dataset.pref !== name;
  });
  nav.querySelectorAll('.pref-tab').forEach((button) => {
    button.classList.toggle('active', button.dataset.prefTarget === name);
  });
  body.scrollTop = 0;
}

/** Open a dialog straight onto one of its pages. */
function openPrefPage(dialogId, name) {
  const dialog = $(dialogId);
  if (!dialog) return;
  const isEquipment = dialogId === 'equipmentDialog';
  showPrefPage(isEquipment ? 'equipmentNav' : 'settingsNav',
    isEquipment ? 'equipmentBody' : 'settingsBody', name);
}

/* --------------------------------------------------------------- settings */

async function loadSettings() {
  try {
    // Optics, camera and focus settings come back as this telescope sees them:
    // the shared value with its own overrides already on top.
    state.settings = await api(rigQuery('/api/settings'));
  } catch (error) {
    console.error(error);
    return null;
  }
  fillSettingsForm(state.settings);
  fillEquipmentForm(state.settings);
  for (const listener of settingsListeners) {
    try { listener(state.settings); } catch (error) { reportBrokenScript('Settings listener', error); }
  }
  return state.settings;
}

function fillSettingsForm(settings) {
  const site = settings.site || {};
  $('chkSiteFromMount').checked = site.useMount !== false;
  $('siteLatitude').value = site.latitude ?? '';
  $('siteLongitude').value = site.longitude ?? '';
  $('siteElevation').value = site.elevation ?? 0;
  const schedule = settings.schedule || {};
  $('schedMinAltitude').value = schedule.minAltitude ?? 30;
  $('schedPerFrame').value = schedule.perFrameSeconds ?? 15;
  $('schedFilterChange').value = schedule.filterChangeSeconds ?? 20;
  $('schedPerPanel').value = schedule.perPanelSeconds ?? 90;

  const optics = settings.optics || {};
  $('opticsFocalLength').value = optics.focalLength ?? '';
  $('opticsAngleHere').textContent = `${Number(optics.rotation ?? 0).toFixed(2)}°`;
  $('opticsSensorWidth').value = optics.sensorWidth ?? '';
  $('opticsSensorHeight').value = optics.sensorHeight ?? '';
  $('opticsPixelSize').value = optics.pixelSize ?? '';
  $('opticsUsableWidth').value = optics.usableWidth ?? '';
  $('opticsUsableHeight').value = optics.usableHeight ?? '';

  const solver = settings.solver || {};
  $('solverPath').value = solver.astapPath || '';
  $('solverRadius').value = solver.searchRadius ?? 15;
  $('solverDownsample').value = String(solver.downsample ?? 0);
  $('solverExposure').value = solver.exposure ?? 5;
  $('solverTolerance').value = solver.tolerance ?? 1;
  $('solverAttempts').value = solver.attempts ?? 3;
  $('solverTimeout').value = solver.timeout ?? 120;
  $('solverMaxStars').value = solver.maxStars ?? 500;
  $('astrometryEnabled').checked = solver.astrometryEnabled !== false;
  $('astrometryKey').value = solver.astrometryKey || '';
  $('astrometryUrl').value = solver.astrometryUrl || '';
  $('astrometryTimeout').value = Math.round((solver.astrometryTimeout ?? 600) / 60);

  const autoplan = settings.autoplan || {};
  $('apChoose').checked = autoplan.chooseExposures !== false;
  $('apLum').value = autoplan.luminanceExposure ?? 120;
  $('apBroad').value = autoplan.broadbandExposure ?? 180;
  $('apNarrow').value = autoplan.narrowbandExposure ?? 600;
  $('apMinExposure').value = autoplan.minExposure ?? 30;
  // Stored as a fraction, shown as a percentage: "40% lit" is how the Moon is
  // actually talked about.
  $('apNarrowAbove').value = Math.round((autoplan.narrowbandAboveIllumination ?? 0.4) * 100);
  $('apMoonSafe').value = autoplan.moonSafeDegrees ?? 90;
  $('apMinFrames').value = autoplan.minFramesPerFilter ?? 3;
  $('apShorten').checked = autoplan.shortenUnderMoon !== false;
  renderFilterExposures(autoplan.filterExposures || {},
    (settings.camera || {}).filterNames || []);

  const status = (state.status && state.status.solver) || {};
  const note = $('solverNote');
  note.textContent = status.available
    ? `Using ${status.executable}`
    : 'ASTAP was not found. Install it from hnsky.org, or give the full path to astap.exe above.';
  note.classList.toggle('missing', !status.available);

  const effective = site.effective || {};
  $('siteNote').classList.toggle('muted', true);
  if (effective.source === 'mount') {
    $('siteNote').textContent =
      `The mount reports ${effective.latitude.toFixed(4)}° N, ${effective.longitude.toFixed(4)}° E — ` +
      'that is what the planetarium is using. Untick above to enter your own.';
  } else if (effective.source === 'manual') {
    $('siteNote').textContent =
      `Using ${effective.latitude.toFixed(4)}° N, ${effective.longitude.toFixed(4)}° E. ` +
      'Longitude is degrees east of Greenwich, so the Americas are negative.';
  } else {
    $('siteNote').textContent =
      'No location yet. Connect a mount that reports one, or type it in — the ' +
      'planetarium needs it to draw the horizon, the meridian and the alt/az grid.';
  }
}

/* The Equipment dialog carries the settings that belong to the gear rather than
   the site: how to focus it, how to dither it, and how closely to watch it. */

const EQUIPMENT_FIELDS = [
  ['camera', 'gain', 'camGain', 'blankable'],
  ['camera', 'offset', 'camOffset', 'blankable'],
  ['camera', 'setpoint', 'camSetpoint', 'number'],
  ['camera', 'coolToleranceC', 'camTolerance', 'number'],
  ['camera', 'coolTimeoutMinutes', 'camCoolTimeout', 'number'],
  ['camera', 'warmRateCPerMinute', 'camWarmRate', 'number'],
  ['camera', 'coolAtStart', 'camCoolStart', 'check'],
  ['camera', 'warmAtEnd', 'camWarmEnd', 'check'],
  ['camera', 'waitForCooling', 'camWaitCooling', 'check'],
  ['sequencer', 'autofocusHfrPercent', 'afHfrPercent', 'number'],
  ['sequencer', 'focusStepSize', 'afStepSize', 'number'],
  ['sequencer', 'focusPoints', 'afPoints', 'number'],
  ['sequencer', 'focusExposure', 'afExposure', 'number'],
  ['sequencer', 'focusBacklash', 'afBacklash', 'number'],
  ['sequencer', 'focusMethod', 'afMethod', 'text'],
  ['sequencer', 'focusFramesPerPoint', 'afFrames', 'number'],
  ['sequencer', 'focusAttempts', 'afAttempts', 'number'],
  ['sequencer', 'focusMaxHfrRatio', 'afMaxRatio', 'number'],
  ['sequencer', 'autofocusIntervalMinutes', 'afInterval', 'number'],
  ['sequencer', 'autofocusTemperatureDelta', 'afTempDelta', 'number'],
  ['sequencer', 'autofocusOnStart', 'afOnStart', 'check'],
  ['sequencer', 'autofocusOnFilterChange', 'afOnFilter', 'check'],
  ['sequencer', 'autofocusFilter', 'afFilter', 'text'],
  ['sequencer', 'useFilterOffsets', 'afUseOffsets', 'check'],
  ['sequencer', 'meridianFlipEnabled', 'flipEnabled', 'check'],
  ['sequencer', 'flipPauseMinutes', 'flipPause', 'number'],
  ['sequencer', 'flipAfterMinutes', 'flipAfter', 'number'],
  ['sequencer', 'flipSolve', 'flipSolve', 'check'],
  ['sequencer', 'settleSeconds', 'seqSettle', 'number'],
  ['sequencer', 'parkAtEnd', 'seqParkAtEnd', 'check'],
  ['sequencer', 'stopTrackingAtEnd', 'seqStopTracking', 'check'],
  ['sequencer', 'homeAtStart', 'seqHomeAtStart', 'check'],
  ['sequencer', 'homeTimeoutMinutes', 'seqHomeTimeout', 'number'],
  ['sequencer', 'loopLeadMinutes', 'seqLoopLead', 'number'],
  ['sequencer', 'measureFrames', 'seqMeasure', 'check'],
  ['sequencer', 'solveEveryFrames', 'seqSolveEvery', 'number'],
  ['sequencer', 'pointingWarnArcmin', 'seqPointingWarn', 'number'],
  ['sequencer', 'focusWarnPercent', 'seqFocusWarn', 'number'],
  ['guiding', 'phd2Path', 'phd2Path', 'text'],
  ['guiding', 'phd2Profile', 'phd2Profile', 'text'],
  ['guiding', 'autoStartPhd2', 'phd2AutoStart', 'check'],
  ['guiding', 'connectEquipment', 'phd2ConnectEquipment', 'check'],
  ['guiding', 'autoSelectStar', 'phd2AutoSelect', 'check'],
  ['guiding', 'startWithSequence', 'guideWithSequence', 'check'],
  ['guiding', 'settleTimeoutSeconds', 'guideSettleTimeout', 'number'],
  ['guiding', 'ditherEnabled', 'ditherEnabled', 'check'],
  ['guiding', 'ditherEveryFrames', 'ditherEvery', 'number'],
  ['guiding', 'ditherPixels', 'ditherAmount', 'number'],
  ['guiding', 'ditherRaOnly', 'ditherRaOnly', 'check'],
  ['guiding', 'settlePixels', 'ditherSettlePixels', 'number'],
  ['guiding', 'settleTime', 'ditherSettleTime', 'number'],
  ['guiding', 'settleTimeout', 'ditherSettleTimeout', 'number'],
  ['recovery', 'enabled', 'recEnabled', 'check'],
  ['recovery', 'guidingRecovery', 'recGuiding', 'check'],
  ['recovery', 'guideGraceSeconds', 'recGuideGrace', 'number'],
  ['recovery', 'guideRestartAttempts', 'recGuideAttempts', 'number'],
  ['recovery', 'recalibrateAfterAttempts', 'recRecalAfter', 'number'],
  ['recovery', 'discardLostFrames', 'recDiscardLost', 'check'],
  ['recovery', 'pointingRecovery', 'recPointing', 'check'],
  ['recovery', 'recentreArcmin', 'recRecentre', 'number'],
  ['recovery', 'recentreAttempts', 'recRecentreAttempts', 'number'],
  ['recovery', 'frameRetryAttempts', 'recFrameRetries', 'number'],
  ['recovery', 'frameRetrySeconds', 'recFrameWait', 'number'],
  ['recovery', 'reconnectDevices', 'recReconnect', 'check'],
  ['recovery', 'maxPerTarget', 'recMaxPerTarget', 'number'],
  ['recovery', 'cooldownSeconds', 'recCooldown', 'number'],
  ['recovery', 'onGiveUp', 'recOnGiveUp', 'text'],
  ['piercam', 'enabled', 'pierEnabled', 'check'],
  ['piercam', 'auto', 'pierAuto', 'check'],
  // Shown as a percentage because "aim for 35% of full scale" is what it means;
  // stored as a fraction because that is what it is.
  ['piercam', 'target', 'pierTarget', 'percent'],
  ['piercam', 'deadband', 'pierDeadband', 'percent'],
  ['piercam', 'minExposure', 'pierMinExposure', 'number'],
  ['piercam', 'maxExposure', 'pierMaxExposure', 'number'],
  // Blank here means something — "use the camera's own range" — so it has to be
  // sent as an explicit null rather than left out.
  ['piercam', 'maxGain', 'pierMaxGain', 'nullable'],
  ['piercam', 'intervalSeconds', 'pierInterval', 'number'],
  ['piercam', 'maxDim', 'pierMaxDim', 'number'],
  ['piercam', 'gamma', 'pierGamma', 'number'],
  ['safety', 'enabled', 'safEnabled', 'check'],
  ['safety', 'blockStart', 'safBlockStart', 'check'],
  ['safety', 'unreachableIsUnsafe', 'safUnreachable', 'check'],
  ['safety', 'onUnsafe', 'safOnUnsafe', 'text'],
  ['safety', 'graceSeconds', 'safGrace', 'number'],
  ['safety', 'resumeAfterSeconds', 'safResume', 'number'],
  ['notify', 'enabled', 'notEnabled', 'check'],
  ['notify', 'webhookUrl', 'notWebhook', 'text'],
  ['notify', 'messageField', 'notField', 'text'],
  ['notify', 'smtpHost', 'notSmtpHost', 'text'],
  ['notify', 'smtpPort', 'notSmtpPort', 'number'],
  ['notify', 'smtpUser', 'notSmtpUser', 'text'],
  ['notify', 'smtpFrom', 'notSmtpFrom', 'text'],
  ['notify', 'smtpTo', 'notSmtpTo', 'text'],
  ['notify', 'smtpStartTls', 'notStartTls', 'check'],
  ['notify', 'onSequenceEnd', 'notOnSequenceEnd', 'check'],
  ['notify', 'onFailure', 'notOnFailure', 'check'],
  ['notify', 'onRecovery', 'notOnRecovery', 'check'],
  ['notify', 'onGiveUp', 'notOnGiveUp', 'check'],
  ['notify', 'onSafety', 'notOnSafety', 'check'],
  ['notify', 'onActivity', 'notOnActivity', 'check'],
  ['notify', 'onCalibration', 'notOnCalibration', 'check'],
  ['notify', 'onWarning', 'notOnWarning', 'check'],
  ['notify', 'minSecondsBetween', 'notMinGap', 'number'],
];

function fillEquipmentForm(settings) {
  for (const [section, key, id, kind] of EQUIPMENT_FIELDS) {
    const node = $(id);
    if (!node) continue;
    const value = (settings[section] || {})[key];
    if (kind === 'check') { node.checked = !!value; continue; }
    // A blankable field left empty means "whatever the camera already has"; a
    // nullable one means "the camera's own range".
    if (value === undefined || value === null) {
      if (kind === 'blankable' || kind === 'nullable') node.value = '';
      continue;
    }
    node.value = kind === 'percent'
      ? String(Math.round(Number(value) * 1000) / 10) : String(value);
  }
  $('capRoot').value = (settings.capture || {}).rootDirectory || '';
  $('calRoot').value = (settings.calibration || {}).libraryDirectory || '';
  // Comes back as a mask when one is stored, and is only sent again if retyped.
  $('notSmtpPassword').value = (settings.notify || {}).smtpPassword || '';
  updateNotifyNote();
  const optics = settings.optics || {};
  // Not overwritten while it is being typed into, or every status refresh
  // would snatch the digits back.
  if (document.activeElement !== $('opticsAngle')) {
    $('opticsAngle').value = Number(optics.rotation ?? 0).toFixed(2);
  }
  $('opticsAngleFromSolve').checked = optics.angleFromSolve !== false;
  const camera = settings.camera || {};
  filterBandpass = { ...(camera.filterBandpass || {}) };
  if ($('camColour')) $('camColour').checked = !!camera.colour;
  renderFilterChips(camera.filterNames || []);
  fillFixedFilter(camera.fixedFilter || '');
  renderFilterOffsets((settings.sequencer || {}).filterOffsets || {});
  fillFocusFilter((settings.sequencer || {}).autofocusFilter || '');
  fillOffsetReference();
  updateFilterNote();
  updateSolvedAngle();
  updateCapturePreview();
  updateCalibrationPreview();
  refreshPhd2Note();
}

/* Names offered as suggestions while typing a slot. Not a menu to pick from and
   not an order — the order is the wheel's, and it is whatever the operator
   types into the numbered boxes. */
/* One letter per filter, the program's one spelling: whatever is typed - Ha,
   H-alpha, OIII, luminance - is folded to it when saved. */
const FILTER_SUGGESTIONS = ['L', 'R', 'G', 'B', 'S', 'H', 'O', 'Dual'];

/* The filters this telescope carries, one per wheel slot, in slot order.
 *
 * Held here rather than read back out of the DOM, and there is exactly one way
 * to set it: type the name into the numbered box. It used to be a palette of
 * chips *and* an ordered strip, which was two controls for one decision — and
 * the palette's order was what actually got saved, so LRGB + SHO always came
 * back with Ha before SII however it was entered. */
let filterOrder = [];
/* Filter name -> bandpass in nanometres. Kept beside the order rather than in
   it because it is a property of the filter, not of the slot it sits in. */
let filterBandpass = {};

/** Which filters this telescope carries, in slot order. Blanks are dropped. */
function readFilterNames() {
  return filterOrder.map((name) => name.trim()).filter(Boolean);
}

function renderFilterChips(chosen) {
  filterOrder = (chosen || []).map((name) => String(name).trim());
  drawFilterSlots();
}

function drawFilterSlots() {
  const host = $('camFilterSlots');
  if (!host) return;
  const options = $('filterNameOptions');
  if (options && !options.options.length) {
    for (const name of FILTER_SUGGESTIONS) options.appendChild(new Option(name));
  }

  host.innerHTML = '';
  if (!filterOrder.length) {
    host.className = 'filter-slots muted-empty';
    host.textContent = 'No slots yet — add one, or take them from the wheel';
    return;
  }
  host.className = 'filter-slots';
  filterOrder.forEach((name, index) => {
    const slot = document.createElement('div');
    slot.className = 'filter-slot';

    const number = document.createElement('span');
    number.className = 'slot-number';
    number.textContent = String(index + 1);

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'slot-name-input';
    input.setAttribute('list', 'filterNameOptions');
    input.spellcheck = false;
    input.maxLength = 24;
    input.placeholder = 'empty';
    input.value = name;
    // Kept in the array as it is typed, so nothing has to be read back out of
    // the DOM in an order the DOM happens to be in.
    input.addEventListener('input', () => {
      filterOrder[index] = input.value;
      updateFilterNote();
    });
    // The dependent lists only need rebuilding once the name has settled.
    input.addEventListener('change', () => {
      filterOrder[index] = input.value.trim();
      filtersChanged();
    });

    // Bandpass, in nanometres. Only narrowband has one that matters, and it
    // matters a great deal: a 3 nm Ha and a 7 nm Ha are not the same data, and
    // a collaboration is entitled to say which it will take. Without this there
    // was nowhere to state it, so a project asking for 3 nm could be joined by
    // nobody at all.
    const nm = document.createElement('input');
    nm.type = 'number';
    nm.className = 'slot-nm-input';
    nm.min = '0'; nm.max = '500'; nm.step = '0.5';
    nm.placeholder = 'nm';
    nm.title = 'Bandpass in nanometres. Leave blank for broadband.';
    nm.value = filterBandpass[name] === undefined || filterBandpass[name] === null
      ? '' : String(filterBandpass[name]);
    nm.addEventListener('change', () => {
      const current = filterOrder[index];
      if (!current) return;
      if (nm.value === '') delete filterBandpass[current];
      else filterBandpass[current] = Number(nm.value);
      filtersChanged();
    });

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn small ghost icon';
    remove.textContent = '×';
    remove.title = 'Remove this slot';
    remove.addEventListener('click', () => {
      filterOrder.splice(index, 1);
      filtersChanged();
    });

    slot.append(number, input, nm, remove);
    host.appendChild(slot);
  });
}

/** What each filter's bandpass is, dropping any that lost its slot. */
function readFilterBandpass() {
  const kept = {};
  for (const name of readFilterNames()) {
    const value = filterBandpass[name];
    if (value !== undefined && value !== null && value !== '') {
      kept[name] = Number(value);
    }
  }
  return kept;
}

/** Everything that follows the filter list, after it has been changed. */
function filtersChanged() {
  drawFilterSlots();
  updateFilterNote();
  fillFixedFilter();
  // Keep whatever has already been typed into the offsets that survive.
  renderFilterOffsets(readFilterOffsets());
  fillFocusFilter();
  fillOffsetReference();
}

/* The sub length Auto-arrange reaches for, per filter.
 *
 * Built from the filters the telescope actually carries, plus anything already
 * configured — so a wheel with a filter the palette has never heard of still
 * gets a row rather than silently falling back to a class default. */
function renderFilterExposures(stored, carried) {
  const host = $('apFilterExposures');
  if (!host) return;
  const names = [...new Set([...(carried || []), ...Object.keys(stored || {})])];
  host.innerHTML = '';
  if (!names.length) {
    host.innerHTML = '<p class="small muted">Choose this telescope\'s filters '
      + 'in <b>Equipment → Filters</b> first — these are per filter.</p>';
    return;
  }
  for (const name of names) {
    const row = document.createElement('label');
    row.className = 'offset-row';
    row.innerHTML = '<span class="offset-name"></span>';
    row.querySelector('.offset-name').textContent = name;
    const input = document.createElement('input');
    input.type = 'number';
    input.step = '10';
    input.min = '1';
    input.max = '3600';
    input.dataset.filterExposure = name;
    input.value = String(Number(stored[name] ?? 0) || '');
    input.placeholder = 'fallback';
    row.appendChild(input);
    host.appendChild(row);
  }
}

function readFilterExposures() {
  const table = {};
  for (const input of document.querySelectorAll('[data-filter-exposure]')) {
    const value = Number(input.value);
    // A row left blank means "use the fallback for its class", so it is left
    // out of the table rather than written as a zero.
    if (Number.isFinite(value) && value > 0) {
      table[input.dataset.filterExposure] = value;
    }
  }
  return table;
}

/* ------------------------------------------------ measured overheads */

/* What the rig has timed itself at, and how sure it is.
 *
 * The sample count matters as much as the figure: one download says almost
 * nothing and forty says a great deal, so both are shown rather than a number
 * that looks equally authoritative either way. */
async function loadOverheads() {
  let payload;
  try {
    payload = await api('/api/overheads');
  } catch (error) { return null; }
  state.overheads = payload;
  renderOverheads(payload);
  return payload;
}

function renderOverheads(payload) {
  const host = $('overheadTable');
  if (!host) return;
  const measured = payload.overheads || {};
  host.innerHTML = '';

  const rows = [
    ['download', 'per frame'],
    ['dither', 'per frame, when dithering'],
    ['filterChange', 'per filter change'],
    ['slew', 'per target or mosaic panel'],
    ['focusPerPoint', 'per sweep point, excluding its exposure'],
    ['focusFixed', 'per sweep, whatever its length'],
  ];
  for (const [key, when] of rows) {
    const item = measured[key];
    if (!item) continue;
    const row = document.createElement('div');
    row.className = `overhead-row${item.measured ? '' : ' assumed'}`;
    row.innerHTML = '<b class="what"></b><span class="value mono"></span>'
      + '<span class="conf small muted"></span><span class="when small muted"></span>';
    row.querySelector('.what').textContent = item.label;
    row.querySelector('.value').textContent = `${item.seconds.toFixed(1)} s`;
    row.querySelector('.conf').textContent = item.measured
      ? `${item.samples} sample${item.samples === 1 ? '' : 's'}`
        + (item.spread ? `, spread ${item.spread.toFixed(1)}s` : '')
      : `assumed ${item.default.toFixed(0)}s`;
    row.querySelector('.when').textContent = when;
    host.appendChild(row);
  }

  const total = document.createElement('div');
  total.className = 'overhead-row total';
  total.innerHTML = '<b class="what">A full autofocus sweep</b>'
    + '<span class="value mono"></span>'
    + '<span class="conf small muted"></span>'
    + '<span class="when small muted">at the points and exposure set above</span>';
  const seconds = Number(payload.focusRunSeconds) || 0;
  total.querySelector('.value').textContent = `${(seconds / 60).toFixed(1)} min`;
  total.querySelector('.conf').textContent = 'worked out, not timed whole';
  host.appendChild(total);

  $('schedUseMeasured').checked = payload.useMeasured !== false;
  const status = $('overheadStatus');
  if (payload.busy) {
    status.textContent = `Measuring… ${payload.detail || ''}`;
    status.className = 'small';
  } else if (payload.detail) {
    status.textContent = payload.detail;
    status.className = 'small muted';
  }
  $('btnCalibrateOverheads').disabled = !!payload.busy;
}

/* Focus offsets: one number per filter this telescope carries, relative to each
   other rather than absolute, so the filter you focus on sits at 0 and the rest
   say how far the focuser has to travel from there. */

function renderFilterOffsets(stored) {
  const host = $('filterOffsetRows');
  if (!host) return;
  const names = readFilterNames();
  host.innerHTML = '';
  if (!names.length) {
    host.innerHTML = '<p class="small muted">Choose this telescope\'s filters '
      + 'above first — the offsets are per filter.</p>';
    return;
  }
  for (const name of names) {
    const row = document.createElement('label');
    row.className = 'offset-row';
    row.innerHTML = `<span class="offset-name">${name}</span>`;
    const input = document.createElement('input');
    input.type = 'number';
    input.step = '1';
    input.min = '-100000';
    input.max = '100000';
    input.dataset.filterOffset = name;
    input.value = String(Number(stored[name] ?? 0) || 0);
    row.appendChild(input);
    host.appendChild(row);
  }
}

function readFilterOffsets() {
  const offsets = {};
  for (const input of document.querySelectorAll('[data-filter-offset]')) {
    const value = Number(input.value);
    offsets[input.dataset.filterOffset] = Number.isFinite(value) ? Math.round(value) : 0;
  }
  return offsets;
}

/** Which filter autofocus sweeps through. Follows the chips above. */
function fillFocusFilter(selected) {
  const select = $('afFilter');
  if (!select) return;
  const wanted = selected !== undefined ? selected : select.value;
  const names = readFilterNames();
  select.innerHTML = '';
  select.appendChild(new Option('whatever is in the path', ''));
  for (const name of names) select.appendChild(new Option(name, name));
  select.value = names.includes(wanted) ? wanted : '';
}

/** The "in the light path now" list follows whatever is selected above. */
function fillFixedFilter(selected) {
  const select = $('camFixedFilter');
  if (!select) return;
  const wanted = selected !== undefined ? selected : select.value;
  const names = readFilterNames();
  select.innerHTML = '';
  select.appendChild(new Option('nothing / not known', ''));
  for (const name of names) select.appendChild(new Option(name, name));
  select.value = names.includes(wanted) ? wanted : '';

  const rig = currentRig() || {};
  const wheel = (rig.devices || {}).filterwheel;
  select.disabled = !!(wheel && wheel.connected);
  select.title = select.disabled
    ? 'A filter wheel is connected, so the wheel decides what is in the path'
    : 'What is fitted, on a telescope with no wheel to change it';
}

/** Say what the wheel itself is reporting, so the two can be compared. */
function updateFilterNote() {
  const node = $('camFilterNote');
  if (!node) return;
  const rig = currentRig() || {};
  const wheel = (rig.devices || {}).filterwheel;
  const typed = readFilterNames();
  if (!wheel || !wheel.connected) {
    node.textContent = typed.length
      ? `${typed.length} filter(s). The wheel is not connected, so these are `
        + 'what everything else will offer you.'
      : 'No filter wheel connected and no names set — the observatory-wide list '
        + 'in Site & Optics will be used.';
    node.classList.remove('warn-text');
    return;
  }
  const live = wheel.names || [];
  const same = live.length === typed.length
    && live.every((name, i) => name === typed[i]);
  let note = `The wheel reports ${live.length} slot(s): ${live.join(', ') || '—'}`;
  // The same filters in a different order is the failure worth naming: it looks
  // right in every list and puts the wheel on the wrong slot all night.
  if (typed.length && !same
      && live.length === typed.length
      && [...live].sort().join() === [...typed].sort().join()) {
    note += '  —  the same filters in a different order. The order here is the '
      + 'one that will be used, so make it the order they are fitted in.';
  }
  node.textContent = note;
  node.classList.toggle('warn-text', typed.length > 0 && !same);
}

function bindFilterChips() {
  $('btnFilterAdd').addEventListener('click', () => {
    filterOrder.push('');
    drawFilterSlots();
    // Straight into the box that was just added: adding a slot is only ever the
    // first half of naming one.
    const inputs = document.querySelectorAll('#camFilterSlots .slot-name-input');
    const last = inputs[inputs.length - 1];
    if (last) last.focus();
  });
  $('btnFilterFromWheel').addEventListener('click', () => {
    const rig = currentRig() || {};
    const wheel = (rig.devices || {}).filterwheel;
    if (!wheel || !wheel.connected || !(wheel.names || []).length) {
      toast('The filter wheel is not connected, or reports no names', 'error');
      return;
    }
    // The wheel's own order is the physical order, which is exactly what this
    // list wants — when the driver reports real names rather than slot numbers.
    renderFilterChips(wheel.names);
    filtersChanged();
  });
  $('btnFilterClear').addEventListener('click', () => {
    renderFilterChips([]);
    fillFixedFilter('');
    filtersChanged();
  });
}

/** What the last plate solve made of the camera angle, if anything. */
function updateSolvedAngle() {
  const node = $('opticsAngleSolved');
  if (!node) return;
  const result = state.solveResult;
  const rig = currentRig() || {};
  const rotator = (rig.devices || {}).rotator;
  if (rotator && rotator.connected) {
    node.value = `rotator at ${Number(rotator.position ?? 0).toFixed(2)}°`;
    return;
  }
  node.value = result
    ? `${(((result.rotation % 360) + 360) % 360).toFixed(2)}° from the last solve`
    : 'nothing solved yet';
}

/** Where the masters actually live, once a blank field is resolved. */
function updateCalibrationPreview() {
  const node = $('calRootPreview');
  if (!node) return;
  const typed = $('calRoot').value.trim();
  const status = (state.status && state.status.calibration) || {};
  node.textContent = typed
    ? `${typed} / masters`
    : (status.libraryRoot ? `${status.libraryRoot} / masters`
      : 'A folder under the data directory will be used.');
}

/* Measuring how far apart the filters focus.
 *
 * The run is slow — a sweep per filter per pass — so it reports as it goes, and
 * the numbers it has so far are worth seeing before it finishes. */

function fillOffsetReference(selected) {
  const select = $('offReference');
  if (!select) return;
  const wanted = selected !== undefined ? selected : select.value;
  const names = readFilterNames();
  select.innerHTML = '';
  select.appendChild(new Option('the autofocus filter', ''));
  for (const name of names) select.appendChild(new Option(name, name));
  select.value = names.includes(wanted) ? wanted : '';
}

function updateOffsetRun(run) {
  const note = $('offsetsNote');
  if (!note) return;
  const running = !!run.running;
  $('btnOffsetsRun').disabled = running;
  $('btnOffsetsRun').textContent = running ? 'Measuring…' : 'Measure offsets';
  $('btnOffsetsAbort').hidden = !running;

  const progress = $('offsetsProgress');
  progress.hidden = !running && !run.result && !run.error;
  if (!progress.hidden) {
    const rows = [];
    if (running) {
      rows.push(`<b>Pass ${run.pass}/${run.passes}</b> — ${run.done} of `
        + `${run.total} sweeps done`);
    }
    // What it has measured so far, per filter, as the raw per-pass numbers.
    // Two passes that disagree by ninety steps are two guesses, and seeing the
    // pair is the only way to know that.
    for (const [name, values] of Object.entries(run.offsets || {})) {
      const spread = (run.spread || {})[name];
      rows.push(`${name}: ${values.map((v) => (v > 0 ? `+${v}` : v)).join(', ')}`
        + (spread ? `  <span class="warn-text">(${spread} apart)</span>` : ''));
    }
    for (const [name, why] of Object.entries(run.skipped || {})) {
      rows.push(`<span class="warn-text">${name}: ${why}</span>`);
    }
    progress.innerHTML = rows.map((r) => `<div>${r}</div>`).join('');
  }

  if (run.error) {
    note.textContent = run.error;
    note.classList.add('warn-text');
    return;
  }
  // The run writes the offsets straight into this telescope's settings, so the
  // boxes above are stale the moment it lands. Pulled once per finished run
  // rather than on every tick, and never while somebody is typing into them.
  if (run.finished && run.finished !== state.offsetRunFinished) {
    state.offsetRunFinished = run.finished;
    if (run.result && !document.activeElement.matches('input')) loadSettings();
  }

  note.classList.remove('warn-text');
  if (running) {
    note.textContent = run.message || 'measuring…';
  } else if (run.result) {
    const worst = Math.max(0, ...Object.values(run.spread || {}));
    note.textContent = `Saved, measured against ${run.reference}.`
      + (worst > 0 ? `  Passes agreed to within ${worst} steps.` : '');
  } else {
    note.textContent = 'Not measured yet.';
  }
}

/** Whether anything has been sent, and whether anything could be. */
function updateNotifyNote() {
  const node = $('notifyNote');
  if (!node) return;
  const status = (state.status && state.status.notify) || {};
  const bits = [];
  if (!status.configured) {
    bits.push('Nothing is configured, so nothing can be sent.');
  } else if (!status.enabled) {
    bits.push('Configured, but switched off.');
  } else {
    if (status.discord) bits.push('Discord');
    bits.push(`${status.sent || 0} sent`);
    if (status.failed) bits.push(`${status.failed} failed`);
  }
  if (status.lastError) bits.push(`last error: ${status.lastError}`);
  node.textContent = bits.join('  ·  ');
  node.classList.toggle('warn-text', !!status.lastError);
}

/** Say whether PHD2 can be found and whether it is already up. */
async function refreshPhd2Note() {
  const note = $('phd2Note');
  if (!note) return;
  let info;
  try {
    info = await api('/api/guider/phd2');
  } catch {
    note.textContent = '';
    return;
  }
  const parts = [];
  parts.push(info.running ? 'PHD2 is running.'
    : 'PHD2 is not running — it will be started when you connect the guider.');
  parts.push(info.found ? `Program: ${info.executable}`
    : 'Its program could not be found; give the full path to phd2.exe above.');
  note.textContent = parts.join('  ');
  note.classList.toggle('missing', !info.found && !info.running);
}

/** Show the folder the next frame will actually land in. */
function updateCapturePreview() {
  const node = $('capPreview');
  if (!node) return;
  const rig = currentRig();
  const capture = (rig && rig.capture) || (state.status && state.status.capture) || {};
  const root = $('capRoot').value.trim() || capture.rootDir || '…';
  const target = (capture.target || '').trim() || '<target>';
  const night = capture.night || '<night>';
  node.textContent = capture.customDir
    ? `A folder was set by hand on the Image tab: ${capture.sessionDir}`
    : `${root} / ${target} / ${night} / …fits`;
  node.classList.toggle('warn-text', !!capture.customDir);
}

async function saveEquipmentSettings() {
  const payload = { camera: {}, sequencer: {}, guiding: {}, recovery: {},
    piercam: {}, safety: {}, notify: {} };
  for (const [section, key, id, kind] of EQUIPMENT_FIELDS) {
    const node = $(id);
    if (!node) continue;
    if (kind === 'check') { payload[section][key] = node.checked; continue; }
    // A path or a profile name is text; blanking it means "work it out".
    if (kind === 'text') { payload[section][key] = node.value.trim(); continue; }
    if (kind === 'blankable' && node.value.trim() === '') continue;
    // Blank means "the camera's own range", which has to be said explicitly.
    if (kind === 'nullable') {
      payload[section][key] = node.value.trim() === '' ? null : Number(node.value);
      continue;
    }
    if (kind === 'percent') { payload[section][key] = Number(node.value) / 100; continue; }
    payload[section][key] = Number(node.value);
  }
  try {
    // Camera and focus settings are this telescope's; the root folder and the
    // dithering belong to the observatory, so they go in unscoped.
    payload.camera.filterNames = readFilterNames();
    payload.camera.filterBandpass = readFilterBandpass();
    payload.camera.fixedFilter = $('camFixedFilter').value;
    if ($('camColour')) payload.camera.colour = $('camColour').checked;
    if (Object.keys(payload.camera).length) {
      await api(rigQuery('/api/settings/camera'), 'POST', payload.camera);
    }
    // The camera angle is this telescope's, so it goes in scoped.
    const angle = Number($('opticsAngle').value);
    await api(rigQuery('/api/settings/optics'), 'POST', {
      rotation: Number.isFinite(angle) ? ((angle % 360) + 360) % 360 : 0,
      angleFromSolve: $('opticsAngleFromSolve').checked,
    });
    await api('/api/settings/capture', 'POST', { rootDirectory: $('capRoot').value });
    await api('/api/calibration/settings', 'POST',
      { libraryDirectory: $('calRoot').value });
    payload.sequencer.filterOffsets = readFilterOffsets();
    await api(rigQuery('/api/settings/sequencer'), 'POST', payload.sequencer);
    await api('/api/settings/guiding', 'POST', payload.guiding);
    await api('/api/settings/recovery', 'POST', payload.recovery);
    // The pier camera watches the whole observatory, not one optical train.
    await api('/api/settings/piercam', 'POST', payload.piercam);
    await api('/api/settings/safety', 'POST', payload.safety);
    // The password is only sent when it has actually been typed into: the form
    // is filled from the server with a mask, and sending that back would set
    // the password to three asterisks.
    const password = $('notSmtpPassword').value;
    if (password && password !== MASKED) payload.notify.smtpPassword = password;
    await api('/api/settings/notify', 'POST', payload.notify);
  } catch (error) {
    toast(error.message, 'error');
    return false;
  }
  await loadSettings();
  toast('Equipment settings saved', 'success');
  return true;
}

const numberOrNull = (id) => {
  const raw = $(id).value.trim();
  if (raw === '') return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
};

async function saveSettings() {
  const site = { useMount: $('chkSiteFromMount').checked };
  const latitude = numberOrNull('siteLatitude');
  const longitude = numberOrNull('siteLongitude');
  if (latitude !== null) site.latitude = latitude;
  if (longitude !== null) site.longitude = longitude;
  const elevation = numberOrNull('siteElevation');
  if (elevation !== null) site.elevation = elevation;

  const optics = {};
  for (const [id, key] of [['opticsFocalLength', 'focalLength'],
    ['opticsSensorWidth', 'sensorWidth'],
    ['opticsSensorHeight', 'sensorHeight'], ['opticsPixelSize', 'pixelSize'],
    ['opticsUsableWidth', 'usableWidth'], ['opticsUsableHeight', 'usableHeight']]) {
    const value = numberOrNull(id);
    if (value !== null) optics[key] = value;
  }

  const solver = {
    astapPath: $('solverPath').value.trim(),
    searchRadius: Number($('solverRadius').value) || 15,
    downsample: Number($('solverDownsample').value) || 0,
    exposure: Number($('solverExposure').value) || 5,
    tolerance: Number($('solverTolerance').value) || 1,
    attempts: Number($('solverAttempts').value) || 3,
    timeout: Number($('solverTimeout').value) || 120,
    maxStars: Number($('solverMaxStars').value) || 500,
    astrometryEnabled: $('astrometryEnabled').checked,
    astrometryKey: $('astrometryKey').value.trim(),
    astrometryUrl: $('astrometryUrl').value.trim()
      || 'https://nova.astrometry.net/api/',
    // Shown in minutes because that is the scale it works on; stored in
    // seconds like every other timeout here.
    astrometryTimeout: (Number($('astrometryTimeout').value) || 10) * 60,
  };

  try {
    await api('/api/settings/site', 'POST', site);
    // The optics belong to the selected telescope — two scopes on one mount do
    // not share a focal length — while the site and the solver are shared.
    if (Object.keys(optics).length) {
      await api(rigQuery('/api/settings/optics'), 'POST', optics);
    }
    await api('/api/settings/solver', 'POST', solver);
    const schedule = {};
    for (const [id, key] of [['schedMinAltitude', 'minAltitude'],
      ['schedPerFrame', 'perFrameSeconds'],
      ['schedFilterChange', 'filterChangeSeconds'],
      ['schedPerPanel', 'perPanelSeconds']]) {
      const value = numberOrNull(id);
      if (value !== null) schedule[key] = value;
    }
    if (Object.keys(schedule).length) {
      await api('/api/settings/schedule', 'POST', schedule);
    }
    await api('/api/settings/autoplan', 'POST', {
      chooseExposures: $('apChoose').checked,
      filterExposures: readFilterExposures(),
      luminanceExposure: Number($('apLum').value) || 120,
      broadbandExposure: Number($('apBroad').value) || 180,
      narrowbandExposure: Number($('apNarrow').value) || 600,
      minExposure: Number($('apMinExposure').value) || 30,
      narrowbandAboveIllumination: (Number($('apNarrowAbove').value) || 0) / 100,
      moonSafeDegrees: Number($('apMoonSafe').value) || 90,
      minFramesPerFilter: Number($('apMinFrames').value) || 3,
      shortenUnderMoon: $('apShorten').checked,
    });
  } catch (error) {
    toast(error.message, 'error');
    return;
  }
  await loadSettings();
  toast('Settings saved', 'success');
  $('settingsDialog').close();
}

/* ---------------------------------------------------------------- saving */

function updateOutput(capture) {
  if (!capture) return;
  // The target is picked up once so a half-typed name is never overwritten; the
  // folder and the save switch mirror the server, which owns them.
  if (!state.outputSynced) {
    state.outputSynced = true;
    $('targetName').value = capture.target || '';
  }
  if (document.activeElement !== $('saveDir')) $('saveDir').value = capture.sessionDir || '';
  $('chkSave').checked = capture.saveEnabled !== false;
  $('panel-camera').classList.toggle('not-saving', capture.saveEnabled === false);
  updateFilePreview(capture);
}

/** Show the shape of the filename the next frame will get. */
function updateFilePreview(capture) {
  const node = $('filePreview');
  if (!node) return;
  if (capture && capture.saveEnabled === false) {
    node.textContent = 'Not saving — frames are shown but never written to disk.';
    return;
  }
  const target = ($('targetName').value || '').trim().replace(/[^A-Za-z0-9._+-]+/g, '_');
  const exposure = Number($('exposure').value) || 0;
  const type = $('frameType').value;
  const rig = currentRig();
  const devices = (rig && rig.devices) || (state.status && state.status.devices) || {};
  const wheel = devices.filterwheel || {};
  const camera = devices.camera || {};
  const filter = (wheel.connected && wheel.names && wheel.names[wheel.target]) || null;
  const temperature = camera.temperature;

  if (!target) {
    node.textContent = `${type.toUpperCase()}_${filter ? filter + '_' : ''}${exposure}s_g${camera.gain ?? 0}_bin${camera.binning ?? 1}_<time>.fits`;
    return;
  }
  const parts = [target];
  if (type !== 'light') parts.push(type.toUpperCase());
  if (filter) parts.push(filter);
  parts.push(`${exposure}s`);
  if (temperature !== null && temperature !== undefined) parts.push(`${temperature.toFixed(0)}C`);
  if ((camera.binning || 1) > 1) parts.push(`bin${camera.binning}`);
  node.textContent = `${parts.join('_')}_0001.fits`;
}

function pushOutput(body) {
  return send(rigQuery('/api/capture/output'), 'POST', body);
}

/* ------------------------------------------------------------- image list */

async function refreshImages() {
  try {
    const result = await api('/api/images');
    state.images = result.images;
    setText('imageCount', String(state.images.length));
    const host = $('imageList');
    if (!state.images.length) {
      host.className = 'image-list muted-empty';
      host.textContent = 'No captures yet';
      return;
    }
    host.className = 'image-list';
    host.innerHTML = '';
    for (const image of state.images) {
      const row = document.createElement('div');
      row.className = `image-row${image.id === state.currentId ? ' active' : ''}`;
      row.dataset.imageId = image.id;
      const time = new Date(image.timestamp * 1000).toLocaleTimeString([], { hour12: false });
      const bits = [
        image.frame_type,
        `${image.exposure}s`,
        image.filter || null,
        `g${image.gain}`,
        image.binning > 1 ? `bin${image.binning}` : null,
      ].filter(Boolean);
      row.innerHTML = `<div class="name"></div><div class="time">${time}</div><div class="meta"></div>`;
      row.querySelector('.name').textContent = image.filename;
      row.querySelector('.meta').textContent = bits.join(' · ');
      row.addEventListener('click', () => selectImage(image.id));
      host.appendChild(row);
    }
  } catch (error) {
    console.error(error);
  }
}

/* ---------------------------------------------------------------- stretch */

const sliderToBlack = (v) => Math.pow(v / 1000, 3);
const blackToSlider = (b) => Math.round(1000 * Math.cbrt(Math.max(0, b)));
const sliderToWhite = (v) => v / 1000;
const whiteToSlider = (w) => Math.round(w * 1000);
const sliderToMidtone = (v) => 0.5 * Math.pow(0.001, 1 - v / 1000);
const midtoneToSlider = (m) => Math.round(1000 * (1 - Math.log(0.5 / Math.max(m, 1e-6)) / Math.log(1000)));

function currentStretch() {
  return {
    black: sliderToBlack(Number($('black').value)),
    white: sliderToWhite(Number($('white').value)),
    midtone: sliderToMidtone(Number($('midtone').value)),
  };
}

function updateStretchLabels() {
  const s = currentStretch();
  setText('blackValue', (s.black * 65535).toFixed(0));
  setText('whiteValue', (s.white * 65535).toFixed(0));
  setText('midtoneValue', s.midtone.toFixed(4));
}

function setStretchSliders(params) {
  $('black').value = blackToSlider(params.black);
  $('white').value = whiteToSlider(params.white);
  $('midtone').value = midtoneToSlider(params.midtone);
  updateStretchLabels();
}

function stretchQuery() {
  if ($('chkAuto').checked) return `auto=true&invert=${$('chkInvert').checked}`;
  const s = currentStretch();
  const white = Math.max(s.white, s.black + 0.0005);
  const midtone = Math.min(Math.max(s.midtone, 0.0002), 0.9998);
  return `auto=false&black=${s.black.toFixed(6)}&white=${white.toFixed(6)}` +
    `&midtone=${midtone.toFixed(6)}&invert=${$('chkInvert').checked}`;
}

/* ----------------------------------------------------------- histogram */

function drawHistogram(stats) {
  const canvas = $('histogram');
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 280;
  const height = 90;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  if (!stats || !stats.histogram) return;
  const bins = stats.histogram;
  const plotHeight = height - 12;

  // Both axes need compressing. Sky background occupies a sliver at the bottom
  // of a linear ADU axis, and outnumbers the stars by orders of magnitude, so
  // the x axis gets a fourth-root scale and the counts get a log scale.
  const xOf = (v01) => Math.pow(Math.max(0, Math.min(1, v01)), 0.25) * width;
  const scaled = bins.map((v) => Math.log10(1 + v));
  const peak = Math.max(...scaled) || 1;

  ctx.fillStyle = '#5b8dd9';
  for (let i = 0; i < bins.length; i += 1) {
    if (!bins[i]) continue;
    const x0 = xOf(i / bins.length);
    const x1 = xOf((i + 1) / bins.length);
    const h = (scaled[i] / peak) * plotHeight;
    ctx.fillRect(x0, plotHeight - h, Math.max(x1 - x0, 0.8), h);
  }

  // Black / white point markers, on the same compressed axis.
  const s = currentStretch();
  ctx.lineWidth = 1;
  for (const [position, colour] of [[s.black, '#d94f3d'], [s.white, '#7b8698']]) {
    const x = xOf(position);
    ctx.strokeStyle = colour;
    ctx.beginPath();
    ctx.moveTo(x + 0.5, 0);
    ctx.lineTo(x + 0.5, plotHeight);
    ctx.stroke();
  }

  ctx.fillStyle = '#5c6577';
  ctx.font = '9px ui-monospace, monospace';
  ctx.textAlign = 'center';
  for (const adu of [0, 100, 1000, 10000, 65535]) {
    const x = xOf(adu / 65535);
    ctx.fillRect(x, plotHeight, 1, 3);
    const label = adu >= 1000 ? `${Math.round(adu / 1000)}k` : String(adu);
    ctx.fillText(label, Math.min(width - 9, Math.max(9, x)), height - 1);
  }
  ctx.textAlign = 'left';
}

/* ------------------------------------------------------------- viewer */

const viewer = {
  canvas: null,
  ctx: null,
  preview: null,        // downsampled Image covering the whole frame
  detail: null,         // { img, x, y, w, h } full-resolution crop
  width: 0,
  height: 0,
  scale: 1,
  tx: 0,
  ty: 0,
  dragging: false,
  detailTimer: null,
  detailToken: 0,
  cursor: null,
  // A frame can arrive while the Image tab is hidden — a centring solve started
  // from the planetarium does exactly that. The canvas then measures 0×0 and
  // there is no sensible scale to fit to, so the fit is deferred rather than
  // computed as zero and kept forever.
  needsFit: false,
};

function initViewer() {
  viewer.canvas = $('view');
  viewer.ctx = viewer.canvas.getContext('2d');

  const wrap = $('canvasWrap');
  // Fires when the tab becomes visible and the wrap goes from 0×0 to real, which
  // is the moment a deferred fit can finally be worked out.
  new ResizeObserver(() => repaintViewer()).observe(wrap);

  wrap.addEventListener('wheel', (event) => {
    if (!viewer.preview) return;
    event.preventDefault();
    const rect = wrap.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    const factor = Math.exp(-event.deltaY * 0.0016);
    zoomAt(px, py, factor);
  }, { passive: false });

  wrap.addEventListener('mousedown', (event) => {
    if (!viewer.preview) return;
    viewer.dragging = true;
    viewer.lastX = event.clientX;
    viewer.lastY = event.clientY;
    wrap.classList.add('panning');
  });

  window.addEventListener('mouseup', () => {
    if (!viewer.dragging) return;
    viewer.dragging = false;
    $('canvasWrap').classList.remove('panning');
    scheduleDetail();
  });

  wrap.addEventListener('mousemove', (event) => {
    const rect = wrap.getBoundingClientRect();
    if (viewer.dragging) {
      viewer.tx += event.clientX - viewer.lastX;
      viewer.ty += event.clientY - viewer.lastY;
      viewer.lastX = event.clientX;
      viewer.lastY = event.clientY;
      draw();
    }
    viewer.cursor = { px: event.clientX - rect.left, py: event.clientY - rect.top };
    updateCursorReadout();
    if ($('chkCrosshair').checked) draw();
  });

  wrap.addEventListener('mouseleave', () => {
    viewer.cursor = null;
    setText('cursorReadout', '—');
    if ($('chkCrosshair').checked) draw();
  });

  $('btnFit').addEventListener('click', () => { fitView(); scheduleDetail(); });
  $('btnActual').addEventListener('click', () => { setZoom(1); });
  $('btnZoomIn').addEventListener('click', () => zoomAt(centreX(), centreY(), 1.35));
  $('btnZoomOut').addEventListener('click', () => zoomAt(centreX(), centreY(), 1 / 1.35));
  $('chkCrosshair').addEventListener('change', draw);
}

const centreX = () => $('canvasWrap').clientWidth / 2;
const centreY = () => $('canvasWrap').clientHeight / 2;

function resizeCanvas() {
  const wrap = $('canvasWrap');
  const ratio = window.devicePixelRatio || 1;
  viewer.canvas.width = Math.max(1, Math.round(wrap.clientWidth * ratio));
  viewer.canvas.height = Math.max(1, Math.round(wrap.clientHeight * ratio));
  viewer.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
}

function fitView() {
  const wrap = $('canvasWrap');
  if (!viewer.width || !viewer.height) return;
  // Hidden tab: the wrap has no size yet, so fitting now would set the scale to
  // zero and the frame would draw at nothing once the tab came back. Remember
  // that a fit is owed and do it when there is a box to fit into.
  if (!wrap.clientWidth || !wrap.clientHeight) {
    viewer.needsFit = true;
    return;
  }
  const scale = Math.min(wrap.clientWidth / viewer.width, wrap.clientHeight / viewer.height) * 0.98;
  viewer.scale = scale;
  viewer.tx = (wrap.clientWidth - viewer.width * scale) / 2;
  viewer.ty = (wrap.clientHeight - viewer.height * scale) / 2;
  viewer.needsFit = false;
  draw();
}

/** Re-measure the canvas and paint, taking a deferred fit first if one is owed. */
function repaintViewer() {
  resizeCanvas();
  if (viewer.needsFit) fitView(); else draw();
}

function setZoom(scale) {
  zoomAt(centreX(), centreY(), scale / viewer.scale);
}

function zoomAt(px, py, factor) {
  if (!viewer.preview) return;
  const next = Math.max(0.02, Math.min(16, viewer.scale * factor));
  // Keep the image point under the cursor fixed.
  viewer.tx = px - (px - viewer.tx) * (next / viewer.scale);
  viewer.ty = py - (py - viewer.ty) * (next / viewer.scale);
  viewer.scale = next;
  draw();
  scheduleDetail();
}

function draw() {
  const ctx = viewer.ctx;
  if (!ctx) return;
  const wrap = $('canvasWrap');
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  ctx.clearRect(0, 0, w, h);
  if (!viewer.preview) { setText('zoomLabel', '—'); return; }

  ctx.imageSmoothingEnabled = viewer.scale < 1;
  ctx.drawImage(viewer.preview, viewer.tx, viewer.ty,
    viewer.width * viewer.scale, viewer.height * viewer.scale);

  // The 1:1 crop, when we have one for the visible area, draws sharply on top.
  const detail = viewer.detail;
  if (detail && viewer.scale >= 0.9) {
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(detail.img,
      viewer.tx + detail.x * viewer.scale,
      viewer.ty + detail.y * viewer.scale,
      detail.w * viewer.scale, detail.h * viewer.scale);
  }

  if ($('chkCrosshair').checked && viewer.cursor) {
    ctx.strokeStyle = 'rgba(217, 79, 61, 0.6)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(viewer.cursor.px + 0.5, 0);
    ctx.lineTo(viewer.cursor.px + 0.5, h);
    ctx.moveTo(0, viewer.cursor.py + 0.5);
    ctx.lineTo(w, viewer.cursor.py + 0.5);
    ctx.stroke();
  }

  setText('zoomLabel', `${(viewer.scale * 100).toFixed(0)}%`);
}

function screenToImage(px, py) {
  return {
    x: Math.floor((px - viewer.tx) / viewer.scale),
    y: Math.floor((py - viewer.ty) / viewer.scale),
  };
}

let pixelTimer = null;
function updateCursorReadout() {
  if (!viewer.preview || !viewer.cursor || !state.currentId) return;
  const { x, y } = screenToImage(viewer.cursor.px, viewer.cursor.py);
  if (x < 0 || y < 0 || x >= viewer.width || y >= viewer.height) {
    setText('cursorReadout', '—');
    return;
  }
  setText('cursorReadout', `x ${x}  y ${y}`);
  clearTimeout(pixelTimer);
  pixelTimer = setTimeout(async () => {
    try {
      const result = await api(`/api/images/${state.currentId}/pixel?x=${x}&y=${y}`);
      setText('cursorReadout', `x ${x}  y ${y}   ${result.value} ADU   (5×5 mean ${result.boxMean})`);
    } catch { /* the pointer moved on; ignore */ }
  }, 110);
}

/** Ask the server for a full-resolution crop of whatever is on screen. */
function scheduleDetail() {
  clearTimeout(viewer.detailTimer);
  viewer.detailTimer = setTimeout(loadDetail, 220);
}

async function loadDetail() {
  if (!state.currentId || !viewer.preview) return;
  if (viewer.scale < 0.9 || viewer.previewFactor <= 1) { viewer.detail = null; draw(); return; }

  const wrap = $('canvasWrap');
  const topLeft = screenToImage(0, 0);
  const bottomRight = screenToImage(wrap.clientWidth, wrap.clientHeight);
  const x = Math.max(0, topLeft.x - 8);
  const y = Math.max(0, topLeft.y - 8);
  const w = Math.min(viewer.width - x, bottomRight.x - x + 16);
  const h = Math.min(viewer.height - y, bottomRight.y - y + 16);
  if (w < 2 || h < 2 || w * h > 16e6) { viewer.detail = null; draw(); return; }

  const token = ++viewer.detailToken;
  const url = `/api/images/${state.currentId}/render.png?${stretchQuery()}` +
    `&region=${x},${y},${Math.ceil(w)},${Math.ceil(h)}`;
  const img = new Image();
  img.onload = () => {
    if (token !== viewer.detailToken) return;
    viewer.detail = { img, x, y, w: Math.ceil(w), h: Math.ceil(h) };
    draw();
  };
  img.src = url;
}

async function selectImage(imageId, keepView = false) {
  if (!imageId) return;
  let payload;
  try {
    payload = await api(`/api/images/${imageId}/stats`);
  } catch (error) {
    toast(error.message, 'error');
    return;
  }
  const record = payload.image;
  const sameGeometry = viewer.width === record.width && viewer.height === record.height;
  state.currentId = imageId;
  state.currentRecord = record;
  state.currentStats = payload.stats;

  if ($('chkAuto').checked) setStretchSliders(payload.autoStretch);
  drawHistogram(payload.stats);

  document.querySelectorAll('.image-row').forEach((row) => {
    row.classList.toggle('active', row.dataset.imageId === imageId);
  });
  setText('viewerTitle', record.filename + (record.saved ? '' : '   (not saved)'));
  const download = $('btnDownload');
  download.href = record.saved ? `/api/images/${imageId}/download` : '#';
  download.classList.toggle('disabled', !record.saved);

  await loadPreview(keepView && sameGeometry);
}

function loadPreview(keepView) {
  return new Promise((resolve) => {
    if (!state.currentId) { resolve(); return; }
    const record = state.currentRecord;
    $('viewerLoading').hidden = false;
    const url = `/api/images/${state.currentId}/render.png?${stretchQuery()}&maxDim=1600`;
    const img = new Image();
    img.onload = () => {
      viewer.preview = img;
      viewer.width = record.width;
      viewer.height = record.height;
      viewer.previewFactor = Math.max(1, Math.round(record.width / img.naturalWidth));
      viewer.detail = null;
      $('viewerEmpty').style.display = 'none';
      $('viewerLoading').hidden = true;
      resizeCanvas();
      // `keepView` holds the pan and zoom across frames of the same size, but
      // only once there is a real view to hold: a scale of zero is what a fit
      // against a hidden canvas leaves behind, and keeping that shows nothing.
      if (!keepView || viewer.needsFit || !(viewer.scale > 0)) fitView(); else draw();
      scheduleDetail();
      resolve();
    };
    img.onerror = () => {
      $('viewerLoading').hidden = true;
      toast('Could not render the image', 'error');
      resolve();
    };
    img.src = url;
  });
}

let restretchTimer = null;
function restretch() {
  clearTimeout(restretchTimer);
  restretchTimer = setTimeout(() => {
    if (state.currentId) loadPreview(true);
    if (state.currentRecord) drawHistogram(state.currentStats);
  }, 160);
}

/* ------------------------------------------------------------ live link */

function connectSocket() {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${protocol}://${location.host}/ws`);
  socket.onopen = () => {
    $('linkState').textContent = 'live';
    $('linkState').className = 'link-state online';
  };
  socket.onmessage = (event) => {
    try { applyStatus(JSON.parse(event.data)); } catch (error) { console.error(error); }
  };
  socket.onclose = () => {
    $('linkState').textContent = 'offline';
    $('linkState').className = 'link-state offline';
    setTimeout(connectSocket, 1500);
  };
  socket.onerror = () => socket.close();
}

/* --------------------------------------------------- equipment dialog */

async function openSettings() {
  await loadSettings();
  if (!$('settingsDialog').open) $('settingsDialog').showModal();
}

function openEquipment() {
  const dialog = $('equipmentDialog');
  if (!dialog.open) {
    dialog.showModal();
    loadEquipment().then(buildConnectRows).then(refreshDrivers);
    loadOverheads();
  }
}

function closeEquipment() {
  const dialog = $('equipmentDialog');
  if (dialog.open) dialog.close();
}

/** The native menu bar calls these through pywebview. */
window.openEquipment = openEquipment;
window.closeEquipment = closeEquipment;
window.toggleEquipment = () => ($('equipmentDialog').open ? closeEquipment() : openEquipment());

/* ------------------------------------------------------------- wiring */

function captureBody(loop) {
  return {
    exposure: Number($('exposure').value),
    frameType: $('frameType').value,
    binning: Number($('binning').value) || 1,
    gain: Number($('gain').value),
    offset: Number($('offset').value),
    loop,
  };
}

/* Adding, naming and connecting telescopes, and the profiles that remember
   whole rigs of them. */
function bindTelescopes() {
  const selected = () => state.rig || masterRigId();
  const named = () => {
    const rig = (state.equipment && state.equipment.telescopes
      .find((r) => r.id === selected()));
    return rig ? rig.name : 'this telescope';
  };

  $('btnRigAdd').addEventListener('click', async () => {
    const name = await askForText('Name for the new telescope',
      `Telescope ${((state.equipment && state.equipment.telescopes) || []).length + 1}`,
      { title: 'Add telescope', confirmLabel: 'Add' });
    if (name === null) return;
    const result = await send('/api/equipment/telescopes', 'POST', { name });
    state.rig = (result.telescopes[result.telescopes.length - 1] || {}).id;
    await afterEquipmentChange(result);
  });

  $('btnRigRename').addEventListener('click', async () => {
    const name = await askForText('Telescope name', named(), { title: 'Rename' });
    if (name === null || !name.trim()) return;
    await afterEquipmentChange(
      await send(`/api/equipment/telescopes/${selected()}`, 'POST', { name }));
  });

  $('btnRigMaster').addEventListener('click', async () => {
    const ok = await confirmAction(
      `Give ${named()} the mount and the guider? Every other telescope will `
      + 'follow its pointing.', { title: 'Make master', confirmLabel: 'Make master' });
    if (!ok) return;
    await afterEquipmentChange(
      await send(`/api/equipment/telescopes/${selected()}`, 'POST', { master: true }));
  });

  $('btnRigRemove').addEventListener('click', async () => {
    const ok = await confirmAction(
      `Remove ${named()}? Its devices are disconnected and its settings are lost `
      + '— save a profile first if you want it back.',
      { title: 'Remove telescope', confirmLabel: 'Remove', danger: true });
    if (!ok) return;
    await afterEquipmentChange(
      await send(`/api/equipment/telescopes/${selected()}`, 'DELETE'));
  });

  $('btnRigConnect').addEventListener('click', async () => {
    const result = await send(`/api/equipment/telescopes/${selected()}/connect`, 'POST');
    const count = result.connected.length;
    toast(count ? `Connected ${result.connected.join(', ')}`
      : 'Nothing remembered for this telescope yet', count ? 'success' : 'info');
    await loadEquipment();
  });

  $('btnConnectAll').addEventListener('click', async () => {
    const button = $('btnConnectAll');
    button.disabled = true;
    button.textContent = 'Connecting…';
    try {
      // Can take a while: PHD2 may have to be started, and an ASCOM camera
      // takes its time waking up.
      const result = await send('/api/equipment/connect', 'POST');
      if (!result.connected && !result.failed) {
        toast('Nothing is remembered yet — connect each device once and it '
          + 'will come back on its own', 'info', 6000);
      } else {
        toast(`Connected ${result.connected} device(s)`
          + (result.failed ? `, ${result.failed} failed — see the log` : ''),
        result.failed ? 'warn' : 'success');
      }
      await loadEquipment();
    } finally {
      button.disabled = false;
      button.textContent = 'Connect all';
    }
  });

  $('btnProfileSave').addEventListener('click', async () => {
    const active = (state.equipment && state.equipment.activeProfile) || {};
    const name = await askForText(
      'Name this equipment profile. Saving over a name that already exists '
      + 'replaces it.', active.name || '', { title: 'Save profile' });
    if (name === null || !name.trim()) return;
    // The form first, then the profile. A profile is a snapshot of what is
    // *stored*, and this button used to take that snapshot without storing
    // what was on screen — so filter names typed a minute earlier went into
    // neither, and the reload that followed wiped them off the form as well.
    // Somebody who names a profile means "keep all of this", not "keep the
    // version from before I started typing".
    const saved = await saveEquipmentSettings();
    if (saved === false) return;
    await afterEquipmentChange(
      await send('/api/equipment/profiles', 'POST', { name }, 'Profile saved'));
  });

  $('btnNinaImport').addEventListener('click', openNinaImport);
  $('ninaProfile').addEventListener('change', previewNina);
  $('btnNinaApply').addEventListener('click', applyNina);

  $('btnProfileLoad').addEventListener('click', async () => {
    const id = $('profileSelect').value;
    if (!id) return;
    const label = $('profileSelect').selectedOptions[0].textContent;
    const ok = await confirmAction(
      `Load ${label}? Everything is disconnected first, and the telescopes and `
      + 'settings are replaced by the ones the profile remembers.',
      { title: 'Load profile', confirmLabel: 'Load' });
    if (!ok) return;
    await afterEquipmentChange(
      await send(`/api/equipment/profiles/${id}/load?connect=false`, 'POST'));
    toast('Profile loaded — press Connect this telescope to bring it up', 'success');
  });

  $('btnProfileDelete').addEventListener('click', async () => {
    const id = $('profileSelect').value;
    if (!id) return;
    const ok = await confirmAction(
      `Delete ${$('profileSelect').selectedOptions[0].textContent}?`,
      { title: 'Delete profile', confirmLabel: 'Delete', danger: true });
    if (!ok) return;
    await afterEquipmentChange(await send(`/api/equipment/profiles/${id}`, 'DELETE'));
  });

  $('btnFocusAll').addEventListener('click', async () => {
    closeEquipment();
    await startFocusRun('/api/focus/run?all=true');
  });
}

function bindControls() {
  $('btnRefreshDrivers').addEventListener('click', () => refreshDrivers(true).then(
    () => toast('Driver list refreshed', 'success')));
  $('btnScanAlpaca').addEventListener('click', async () => {
    toast('Scanning the network for Alpaca devices…');
    try {
      const result = await api('/api/alpaca/scan', 'POST', {});
      await refreshDrivers();
      toast(`Found ${result.found} Alpaca device(s)`, result.found ? 'success' : 'info');
    } catch (error) { toast(error.message, 'error'); }
  });
  $('btnDisconnectAll').addEventListener('click', async () => {
    await send('/api/equipment/disconnect', 'POST');
    state.cameraSynced = false;
    state.filterNames = null;
    await loadEquipment();
  });

  bindTelescopes();

  // The banner's own controls, so a run can be paused without hunting for the
  // Plan tab.
  $('seqBannerPause').addEventListener('click', () => {
    const sequence = (state.status && state.status.sequence) || {};
    send(sequence.paused ? '/api/sequence/resume' : '/api/sequence/pause', 'POST');
  });
  $('seqBannerStop').addEventListener('click', async () => {
    const ok = await confirmAction(
      'Stop the sequence? The current frame finishes first, and the mount is '
      + 'left tracking where it is.',
      { title: 'Stop sequence', confirmLabel: 'Stop', danger: true });
    if (ok) send('/api/sequence/stop', 'POST');
  });

  // Camera
  $('btnCapture').addEventListener('click', () => send(rigQuery('/api/camera/expose'), 'POST', captureBody(false)));
  $('btnLoop').addEventListener('click', () => {
    const rig = currentRig();
    const capture = (rig && rig.capture) || (state.status && state.status.capture);
    if (capture && capture.busy) send(rigQuery('/api/camera/loop'), 'POST', { on: !capture.loop });
    else send(rigQuery('/api/camera/expose'), 'POST', captureBody(true));
  });
  $('btnAbort').addEventListener('click', () => send(rigQuery('/api/camera/abort'), 'POST'));
  $('gain').addEventListener('input', (e) => setText('gainValue', e.target.value));
  $('offset').addEventListener('input', (e) => setText('offsetValue', e.target.value));
  $('gain').addEventListener('change', () => send(rigQuery('/api/camera/settings'), 'POST',
    { gain: Number($('gain').value) }));
  $('offset').addEventListener('change', () => send(rigQuery('/api/camera/settings'), 'POST',
    { offset: Number($('offset').value) }));
  $('binning').addEventListener('change', () => send(rigQuery('/api/camera/settings'), 'POST',
    { binning: Number($('binning').value) }));
  $('btnCooler').addEventListener('click', () => {
    const rig = currentRig();
    const camera = ((rig && rig.devices) || state.status.devices).camera;
    send(rigQuery('/api/camera/cooler'), 'POST', { on: !camera.coolerOn });
  });
  $('btnSetpoint').addEventListener('click', () => send(rigQuery('/api/camera/cooler'), 'POST',
    { setpoint: Number($('setpointInput').value) }));

  // Focuser
  document.querySelectorAll('[data-focus-step]').forEach((button) => {
    button.addEventListener('click', () => send(rigQuery('/api/focuser/move'), 'POST',
      { delta: Number(button.dataset.focusStep) }));
  });
  $('btnFocMove').addEventListener('click', () => {
    const value = Number($('focTargetInput').value);
    if (Number.isNaN(value)) { toast('Enter a focuser position', 'error'); return; }
    send(rigQuery('/api/focuser/move'), 'POST', { position: Math.round(value) });
  });
  $('btnFocHalt').addEventListener('click', () => send(rigQuery('/api/focuser/halt'), 'POST'));
  // Focusing from the Focuser panel, where you are when you notice you need it.
  // No confirmation: the button is right next to the focuser readout, it says
  // what it does, and the floating window shows the sweep as it happens.
  $('btnAutofocus').addEventListener('click', () => startFocusRun(rigQuery('/api/focus/run')));
  $('btnAutofocusAll').addEventListener('click', () => startFocusRun('/api/focus/run?all=true'));
  $('btnAutofocusAbort').addEventListener('click', () =>
    send('/api/focus/abort?all=true', 'POST', null, 'Stopping the focus run'));

  // Mount
  $('btnSlew').addEventListener('click', () => send('/api/mount/slew', 'POST',
    { ra: Number($('targetRa').value), dec: Number($('targetDec').value) }));
  $('btnSync').addEventListener('click', () => send('/api/mount/sync', 'POST',
    { ra: Number($('targetRa').value), dec: Number($('targetDec').value) }));
  $('btnAbortSlew').addEventListener('click', () => send('/api/mount/abort', 'POST'));
  $('btnMountStop').addEventListener('click', () => send('/api/mount/abort', 'POST'));
  $('btnTracking').addEventListener('click', () => {
    const mount = state.status.devices.mount;
    send('/api/mount/tracking', 'POST', { on: !mount.tracking });
  });
  $('btnPark').addEventListener('click', () => send('/api/mount/park', 'POST'));
  $('btnUnpark').addEventListener('click', () => send('/api/mount/unpark', 'POST'));

  document.querySelectorAll('[data-jog]').forEach((button) => {
    const start = (event) => {
      event.preventDefault();
      send('/api/mount/jog', 'POST',
        { direction: button.dataset.jog, rate: Number($('jogRate').value) });
    };
    const stop = () => send('/api/mount/jog/stop', 'POST').catch(() => {});
    button.addEventListener('mousedown', start);
    button.addEventListener('mouseup', stop);
    button.addEventListener('mouseleave', (event) => { if (event.buttons) stop(); });
    button.addEventListener('touchstart', start, { passive: false });
    button.addEventListener('touchend', stop);
  });

  // Saving
  $('chkSave').addEventListener('change', () => {
    pushOutput({ save: $('chkSave').checked })
      .catch(() => { $('chkSave').checked = !$('chkSave').checked; });
  });
  $('saveDir').addEventListener('change', () => {
    pushOutput({ directory: $('saveDir').value }).catch(() => {});
  });
  $('btnBrowseDir').addEventListener('click', async () => {
    try {
      const chosen = await choosePath('pick_folder',
        { title: 'Save frames to', start: $('saveDir').value || '' });
      if (chosen) {
        $('saveDir').value = chosen;
        await pushOutput({ directory: chosen });
      }
    } catch (error) { toast(String(error), 'error'); }
  });
  const applyTarget = () => pushOutput({ target: $('targetName').value })
    .then(() => updateFilePreview(state.status && state.status.capture))
    .catch(() => {});
  $('btnSetTarget').addEventListener('click', applyTarget);
  $('targetName').addEventListener('change', applyTarget);
  $('targetName').addEventListener('input', () => updateFilePreview(state.status && state.status.capture));
  $('exposure').addEventListener('input', () => updateFilePreview(state.status && state.status.capture));
  $('frameType').addEventListener('change', () => updateFilePreview(state.status && state.status.capture));

  // Guiding
  const settle = () => ({
    settlePixels: Number($('settlePixels').value) || 1.5,
    settleTime: Number($('settleTime').value) || 0,
    settleTimeout: 60,
  });
  $('btnGuide').addEventListener('click', () => send('/api/guider/guide', 'POST', settle()));
  $('btnGuideStop').addEventListener('click', () => send('/api/guider/stop', 'POST'));
  $('btnDither').addEventListener('click', () => send('/api/guider/dither', 'POST', {
    pixels: Number($('ditherPixels').value) || 3,
    raOnly: $('chkRaOnly').checked,
    ...settle(),
  }));

  // Plate solving
  // A solve measures the selected telescope's own frame and field. Syncing and
  // centring move the one mount, so those only ever go to the master.
  $('btnSolve').addEventListener('click', () => send(rigQuery('/api/solve'), 'POST', { mode: 'solve' }));
  $('btnSolveSync').addEventListener('click', () => send('/api/solve', 'POST', { mode: 'sync' }));
  $('btnSolveCenter').addEventListener('click', () => send('/api/solve', 'POST', { mode: 'center' }));
  $('btnSolveAbort').addEventListener('click', () => send(rigQuery('/api/solve/abort'), 'POST'));

  // The roof. Opening is confirmed because it is the one control in the program
  // that exposes the telescope to the sky, and closing because it is the one
  // that can come down on it.
  $('btnRoofOpen').addEventListener('click', async () => {
    const ok = await confirmAction(
      'Open the roof? Check nothing is in the way of the shutter.',
      { title: 'Open roof', confirmLabel: 'Open', danger: true });
    if (ok) send('/api/dome/shutter', 'POST', { open: true });
  });
  $('btnRoofClose').addEventListener('click', async () => {
    const ok = await confirmAction(
      'Close the roof? Make sure the telescope is parked clear of the shutter.',
      { title: 'Close roof', confirmLabel: 'Close', danger: true });
    if (ok) send('/api/dome/shutter', 'POST', { open: false });
  });
  $('btnOffsetsRun').addEventListener('click', async () => {
    const names = readFilterNames();
    if (names.length < 2) {
      toast('Name at least two of the wheel\'s slots first', 'error');
      return;
    }
    const passes = Number($('offPasses').value) || 2;
    const ok = await confirmAction(
      `Focus each of ${names.length} filters ${passes} time(s) and save the `
      + 'differences? That is one focus sweep per filter per pass, so it takes '
      + 'a while — and it needs stars, so do it under a dark sky.',
      { title: 'Measure filter offsets', confirmLabel: 'Measure' });
    if (!ok) return;
    try {
      await api(rigQuery('/api/focus/offsets/run'), 'POST',
        { filters: names, passes, reference: $('offReference').value });
    } catch (error) { toast(error.message, 'error'); }
  });
  $('btnOffsetsAbort').addEventListener('click', () =>
    send('/api/focus/offsets/abort', 'POST'));

  // Proving the settings before a night depends on them.
  $('btnNotifyTest').addEventListener('click', async () => {
    const button = $('btnNotifyTest');
    button.disabled = true;
    button.textContent = 'Sending…';
    try {
      await api('/api/notify/test', 'POST');
      toast('Test message sent — check it arrived', 'success');
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
      button.textContent = 'Send a test message';
      setTimeout(updateNotifyNote, 1500);
    }
  });

  $('btnDomeSlave').addEventListener('click', () => {
    const dome = ((state.status && state.status.devices) || {}).dome || {};
    send('/api/dome/slave', 'POST', { on: !dome.slaved });
  });
  $('btnSolveTarget').addEventListener('click', () => {
    const result = state.solveResult;
    if (!result) return;
    $('targetRa').value = result.ra.toFixed(6);
    $('targetDec').value = result.dec.toFixed(5);
    if (window.sky) window.sky.goto(result.ra, result.dec, 'Solved position');
    showTab('sky');
  });

  // Rotator
  document.querySelectorAll('[data-rotate-step]').forEach((button) => {
    button.addEventListener('click', () => send(rigQuery('/api/rotator/move'), 'POST',
      { delta: Number(button.dataset.rotateStep) }));
  });
  $('btnRotMove').addEventListener('click', () => {
    const value = Number($('rotTargetInput').value);
    if (Number.isNaN(value)) { toast('Enter a position angle', 'error'); return; }
    send(rigQuery('/api/rotator/move'), 'POST', { position: ((value % 360) + 360) % 360 });
  });
  $('btnRotHalt').addEventListener('click', () => send(rigQuery('/api/rotator/halt'), 'POST'));
  $('btnRotSync').addEventListener('click', async () => {
    const result = state.solveResult;
    if (!result) { toast('Plate solve a frame first', 'error'); return; }
    const ok = await confirmAction(
      `Tell the rotator it is at ${result.rotation.toFixed(2)}°? `
      + 'That is the angle the last plate solve measured.',
      { title: 'Sync rotator', confirmLabel: 'Sync' });
    if (!ok) return;
    send(rigQuery('/api/rotator/sync'), 'POST',
      { position: ((result.rotation % 360) + 360) % 360 },
      `Rotator synced to ${result.rotation.toFixed(2)}°`);
  });

  // Tabs and the panels the topbar calls up
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => showTab(tab.dataset.tab));
  });
  document.querySelectorAll('[data-panel-toggle]').forEach((button) => {
    button.addEventListener('click', () => togglePanel(button.dataset.panelToggle));
  });
  document.querySelectorAll('[data-device-toggle]').forEach((button) => {
    button.addEventListener('click', () => toggleDevicePanel(button.dataset.deviceToggle));
  });
  buildPrefNav('equipmentNav', 'equipmentBody');
  buildPrefNav('settingsNav', 'settingsBody');

  // Equipment dialog
  $('btnEquipment').addEventListener('click', openEquipment);
  $('warningsHead').addEventListener('click', () => {
    warningsView.open = !warningsView.open;
    if (state.status) updateWarnings(state.status);
  });
  $('btnSaveEquipment').addEventListener('click', (event) => {
    event.preventDefault();
    saveEquipmentSettings();
  });
  $('capRoot').addEventListener('input', updateCapturePreview);
  $('btnPhd2Browse').addEventListener('click', async () => {
    try {
      const chosen = await choosePath('pick_file',
        { title: 'Find phd2.exe', start: $('phd2Path').value || '', files: ['.exe'] });
      if (chosen) { $('phd2Path').value = chosen; refreshPhd2Note(); }
    } catch (error) { toast(String(error), 'error'); }
  });
  $('btnCapBrowse').addEventListener('click', async () => {
    try {
      const chosen = await choosePath('pick_folder',
        { title: 'Where frames are saved', start: $('capRoot').value || '' });
      if (chosen) { $('capRoot').value = chosen; updateCapturePreview(); }
    } catch (error) { toast(String(error), 'error'); }
  });
  bindFilterChips();
  bindDeviceSetup();

  // Measuring what the rig costs between exposures.
  $('btnCalibrateOverheads').addEventListener('click', async () => {
    const ok = await confirmAction(
      'Time the rig? It takes a few short frames, moves the filter wheel back '
      + 'and forth'
      + ($('calOverheadFocus').checked ? ', and runs one autofocus sweep' : '')
      + '. Point somewhere with stars in it first if the sweep is included.',
      { title: 'Calibrate overhead', confirmLabel: 'Measure' });
    if (!ok) return;
    try {
      await api(rigQuery('/api/overheads/calibrate'), 'POST',
        { frames: 3, focus: $('calOverheadFocus').checked });
    } catch (error) { toast(error.message, 'error'); return; }
    toast('Measuring — watch the log', 'info');
    // Poll while it runs; it takes about as long as one focus sweep.
    const watch = setInterval(async () => {
      const payload = await loadOverheads();
      if (payload && !payload.busy) {
        clearInterval(watch);
        toast('Overheads measured', 'success');
      }
    }, 2000);
  });
  $('btnClearOverheads').addEventListener('click', async () => {
    const ok = await confirmAction(
      'Forget every measurement and go back to the typed figures?',
      { title: 'Forget overheads', confirmLabel: 'Forget', danger: true });
    if (!ok) return;
    await api('/api/overheads', 'DELETE').catch((error) => toast(error.message, 'error'));
    loadOverheads();
  });
  $('schedUseMeasured').addEventListener('change', async () => {
    try {
      await api('/api/settings/schedule', 'POST',
        { useMeasured: $('schedUseMeasured').checked });
    } catch (error) { toast(error.message, 'error'); return; }
    loadOverheads();
  });
  $('btnOpticsAngle').addEventListener('click', () => {
    $('settingsDialog').close();
    openEquipment();
    openPrefPage('equipmentDialog', 'camera');
    const field = $('opticsAngle');
    if (field) { field.focus(); field.select(); }
  });
  // The recovery bar is a shortcut to the settings that govern it: something
  // has just gone wrong, and that is exactly when the thresholds get revisited.
  const recoveryBar = $('recoveryBar');
  if (recoveryBar) {
    recoveryBar.addEventListener('click', () => {
      openEquipment();
      openPrefPage('equipmentDialog', 'recovery');
    });
  }
  $('calRoot').addEventListener('input', updateCalibrationPreview);
  $('btnCalRootBrowse').addEventListener('click', async () => {
    try {
      const chosen = await choosePath('pick_folder',
        { title: 'Calibration library', start: $('calRoot').value || '' });
      if (chosen) { $('calRoot').value = chosen; updateCalibrationPreview(); }
    } catch (error) { toast(String(error), 'error'); }
  });
  $('btnFocusNow').addEventListener('click', async () => {
    const ok = await confirmAction(
      'Run a focus sweep now? It will move the focuser and take several exposures.',
      { title: 'Focus now', confirmLabel: 'Focus' });
    if (!ok) return;
    closeEquipment();
    await startFocusRun(rigQuery('/api/focus/run'));
  });

  // Settings dialog
  $('btnSettings').addEventListener('click', openSettings);
  $('btnSaveSettings').addEventListener('click', (event) => {
    event.preventDefault();
    saveSettings();
  });

  // Stretch
  const sliders = ['black', 'white', 'midtone'];
  for (const id of sliders) {
    $(id).addEventListener('input', () => {
      $('chkAuto').checked = false;
      updateStretchLabels();
      if (state.currentRecord) drawHistogram(state.currentStats);
      restretch();
    });
  }
  $('chkAuto').addEventListener('change', async () => {
    if ($('chkAuto').checked && state.currentId) {
      const payload = await api(`/api/images/${state.currentId}/stats`);
      setStretchSliders(payload.autoStretch);
    }
    restretch();
  });
  $('chkInvert').addEventListener('change', restretch);

  window.addEventListener('keydown', (event) => {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName)) return;
    if (event.key === 'e') window.toggleEquipment();
    if (event.key === 'p') showTab(state.tab === 'sky' ? 'image' : 'sky');
    if (state.tab !== 'image') return;
    if (event.key === 'f') { fitView(); scheduleDetail(); }
    if (event.key === '1') setZoom(1);
    if (event.key === ' ') { event.preventDefault(); $('btnCapture').click(); }
  });
}

function startClock() {
  const tick = () => {
    const now = new Date();
    const local = now.toLocaleTimeString([], { hour12: false });
    const utc = now.toISOString().slice(11, 19);
    setText('clock', `${local}  ·  ${utc} UTC`);
  };
  tick();
  setInterval(tick, 1000);
}

/* What the planetarium needs from here: the shared status, the API helpers and
   a way to hear about a new status without opening a second socket. */
window.astro = {
  $,
  state,
  api,
  send,
  toast,
  confirmAction,
  askForText,
  /** Browse…: the native dialog in the window, the page's own in a browser. */
  choosePath,
  fmtHours,
  fmtDegrees,
  showTab,
  loadSettings,
  /** The dialogs, for the first-light walk-through to open in turn. */
  openEquipment,
  openSettings,
  openNinaImport,
  /** Scope a path to the selected telescope, the way every device call does. */
  rigQuery,
  /** Re-render everything from the status already held. */
  refresh: () => { if (state.status) applyStatus(state.status); },
  onStatus: (listener) => statusListeners.push(listener),
  /** Called with the settings each time they are (re)loaded, after a save too. */
  onSettings: (listener) => settingsListeners.push(listener),
};

function init() {
  watchForScriptErrors();
  restorePanels();
  bindFocusFloatDrag();
  buildConnectRows();
  bindAsk();
  bindPick();
  bindControls();
  initViewer();
  applyOptionalTabs();
  applyPanelVisibility();
  applyDevicePanels();
  updateStretchLabels();
  startClock();
  // The telescope list first: which slots the connection rows show, and which
  // camera the panels drive, both depend on it.
  loadEquipment().then(() => {
    buildConnectRows();
    return refreshDrivers();
  });
  refreshImages();
  loadSettings();
  connectSocket();
}

document.addEventListener('DOMContentLoaded', init);
