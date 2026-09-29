/* The Stack tab: watching the picture arrive.
 *
 * One job, and it is nearly all presentation — the pipeline does the work on
 * its own thread as frames are calibrated, and this asks it three questions:
 * what is being stacked, what does the chosen one hold, and what does it look
 * like now.
 *
 * Two things here are less obvious than they look.
 *
 * The preview is fetched with a cache-buster on every tick, because it is the
 * one picture in this program that is *expected* to be different every time
 * anybody looks at it, and a browser that helpfully served the cached one
 * would make a working stack look frozen.
 *
 * And the picture is only swapped in once the new one has decoded. Pointing an
 * <img> at a new URL blanks it while it loads, so refreshing every few seconds
 * would flicker between the stack and nothing at all — which reads as
 * something going wrong rather than as something arriving.
 */
'use strict';

(function () {
  const app = window.astro;
  const $ = app.$;

  const view = {
    status: null,
    chosen: '',        // "project/filter"
    loading: false,
    settingsSynced: false,
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

  /* --------------------------------------------------------------- facts */

  function drawFacts(summary) {
    const table = $('stackFacts');
    if (!table) return;
    if (!summary) {
      table.innerHTML = '<tr><td class="muted">nothing chosen</td></tr>';
      return;
    }
    const plan = summary.plan || {};
    const rows = [
      ['Frames', summary.frames],
      ['Integration', duration(summary.seconds)],
      ['Telescopes', (summary.agents || []).join(', ') || '—'],
      ['Filters', (summary.filters || []).join(', ') || '—'],
      ['Canvas', `${plan.pixelWidth || '?'} x ${plan.pixelHeight || '?'} at `
        + `${Number(plan.actualScale || 0).toFixed(2)}"/px`],
      ['Covered', `${Math.round((summary.covered || 0) * 100)}% of the canvas`],
      ['Deepest', `${summary.maxFramesDeep || 0} frames, `
        + duration(summary.deepestSeconds)],
      ['Typical depth', duration(summary.medianSeconds)],
      ['Rejected', `${summary.rejectedPixels || 0} pixels (satellites, rays)`],
      ['Reference', `${summary.referenceStars || 0} stars`],
    ];
    table.innerHTML = rows.map(([name, value]) =>
      `<tr><th>${esc(name)}</th><td>${esc(value)}</td></tr>`).join('');
  }

  function drawLast(status) {
    const box = $('stackLast');
    if (!box) return;
    const last = status && status.last;
    if (!last) { box.textContent = 'nothing yet'; return; }
    if (last.error) {
      box.innerHTML = `<span class="bad">${esc(last.path)}</span> — `
        + esc(last.error);
      return;
    }
    const placing = last.placing || {};
    const stack = last.stack || {};
    const bits = [`<b>${esc(last.path)}</b>`];
    if (placing.detail) bits.push(esc(placing.detail));
    if (stack.detail) bits.push(esc(stack.detail));
    // Whether it left the building. A rig that is stacking beautifully and
    // sending nothing is a thing somebody wants to know about tonight.
    bits.push(last.sent ? 'sent to the collaboration'
      : 'held locally (not shared, or waiting for the server)');
    box.innerHTML = bits.join('<br>');
  }

  /* ---------------------------------------------------------- the picture */

  let pending = null;

  function drawPicture(key, summary) {
    const image = $('stackImage');
    const empty = $('stackEmpty');
    if (!image || !empty) return;
    if (!key || !summary || !summary.frames) {
      image.hidden = true;
      empty.hidden = false;
      return;
    }
    const [project, filterName] = key.split('/');
    const url = `/api/livestack/${encodeURIComponent(project)}`
      + `/${encodeURIComponent(filterName)}/preview.png`
      + `?maxDim=1400&t=${summary.updated || Date.now()}`;
    if (image.dataset.url === url) return;

    // Decode first, swap second. Pointing the element straight at the new URL
    // blanks it while it loads, and a stack that blinks out every few seconds
    // reads as a fault rather than as an arrival.
    const next = new Image();
    pending = next;
    next.onload = () => {
      if (pending !== next) return;             // a newer one has started
      image.src = next.src;
      image.dataset.url = url;
      image.hidden = false;
      empty.hidden = true;
    };
    next.onerror = () => {
      if (pending !== next) return;
      image.hidden = true;
      empty.hidden = false;
      empty.textContent = 'The stack could not be drawn.';
    };
    next.src = url;
  }

  /* ----------------------------------------------------------- the picker */

  function drawPick(status) {
    const pick = $('stackPick');
    if (!pick) return;
    const keys = Object.keys(status.stacks || {}).sort();
    const same = pick.dataset.keys === keys.join('|');
    if (!same) {
      pick.dataset.keys = keys.join('|');
      pick.innerHTML = keys.length
        ? keys.map((key) => {
          const summary = status.stacks[key] || {};
          const [project, filterName] = key.split('/');
          return `<option value="${esc(key)}">${esc(project)} — `
            + `${esc(filterName)} (${summary.frames || 0} frames)</option>`;
        }).join('')
        : '<option value="">nothing yet</option>';
    }
    if (!view.chosen || !keys.includes(view.chosen)) view.chosen = keys[0] || '';
    if (pick.value !== view.chosen) pick.value = view.chosen;

    const pill = $('stackPill');
    const text = $('stackPillText');
    if (pill && text) {
      const running = status.running && status.enabled;
      const held = status.held || 0;
      pill.classList.toggle('ok', Boolean(running && !held));
      pill.classList.toggle('warn', Boolean(held));
      text.textContent = !status.enabled ? 'switched off'
        : !keys.length ? 'nothing stacking'
          : held ? `${keys.length} stacking, ${held} tiles waiting to send`
            : `${keys.length} stacking, ${status.pending || 0} queued`;
    }
  }

  /* -------------------------------------------------------------- loading */

  async function load() {
    if (view.loading) return;
    view.loading = true;
    try {
      const status = await app.api('/api/livestack');
      view.status = status;
      drawPick(status);
      const summary = (status.stacks || {})[view.chosen];
      drawFacts(summary);
      drawLast(status);
      drawPicture(view.chosen, summary);
    } catch (error) {
      console.error(error);
    } finally {
      view.loading = false;
    }
  }

  /* ------------------------------------------------------------- settings */

  function syncSettings(settings) {
    // Once, and never again while the tab is open: rewriting the boxes on
    // every tick would undo a number somebody is halfway through typing.
    if (view.settingsSynced || !settings || !settings.livestack) return;
    view.settingsSynced = true;
    const live = settings.livestack;
    if ($('stackEnabled')) $('stackEnabled').checked = live.enabled !== false;
    if ($('stackOwn')) $('stackOwn').checked = live.stackOwnTargets !== false;
    if ($('stackShare')) $('stackShare').checked = live.share !== false;
    if ($('stackGradient')) {
      $('stackGradient').checked = live.removeGradient !== false;
    }
    if ($('stackMaxPixels')) $('stackMaxPixels').value = live.maxPixels || 4096;
  }

  async function saveSettings() {
    const values = {
      enabled: $('stackEnabled') ? $('stackEnabled').checked : true,
      stackOwnTargets: $('stackOwn') ? $('stackOwn').checked : true,
      share: $('stackShare') ? $('stackShare').checked : true,
      removeGradient: $('stackGradient') ? $('stackGradient').checked : true,
      maxPixels: Number($('stackMaxPixels') && $('stackMaxPixels').value) || 4096,
    };
    try {
      await app.api('/api/settings/livestack', 'POST', values);
      app.toast('Live stack settings saved');
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
    on('stackPick', 'change', () => {
      view.chosen = $('stackPick').value;
      load();
    });
    on('btnStackRefresh', 'click', load);
    on('btnStackSave', 'click', saveSettings);
  }

  let here = false;
  app.onStatus((status, tab) => {
    syncSettings(app.state.settings);
    if (tab !== 'stack') { here = false; return; }
    if (!here) { here = true; load(); return; }
    // While the tab is open, follow the stack. The status tick is a couple of
    // times a second and a canvas render is not free, so this rides one in
    // every eight of them — a frame lands every few minutes, so anything
    // faster is work nobody asked for.
    const follow = $('stackLive');
    if (follow && !follow.checked) return;
    view.ticks = (view.ticks || 0) + 1;
    if (view.ticks % 8 === 0) load();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
