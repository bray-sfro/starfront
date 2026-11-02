/* The live stack on the Image tab: tonight's subs, calibrated and added up.
 *
 * The pipeline does the work on its own thread as frames land - calibrate,
 * place on the sky, resample onto the canvas, fold in. This file only decides
 * what the viewer shows. One button swaps the viewer between the single frame
 * it always showed and the stack, and everything else about the viewer -
 * stretch, histogram, pan, zoom, the sharp crop at the screen's resolution,
 * the pixel readout, Save FITS - works on the stack because app.js asks
 * `viewUrl()` where to fetch from rather than assuming a frame.
 *
 * Two things here are less obvious than they look.
 *
 * The stack is redrawn only when the status says it has grown, and never
 * while a redraw is still in flight. A status tick is a couple of times a
 * second; a sub lands every few minutes; redrawing a 3000-pixel canvas on
 * every tick would be the machine's whole job.
 *
 * And a new frame landing does not pull the viewer off the stack - app.js
 * checks `stackView.on` before following the camera. Picking a frame from the
 * session list, or stepping with the arrow keys, does leave the stack, because
 * that is what "show me that sub" means; the button brings it back.
 */
'use strict';

(function () {
  const app = window.astro;
  const $ = app.$;

  const view = {
    chosen: '',          // "project/filter" of the stack on show or last shown
    loading: false,
    settingsSynced: false,
    lastKeys: '',
  };

  const esc = (text) => String(text === null || text === undefined ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  function duration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    if (total < 60) return `${total}s`;
    const hours = Math.floor(total / 3600);
    const minutes = Math.round((total - hours * 3600) / 60);
    return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
  }

  /** The selected telescope's live-stack slice of the status. */
  function mine() {
    const rig = (app.rigList() || []).find((one) => one.id === app.state.rig)
      || (app.rigList() || []).find((one) => one.role === 'master')
      || (app.rigList() || [])[0];
    return (rig && rig.livestack) || (app.state.status && app.state.status.livestack) || null;
  }

  const rigId = () => app.state.rig || app.masterRigId();

  function split(key) {
    // The filter is the part after the last slash: a target name may hold
    // slashes of its own and the filter never does.
    const at = key.lastIndexOf('/');
    return at < 0 ? [key, 'none'] : [key.slice(0, at), key.slice(at + 1)];
  }

  /* ------------------------------------------------------------ the picker */

  function drawPick(status) {
    const pick = $('stackPick');
    if (!pick) return;
    const stacks = (status && status.stacks) || {};
    const keys = Object.keys(stacks).sort();
    const signature = keys.map((key) => `${key}:${(stacks[key] || {}).frames || 0}`).join('|');
    if (signature !== view.lastKeys) {
      view.lastKeys = signature;
      pick.innerHTML = keys.map((key) => {
        const [project, filterName] = split(key);
        const brief = stacks[key] || {};
        return `<option value="${esc(key)}">${esc(project)} · ${esc(filterName)} `
          + `(${brief.frames || 0})</option>`;
      }).join('');
    }
    // The stack the newest frame went into is the one to show unless the
    // operator picked another one tonight.
    if (!view.chosen || !keys.includes(view.chosen)) {
      view.chosen = (status && status.current && keys.includes(status.current))
        ? status.current : (keys[0] || '');
    }
    if (pick.value !== view.chosen) pick.value = view.chosen;
    pick.hidden = keys.length < 2;
  }

  /* ------------------------------------------------------------- the line */

  function drawInfo(status) {
    const line = $('stackInfo');
    const button = $('btnLiveStack');
    const reset = $('btnStackReset');
    if (!line || !button) return;
    const stacks = (status && status.stacks) || {};
    const brief = stacks[view.chosen];
    const on = app.stackView.on;

    button.classList.toggle('active', on);
    button.disabled = !brief && !on;
    if (reset) reset.hidden = !on;

    if (!status || !status.enabled) {
      line.textContent = on ? 'live stack is switched off in Settings › Files' : '';
      line.classList.remove('raw');
      return;
    }
    if (!brief) {
      line.textContent = on ? 'nothing stacked yet tonight' : '';
      line.classList.remove('raw');
      return;
    }
    const raw = (brief.frames || 0) - (brief.calibrated || 0);
    const bits = [`live stack ${brief.frames} frame${brief.frames === 1 ? '' : 's'}`,
      duration(brief.seconds)];
    if (raw > 0) bits.push(`${raw} raw (no matching masters)`);
    if (status.pending) bits.push(`${status.pending} waiting`);
    const last = status.last || {};
    if (on && last.error) bits.push(`last: ${last.error}`);
    line.textContent = bits.join(' · ') + (on ? '' : '   (s)');
    line.classList.toggle('raw', raw > 0);
  }

  /* ----------------------------------------------------------- the picture */

  async function show(keepView) {
    if (!view.chosen || view.loading) return;
    const [project, filterName] = split(view.chosen);
    view.loading = true;
    try {
      const shown = await app.selectStack(rigId(), project, filterName, keepView);
      if (!shown) leave();
    } finally {
      view.loading = false;
    }
  }

  function leave() {
    app.stackView.on = false;
    const button = $('btnLiveStack');
    if (button) button.classList.remove('active');
    const reset = $('btnStackReset');
    if (reset) reset.hidden = true;
    drawInfo(mine());
  }

  async function toggle() {
    if (app.stackView.on) {
      // Back to the frames: the newest one, following the camera again.
      leave();
      const images = app.state.images || [];
      if (images.length) {
        app.state.browsing = false;
        app.selectImage(images[0].id, true);
      }
      return;
    }
    const status = mine();
    drawPick(status);
    if (!view.chosen) {
      app.toast(status && status.enabled
        ? 'Nothing stacked yet tonight - take a light frame first'
        : 'The live stack is switched off in Settings › Files', 'error');
      return;
    }
    await show(false);
    drawInfo(status);
  }

  async function restart() {
    if (!view.chosen) return;
    const [project, filterName] = split(view.chosen);
    const ok = await app.confirmAction(
      `Throw away tonight's stack of ${project} (${filterName}) and start it again `
      + 'from the next frame? The subs on disk are untouched.');
    if (!ok) return;
    try {
      let path = `/api/livestack/reset?project=${encodeURIComponent(project)}`
        + `&filter=${encodeURIComponent(filterName)}`;
      path = app.rigQuery(path);
      await app.api(path, 'POST');
      app.toast('The stack will start again with the next frame');
      leave();
    } catch (error) {
      app.toast(String(error && error.message || error), 'error');
    }
  }

  /* ------------------------------------------------------------- settings */

  function syncSettings(settings) {
    // Once, so a number somebody is halfway through typing is not rewritten.
    if (view.settingsSynced || !settings || !settings.livestack) return;
    view.settingsSynced = true;
    const live = settings.livestack;
    const set = (id, value) => { if ($(id)) $(id).checked = value; };
    set('stackEnabled', live.enabled !== false);
    set('stackOwn', live.stackOwnTargets !== false);
    set('stackShare', live.share !== false);
    set('stackGradient', live.removeGradient !== false);
    set('stackRaw', live.stackUncalibrated !== false);
    // Zero is a value: the camera's own resolution.
    if ($('stackMaxPixels')) $('stackMaxPixels').value = Number(live.maxPixels) || 0;
  }

  async function saveSettings() {
    const get = (id, fallback) => ($(id) ? $(id).checked : fallback);
    const values = {
      enabled: get('stackEnabled', true),
      stackOwnTargets: get('stackOwn', true),
      share: get('stackShare', true),
      removeGradient: get('stackGradient', true),
      stackUncalibrated: get('stackRaw', true),
      maxPixels: Number($('stackMaxPixels') && $('stackMaxPixels').value) || 0,
    };
    try {
      await app.api('/api/settings/livestack', 'POST', values);
      app.toast('Live stack settings saved');
      view.settingsSynced = false;
      app.loadSettings();
    } catch (error) {
      app.toast(String(error && error.message || error), 'error');
    }
  }

  /* ---------------------------------------------------------------- wiring */

  function bind() {
    const on = (id, event, handler) => {
      const element = $(id);
      if (element) element.addEventListener(event, handler);
    };
    on('btnLiveStack', 'click', toggle);
    on('btnStackReset', 'click', restart);
    on('btnStackSave', 'click', saveSettings);
    on('stackPick', 'change', () => {
      view.chosen = $('stackPick').value;
      if (app.stackView.on) show(false);
    });
    app.leaveStack = leave;
  }

  let lastRig = null;
  app.onStatus(() => {
    syncSettings(app.state.settings);
    const status = mine();
    // Switching telescope switches stacks: each camera sees its own field.
    if (lastRig !== app.state.rig) {
      lastRig = app.state.rig;
      view.chosen = '';
      view.lastKeys = '';
      if (app.stackView.on) leave();
    }
    drawPick(status);
    drawInfo(status);
    if (!app.stackView.on || !status) return;
    const brief = (status.stacks || {})[view.chosen];
    if (!brief) return;
    // Redraw when the stack has grown since the picture on screen was made,
    // holding the pan and zoom: watching one corner sharpen as the subs come
    // in is the point of a live stack.
    if ((brief.updated || 0) > (app.stackView.updated || 0)) show(true);
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
