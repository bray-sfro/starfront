/* First light: a walk from an empty program to a night of imaging.
 *
 * Eight steps, each one a real thing in the program rather than a copy of
 * it: the step explains what is about to happen and why, presses the button
 * that opens the actual dialog or tab, and then *watches* — the tick appears
 * when the camera really is connected, when a target really exists, when the
 * plan really has frames on it. Nothing here is a form of its own, so there
 * is nothing that can drift from the program it describes.
 *
 * Opens on its own the first time the program runs, and afterwards only from
 * the First light button, which keeps its bookmark: somebody who got as far as
 * framing a target and went to bed picks up at the plan.
 */
'use strict';

(function () {
  const app = window.astro;
  const $ = app.$;

  const wiz = {
    step: 0,
    open: false,
    targets: [],
    plan: null,
    collab: null,
    pairing: null,
    fetchedAt: 0,
    autoOpened: false,
  };

  /* ---------------------------------------------------------------- data */

  const settings = () => app.state.settings || {};
  const status = () => app.state.status || {};
  const device = (kind) => ((status().devices || {})[kind]) || {};
  /* Every telescope on the mount, master first; the master alone before the
     first status arrives. */
  const telescopes = () => {
    const all = (app.rigList && app.rigList()) || [];
    return all.length ? all : [{ id: 'main', name: 'this telescope', role: 'master',
      devices: status().devices || {} }];
  };
  const multiple = () => telescopes().length > 1;

  async function refresh(force = false) {
    if (!force && Date.now() - wiz.fetchedAt < 4000) return;
    wiz.fetchedAt = Date.now();
    const [targets, plan, collab] = await Promise.all([
      app.api('/api/targets').then((r) => r.targets || []).catch(() => wiz.targets),
      app.api('/api/plan').catch(() => wiz.plan),
      app.api('/api/collab').catch(() => wiz.collab),
    ]);
    wiz.targets = targets;
    wiz.plan = plan;
    wiz.collab = collab;
  }

  /* --------------------------------------------------------------- steps */

  /* Each step: a title, what it is for, the buttons that do it, and `check`
     - what is true once it is done, as a short line, or '' while it is not.
     A step with no check is done by reading it. */
  const STEPS = [
    {
      key: 'welcome',
      title: 'Welcome to Starfront',
      body: [
        'This walk-through takes you from an empty program to a night of imaging: '
        + 'connect the equipment, bring your settings over from N.I.N.A., frame a '
        + 'target, plan tonight, and join the collaboration.',
        'Every step opens the real part of the program and then watches for it to '
        + 'be done — the tick appears when the camera is really connected, when a '
        + 'target really exists. Press Later at any time; the First light button '
        + 'remembers where you were.',
      ],
      actions: [],
      check: () => 'ready',
    },
    {
      key: 'nina',
      title: 'Bring your settings over from N.I.N.A.',
      body: [
        'If you have used N.I.N.A. on this PC, its profiles already know your focal '
        + 'length, pixel size, filters and their offsets, focus and dither settings, '
        + 'ASTAP, and which camera is which. Import them and skip an hour of typing.',
        'More than one telescope on the mount? One N.I.N.A. profile per telescope is '
        + 'the one place that already records which camera is on which scope and '
        + 'which one has the mount — Set up every telescope reads all of them at '
        + 'once, makes a telescope for each and picks the master. Everything is '
        + 'shown before it is written. Coming from somewhere else? Skip this and '
        + 'fill in Site & Optics on the next step.',
      ],
      actions: [
        ['Set up every telescope from N.I.N.A.…', () => app.openNinaSetup(), true],
        ['Import one profile…', () => app.openNinaImport(), false],
        ['Type them in instead', () => app.openSettings(), false],
      ],
      check: () => {
        const optics = settings().optics || {};
        const camera = settings().camera || {};
        const have = [];
        if (multiple()) have.push(`${telescopes().length} telescopes: ${telescopes().map((t) => t.name).join(', ')}`);
        if (optics.focalLength) have.push(`${optics.focalLength} mm`);
        if (optics.sensorWidth && optics.sensorHeight) have.push(`${optics.sensorWidth}×${optics.sensorHeight} px`);
        if ((camera.filterNames || []).length) have.push(`filters ${camera.filterNames.join(' ')}`);
        return optics.focalLength && optics.pixelSize ? have.join(' · ') : '';
      },
    },
    {
      key: 'site',
      title: 'Where the telescope is',
      body: [
        'The planner draws the night for one place on Earth: what rises when, where '
        + 'the meridian is, when it gets dark. Latitude and longitude come from the '
        + 'mount if it knows them, or from the N.I.N.A. import, or you type them in.',
        'The same dialog holds the optics — focal length, sensor, pixel size — which '
        + 'is what every field of view and every mosaic is drawn from.',
      ],
      actions: [['Open Site & Optics…', () => app.openSettings(), true]],
      check: () => {
        const site = settings().site || {};
        if (site.latitude === null || site.latitude === undefined) return '';
        const lat = Number(site.latitude), lon = Number(site.longitude);
        return `${Math.abs(lat).toFixed(2)}° ${lat >= 0 ? 'N' : 'S'}, `
          + `${Math.abs(lon).toFixed(2)}° ${lon >= 0 ? 'E' : 'W'}`;
      },
    },
    {
      key: 'equipment',
      title: 'Connect the equipment',
      body: [
        'Pick a driver for each slot — camera, mount, filter wheel, focuser, '
        + 'guider — once, and it is remembered: on later nights one button brings '
        + 'the whole rig up. If you set up from N.I.N.A., the drivers are already '
        + 'chosen; just press Connect everything.',
        'A camera and the mount are enough to go on with; the rest can wait. If '
        + 'N.I.N.A. is still running, close it first — a camera can only be held '
        + 'by one program.',
      ],
      actions: [['Open Equipment…', () => app.openEquipment(), true]],
      check: () => {
        const lines = telescopes().map((rig) => {
          const on = ['camera', 'mount', 'filterwheel', 'focuser', 'guider', 'flatpanel', 'rotator']
            .filter((kind) => ((rig.devices || {})[kind] || {}).connected);
          return on.length ? `${rig.name}: ${on.join(', ')}` : '';
        }).filter(Boolean);
        const cameras = telescopes().every((rig) => ((rig.devices || {}).camera || {}).connected);
        return cameras && device('mount').connected ? lines.join(' · ') : '';
      },
      partial: () => {
        const lines = telescopes().map((rig) => {
          const on = ['camera', 'mount', 'filterwheel', 'focuser', 'guider', 'flatpanel', 'rotator']
            .filter((kind) => ((rig.devices || {})[kind] || {}).connected);
          return on.length ? `${rig.name}: ${on.join(', ')}` : '';
        }).filter(Boolean);
        return lines.length ? `so far — ${lines.join(' · ')}` : '';
      },
    },
    {
      key: 'frame',
      title: 'Frame a target',
      body: [
        'The Planner shows the sky with your camera’s field drawn on it. Search for '
        + 'an object, drag the frame, turn it, and make it a mosaic only if the '
        + 'object is bigger than the sensor — then Save target.',
        'Save as many as you like; the list on the right is your target list.',
      ],
      actions: [['Open the Planner', () => app.showTab('planner'), true]],
      check: () => {
        const own = wiz.targets.filter((t) => t.type !== 'allsky');
        return own.length ? `${own.length} target${own.length === 1 ? '' : 's'} saved — `
          + own.slice(0, 3).map((t) => t.name).join(', ') + (own.length > 3 ? '…' : '') : '';
      },
    },
    {
      key: 'plan',
      title: 'Plan tonight',
      body: [
        'The Plan tab is tonight: drag targets into it, give each one its filters '
        + 'and frame counts, and see on the graph when each is up. Auto-arrange '
        + 'orders the night for you.',
        'Run sequence starts it; Run on loop runs it every night, opening at '
        + 'dusk and parking at dawn.',
      ],
      actions: [['Open the Plan', () => app.showTab('plan'), true]],
      check: () => {
        const entries = ((wiz.plan || {}).plan || {}).entries || [];
        const framed = entries.filter((e) => (e.filters || []).some((f) => f.count > 0)
          || e.kind === 'calibration');
        return framed.length
          ? (framed.length === 1 ? '1 entry with frames on it'
            : `${framed.length} entries with frames on them`) : '';
      },
      partial: () => {
        const entries = ((wiz.plan || {}).plan || {}).entries || [];
        return entries.length ? `${entries.length} in the plan, none with frames yet` : '';
      },
    },
    {
      key: 'join',
      title: 'Join the collaboration',
      body: [
        'A collaboration is a target many telescopes shoot together — usually one '
        + 'object everybody points at, sometimes a mosaic too big for one of them. '
        + 'The server tells each telescope what to shoot each night and keeps '
        + 'count of the depth for all of you.',
        'One button: Join with Discord. It signs you in and enrols this telescope. '
        + 'You need to be a member of the Starfront Discord server.',
      ],
      actions: [['Open Collab', () => app.showTab('collab'), true]],
      check: () => {
        const c = wiz.collab || {};
        const person = c.person || {};
        if (!(person.signedIn || person.admin) || !c.enrolled) return '';
        return `joined as ${person.name || 'you'} — ${c.telescope || 'this telescope'} is enrolled`;
      },
    },
    {
      key: 'collab',
      title: 'Take part in one, or start your own',
      body: [
        'Every active collaboration is listed on the Collab tab with its patch of '
        + 'sky drawn on the survey. Take part tonight puts it in your plan; the '
        + 'server chooses the filters and how deep, and the Plan tab shows what '
        + 'your telescope will shoot.',
        'To start your own, frame the target in the Planner and say what you '
        + 'will accept — filters, focal length, star size.',
      ],
      actions: [
        ['Take part in one', () => app.showTab('collab'), true],
        ['Start one', () => {
          app.showTab('collab');
          const fold = $('colStartFold');
          if (fold) { fold.open = true; fold.scrollIntoView({ block: 'start' }); }
        }, false],
      ],
      check: () => {
        const c = wiz.collab || {};
        const open = c.open || [];
        const joined = (c.tasks || []).length;
        const person = c.person || {};
        const mine = open.filter((p) => person.id && p.ownerId === person.id).length;
        if (joined) return `taking part in ${joined}`;
        if (mine) return `started ${mine}`;
        return '';
      },
      partial: () => {
        const open = (wiz.collab || {}).open || [];
        return open.length ? `${open.length} running on the server` : 'nothing running on the server yet';
      },
    },
    {
      key: 'done',
      title: 'First light',
      body: [
        'That is the whole program. From here on a night is: connect the rig, '
        + 'check the plan, Run sequence. The collaboration checks in every ten '
        + 'minutes on its own; the log under your Starfront folder says what '
        + 'happened while you slept.',
        'The full manual is README.md beside the program. Clear skies.',
      ],
      actions: [['Open the Plan', () => app.showTab('plan'), true]],
      check: () => 'done',
    },
  ];

  /* --------------------------------------------------------------- render */

  function render() {
    const step = STEPS[wiz.step];
    $('wizWhere').textContent = `step ${wiz.step + 1} of ${STEPS.length}`;
    $('wizTitle').textContent = step.title;

    const dots = $('wizSteps');
    dots.innerHTML = '';
    STEPS.forEach((s, index) => {
      const dot = document.createElement('button');
      dot.type = 'button';
      dot.className = 'wizard-dot'
        + (index === wiz.step ? ' current' : '')
        + (index !== wiz.step && s.check() ? ' done' : '');
      dot.title = s.title;
      dot.textContent = String(index + 1);
      dot.addEventListener('click', () => go(index));
      dots.appendChild(dot);
    });

    const body = $('wizBody');
    body.innerHTML = '';
    for (const text of step.body) {
      const p = document.createElement('p');
      p.textContent = text;
      body.appendChild(p);
    }

    const actions = $('wizActions');
    actions.innerHTML = '';
    for (const [label, run, primary] of step.actions) {
      const button = document.createElement('button');
      button.className = `btn small${primary ? ' primary' : ''}`;
      button.textContent = label;
      button.addEventListener('click', async () => {
        // The real thing opens on top of, or behind, this window. Behind is
        // a tab, and a modal in front of it would hide it - so the wizard
        // steps aside and keeps its place.
        await remember();
        $('wizardDialog').close();
        try { await run(); } catch (error) { app.toast(error.message, 'error'); }
      });
      actions.appendChild(button);
    }

    paintState();
    $('wizBack').disabled = wiz.step === 0;
    $('wizSkip').hidden = wiz.step === 0 || wiz.step === STEPS.length - 1 || !!step.check();
    $('wizNext').textContent = wiz.step === STEPS.length - 1 ? 'Finish' : 'Next';
  }

  function paintState() {
    const step = STEPS[wiz.step];
    const node = $('wizState');
    const done = step.check();
    if (step.key === 'welcome' || step.key === 'done') {
      node.hidden = true;
      return;
    }
    node.hidden = false;
    if (done) {
      node.className = 'wizard-state done';
      node.textContent = `✓ ${done}`;
    } else {
      const partial = step.partial ? step.partial() : '';
      node.className = 'wizard-state waiting';
      node.textContent = partial ? `… ${partial}` : '… not yet';
    }
  }

  /* --------------------------------------------------------- navigation */

  async function remember(extra = {}) {
    try {
      const answer = await app.api('/api/settings/meta', 'POST', { firstLightStep: wiz.step, ...extra });
      if (app.state.settings && answer && answer.meta) app.state.settings.meta = answer.meta;
    } catch (error) { /* a bookmark that cannot be saved is not worth a toast */ }
    paintButton();
  }

  function go(index) {
    wiz.step = Math.max(0, Math.min(STEPS.length - 1, index));
    render();
    remember();
  }

  async function open(force = false) {
    await app.loadSettings();
    await refresh(true);
    const meta = settings().meta || {};
    if (!force) wiz.step = Math.max(0, Math.min(STEPS.length - 1, Number(meta.firstLightStep) || 0));
    $('wizNoMore').checked = !!meta.firstLightDone;
    render();
    const dialog = $('wizardDialog');
    if (!dialog.open) dialog.showModal();
    wiz.open = true;
  }

  function paintButton() {
    const button = $('btnFirstLight');
    if (!button) return;
    const meta = settings().meta || {};
    const step = Number(meta.firstLightStep) || 0;
    if (meta.firstLightDone) {
      button.textContent = 'First light…';
      button.classList.remove('in-progress');
    } else {
      button.textContent = step > 0 ? `First light · ${step + 1}/${STEPS.length}` : 'First light…';
      button.classList.add('in-progress');
    }
  }

  function bind() {
    const on = (id, event, handler) => { const n = $(id); if (n) n.addEventListener(event, handler); };
    on('btnFirstLight', 'click', () => open(false));
    on('wizBack', 'click', () => go(wiz.step - 1));
    on('wizSkip', 'click', () => go(wiz.step + 1));
    on('wizNext', 'click', async () => {
      if (wiz.step === STEPS.length - 1) {
        await remember({ firstLightDone: true, firstLightStep: 0 });
        $('wizardDialog').close();
        return;
      }
      go(wiz.step + 1);
    });
    on('wizNoMore', 'change', () => remember({ firstLightDone: $('wizNoMore').checked }));
    const dialog = $('wizardDialog');
    if (dialog) {
      dialog.addEventListener('close', () => { wiz.open = false; paintButton(); });
    }
  }

  /* Watching: while the wizard is open the tick lines follow the program.
     While it is closed, the button in the bar keeps its bookmark. The first
     run opens it on its own, once settings and status have both arrived so
     the ticks are honest from the first frame. */
  app.onStatus(() => {
    if (wiz.open) {
      refresh().then(paintState).catch(() => {});
      paintState();
      return;
    }
    if (!wiz.autoOpened && app.state.settings && app.state.settings.meta) {
      wiz.autoOpened = true;
      paintButton();
      if (!app.state.settings.meta.firstLightDone) {
        setTimeout(() => open(false), 800);
      }
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
