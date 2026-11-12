// Starfront fleet dashboard. Everything shown comes from /api/fleet, which the
// dashboard rebuilds from what each PC last said; nothing here is kept.
(function () {
  'use strict';
  const POLL_MS = 2000;
  const params = new URLSearchParams(location.search);
  if (params.get('token')) localStorage.setItem('fleetDashboardToken', params.get('token'));
  let token = localStorage.getItem('fleetDashboardToken') || '';
  let last = null;
  const seenAlerts = new Set();
  let firstAlerts = true;
  const local = {};          // "node|rig|action" -> {state, detail} until the server has a record
  const $ = (id) => document.getElementById(id);

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const headers = () => (token ? { 'X-Dashboard-Token': token } : {});
  const withToken = (url) => (token ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token) : url);

  function ago(seconds) {
    if (seconds == null) return '';
    if (seconds < 60) return Math.round(seconds) + 's';
    if (seconds < 3600) return Math.round(seconds / 60) + ' min';
    return (seconds / 3600).toFixed(1) + ' h';
  }
  function clock(t) {
    if (!t) return '';
    return new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
  function hms(hours, sign) {
    if (hours == null) return '—';
    const neg = hours < 0; let v = Math.abs(hours);
    const a = Math.floor(v); v = (v - a) * 60; const b = Math.floor(v); const c = Math.round((v - b) * 60);
    const pad = (n) => String(n).padStart(2, '0');
    return (neg ? '−' : (sign ? '+' : '')) + pad(a) + (sign ? '°' : 'h') + pad(b) + (sign ? '′' : 'm') + pad(c % 60) + (sign ? '″' : 's');
  }
  function pier(side) {
    const s = String(side).toLowerCase();
    if (s.includes('east') || s === '0') return 'east (pointing west)';
    if (s.includes('west') || s === '1') return 'west (pointing east)';
    return s === '-1' ? 'unknown' : String(side);
  }
  const ROLE = { master: 'mount + guider', slave: 'rides the same mount' };
  function freshLabel(f) {
    if (!f) return '';
    if (f.state === 'live') return 'live';
    if (f.state === 'stale') return 'stale ' + ago(f.age);
    if (f.state === 'connecting') return 'connecting';
    return 'offline' + (f.age != null ? ' ' + ago(f.age) : '');
  }

  function summary(s) {
    const item = (cls, n, label) => `<span class="count ${cls}"><b>${n}</b>${label}</span>`;
    $('summary').innerHTML = item('imaging', s.imaging, 'imaging') + item('idle', s.idle, 'idle') +
      item('busy', s.busy, 'busy / paused') + item('recovering', s.recovering, 'recovering') +
      item('offline', s.offline, 'offline');
  }

  function alerts(list, now) {
    $('alerts').innerHTML = list.map((a) =>
      `<div class="alert ${esc(a.level)}" data-alert="${esc(a.id)}"><b>${a.level === 'critical' ? '⚠' : '•'}</b>` +
      `<span>${esc(a.text)}</span><span class="since">since ${clock(a.since)} (${ago(now - a.since)})</span></div>`).join('');
    for (const a of list) {
      if (!seenAlerts.has(a.id)) {
        seenAlerts.add(a.id);
        if (!firstAlerts && window.Notification && Notification.permission === 'granted' &&
            localStorage.getItem('fleetNotify') === '1') {
          try { new Notification('Starfront fleet', { body: a.text, tag: a.id }); } catch (e) { /* ignore */ }
        }
      }
    }
    firstAlerts = false;
  }

  function cmdLines(commands, node, rig) {
    const mine = commands.filter((c) => c.node === node && (rig === undefined || c.rig === rig))
      .slice(-3).reverse();
    const lines = mine.map((c) => `<div class="cmd ${esc(c.state)}" data-cmd="${c.id}">` +
      `${esc(c.action)}: ${esc(c.state)}${c.detail && c.detail !== c.state ? ' — ' + esc(c.detail) : ''}</div>`);
    for (const [key, v] of Object.entries(local)) {
      const [n, r] = key.split('|');
      if (n === node && (rig === undefined ? r === '' : r === (rig || ''))) {
        lines.unshift(`<div class="cmd ${esc(v.state)}">${esc(v.action)}: ${esc(v.state)}${v.detail ? ' — ' + esc(v.detail) : ''}</div>`);
      }
    }
    return lines.length ? `<div class="cmds">${lines.join('')}</div>` : '';
  }

  function button(node, action, label, opts) {
    opts = opts || {};
    return `<button class="btn small ${opts.cls || ''}" data-node="${esc(node)}" data-action="${action}"` +
      `${opts.rig ? ` data-rig="${esc(opts.rig)}"` : ''}${opts.disabled ? ' disabled' : ''}>${label}</button>`;
  }

  function card(c, commands, now) {
    const live = c.fresh && c.fresh.state === 'live';
    if (!c.rigId) {
      return `<div class="card offline" data-card="${esc(c.node)}|"><div class="top"><h3>${esc(c.name)}</h3>` +
        `<span class="state offline">offline</span></div>` +
        `<div class="error-line">${esc((c.fresh && c.fresh.error) || 'no answer yet')}</div>` +
        `<div class="role">Its telescopes appear here once it answers.</div></div>`;
    }
    const f = [];
    const m = c.mount;
    if (m) {
      f.push(['Mount', m.ra != null ? `${esc(m.state)}${m.shared ? ' (shared)' : ''}` : esc(m.state)]);
      if (m.ra != null) f.push(['RA / Dec', `${hms(m.ra)}  ${hms(m.dec, true)}`]);
      if (m.pierSide != null) f.push(['Pier side', esc(pier(m.pierSide))]);
    }
    const s = c.sequence;
    if (s) {
      f.push(['Target', esc(s.target || '—')]);
      const step = [];
      if (s.panels > 1) step.push(`panel ${s.panel}/${s.panels}`);
      if (s.slots) step.push(`step ${s.slot}/${s.slots}`);
      if (s.frames) step.push(`frame ${s.frame || 0} of ${s.frames}`);
      f.push(['Plan', esc(step.join(' · ') || s.state || '')]);
      if (s.message) f.push(['Doing', esc(s.message)]);
    }
    f.push(['Filter', esc(c.filter || '—')]);
    if (c.focuser) f.push(['Focuser', `${esc(c.focuser.position)}${c.focuser.temperature != null ? ` · ${c.focuser.temperature}°C` : ''}`]);
    if (c.camera) {
      const cam = c.camera;
      f.push(['Camera', cam.connected ? `${cam.temperature != null ? cam.temperature + '°C' : '—'}` +
        `${cam.setpoint != null ? ' → ' + cam.setpoint + '°C' : ''} · cooler ${cam.coolerOn ? 'on' : 'off'}` +
        `${cam.coolerPower != null ? ' ' + cam.coolerPower + '%' : ''}${cam.exposing ? ' · exposing' : ''}` : 'not connected']);
    }
    if (c.guiding) f.push(['Guiding', `${c.guiding.rms != null ? c.guiding.rms + '"' : (c.guiding.rmsPx != null ? c.guiding.rmsPx + ' px' : '—')} RMS · ${esc(c.guiding.state || '')}`]);
    const lf = c.lastFrame;
    const thumb = lf ? `<div class="thumb"><img alt="last frame" loading="lazy" src="${esc(withToken(lf.thumb))}">` +
      `<span>${esc(lf.filter || '')} ${lf.exposure ? esc(lf.exposure) + 's' : ''} · ${clock(lf.time)}</span></div>`
      : '<div class="thumb"><div class="none">no frame yet</div></div>';
    const r = c.recovery;
    const rec = r ? `<div class="recovery ${esc(r.state)}">${r.state === 'failed' ? 'Recovery failed' : 'Recovering'}` +
      `${r.attempts ? `, attempt ${r.attempt}/${r.attempts}` : ''}${r.reason ? ': ' + esc(r.reason) : ''}` +
      `${r.step ? ` (${esc(r.step)})` : ''}</div>` : '';
    const warns = c.warnings && c.warnings.length
      ? `<div class="warns">${c.warnings.map((w) => `<span class="${esc(w.level)}">${esc(w.title)}</span>`).join(' ')}</div>` : '';
    const master = m && !m.shared;
    const parked = m && m.state === 'parked';
    const ctl = [button(c.node, 'camera.abort', 'Abort exposure', { rig: c.rigId, cls: 'danger', disabled: !live })];
    if (master) {
      ctl.push(button(c.node, 'mount.park', 'Park', { rig: c.rigId, disabled: !live || parked }));
      ctl.push(button(c.node, 'mount.unpark', 'Unpark…', { rig: c.rigId, disabled: !live || !parked }));
      ctl.push(button(c.node, 'mount.slew', 'Slew…', { rig: c.rigId, disabled: !live || parked }));
    }
    return `<div class="card ${esc(c.state)}" data-card="${esc(c.node)}|${esc(c.rigId)}">` +
      `<div class="top"><h3>${esc(c.name)}</h3><span class="role">${esc(ROLE[c.role] || c.role || '')}</span>` +
      `<span class="state ${esc(c.state)}">${esc(c.state)}</span></div>` + rec +
      `<div class="body"><dl class="facts">${f.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>${thumb}</div>` +
      warns + `<div class="controls">${ctl.join('')}</div>` + cmdLines(commands, c.node, c.rigId) + '</div>';
  }

  function nodeSection(n, cards, commands, now) {
    const seq = n.sequence;
    const live = n.state === 'live';
    const running = seq && seq.running;
    const paused = seq && seq.paused;
    const ctl = [
      button(n.name, 'sequence.start', 'Start', { cls: 'go', disabled: !live || running }),
      paused ? button(n.name, 'sequence.resume', 'Resume', { disabled: !live })
        : button(n.name, 'sequence.pause', 'Pause', { disabled: !live || !running }),
      button(n.name, 'sequence.stop', 'Stop', { cls: 'danger', disabled: !live || !running }),
    ];
    const warn = (n.warnings || []).length
      ? `<div class="node-warn">${n.warnings.map((w) => `<span class="${esc(w.level)}">${esc(w.title)}</span>`).join('')}</div>` : '';
    const seqLine = seq ? (seq.running ? (seq.paused ? 'run paused' : `running — ${esc(seq.message || seq.state || '')}`)
      : (seq.error ? `<span class="error-line">${esc(seq.error)}</span>` : 'no run')) : '';
    return `<section class="node" data-node="${esc(n.name)}"><div class="node-head">` +
      `<h2>${esc(n.name)}</h2><span class="fresh ${esc(n.state)}">${esc(freshLabel(n))}</span>` +
      `<span class="meta">${esc(n.host)}:${n.port}${n.version ? ' · Starfront ' + esc(n.version) : ''}` +
      `${n.latency != null && live ? ' · ' + Math.round(n.latency * 1000) + ' ms' : ''}${n.note ? ' · ' + esc(n.note) : ''}</span>` +
      `<span class="meta">${seqLine}</span>` +
      (n.error ? `<span class="error-line">${esc(n.error)}</span>` : '') +
      `<div class="ctl">${ctl.join('')}</div></div>` + warn +
      (cmdLines(commands, n.name, null) ? `<div style="padding:6px 12px">${cmdLines(commands, n.name, null)}</div>` : '') +
      `<div class="cards">${cards.map((c) => card(c, commands, now)).join('')}</div></section>`;
  }

  function render(data) {
    last = data;
    summary(data.summary);
    alerts(data.alerts, data.time);
    // A command the server has a record of no longer needs its local placeholder.
    $('nodes').innerHTML = data.nodes.map((n) =>
      nodeSection(n, data.cards.filter((c) => c.node === n.name), data.commands, data.time)).join('');
    $('updated').textContent = 'updated ' + clock(data.time);
  }

  async function poll() {
    try {
      const res = await fetch('/api/fleet', { headers: headers(), cache: 'no-store' });
      if (res.status === 403) { $('tokenbar').hidden = false; $('updated').textContent = 'needs the dashboard token'; return; }
      render(await res.json());
    } catch (e) {
      $('updated').textContent = 'dashboard not answering';
    }
  }

  async function send(node, action, rig, extra) {
    const key = `${node}|${rig || ''}|${action}`;
    local[key] = { action, state: 'sending', detail: '' };
    if (last) render(last);
    try {
      const res = await fetch(`/api/nodes/${encodeURIComponent(node)}/command`, {
        method: 'POST', headers: Object.assign({ 'Content-Type': 'application/json' }, headers()),
        body: JSON.stringify(Object.assign({ action, rig: rig || null }, extra || {})),
      });
      const data = await res.json().catch(() => ({}));
      delete local[key];
      if (!res.ok) {
        local[key] = { action, state: 'failed', detail: data.detail || res.statusText };
        setTimeout(() => { delete local[key]; }, 15000);
      }
    } catch (e) {
      local[key] = { action, state: 'failed', detail: 'the dashboard did not answer' };
    }
    poll();
  }

  document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-action]');
    if (!btn || btn.disabled) return;
    const { node, action } = btn.dataset;
    const rig = btn.dataset.rig || null;
    const sendRig = action === 'camera.abort' ? rig : (action.startsWith('mount.') ? rig : null);
    if (action === 'mount.unpark') {
      if (!window.confirm(`Unpark the mount on ${node}? It will start moving and tracking.`)) return;
      send(node, action, sendRig, { confirm: true });
    } else if (action === 'mount.slew') {
      const answer = window.prompt(`Slew the mount on ${node} to (RA hours, Dec degrees), e.g. "5.5881, -5.39":`);
      if (!answer) return;
      const [ra, dec] = answer.split(/[ ,]+/).filter(Boolean).map(Number);
      if (!isFinite(ra) || !isFinite(dec)) { window.alert('Give RA in hours and Dec in degrees.'); return; }
      if (!window.confirm(`Slew ${node} to RA ${ra} h, Dec ${dec}°? Nobody may be watching the telescope.`)) return;
      send(node, action, sendRig, { confirm: true, ra, dec });
    } else if (action === 'sequence.stop') {
      if (!window.confirm(`Stop the run on ${node}?`)) return;
      send(node, action, null);
    } else {
      send(node, action, sendRig);
    }
  });

  function notifyLabel() {
    const on = localStorage.getItem('fleetNotify') === '1' && window.Notification && Notification.permission === 'granted';
    $('notify-btn').textContent = on ? 'Notifications on' : 'Notifications off';
  }
  $('notify-btn').addEventListener('click', async () => {
    if (!window.Notification) { window.alert('This browser has no notifications.'); return; }
    if (localStorage.getItem('fleetNotify') === '1') { localStorage.setItem('fleetNotify', '0'); notifyLabel(); return; }
    const p = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
    localStorage.setItem('fleetNotify', p === 'granted' ? '1' : '0');
    notifyLabel();
  });
  $('quit-btn').addEventListener('click', async () => {
    if (!window.confirm('Stop the dashboard? (The telescopes carry on.)')) return;
    await fetch('/api/quit', { method: 'POST', headers: headers() }).catch(() => null);
    $('updated').textContent = 'dashboard stopped';
  });
  $('token-save').addEventListener('click', () => {
    token = $('token-input').value.trim();
    localStorage.setItem('fleetDashboardToken', token);
    $('tokenbar').hidden = true;
    poll();
  });

  window.fleetPage = { poll, render, send, get last() { return last; } };
  notifyLabel();
  poll();
  setInterval(poll, POLL_MS);
})();
