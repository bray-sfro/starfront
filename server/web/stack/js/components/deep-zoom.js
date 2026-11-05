// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// "Explore full resolution" on the public project page (MOSAIC_PLAN.md phase 6, contract 1.18.0): an OpenSeadragon viewer of the stream's
// Deep Zoom pyramid. Shown only for streams whose status entry has `deep_zoom`; for the rest, and when the viewer cannot start, the page's
// 2048 px previews are what there is, and the section says so. Its list also offers "Preview", the auto-stretched combined (colour) preview,
// opened as one image in the same viewer, when the section is shown and the project has one.
//
//   * Lazy. OpenSeadragon (vendored, same origin) is requested when the visitor presses the button, not before.
//   * The page's own controls. Zoom in, zoom out, fit and full screen are real buttons with names; OpenSeadragon's image buttons and its
//     built-in keyboard handling of "r", "f" and "j" (rotate, full page, flip) are off, so there is nothing to sight-read and no way to turn north away.
//   * Survives the page's refresh. The project page repaints its body whenever status.json changes; the viewer's element is kept and put back, and
//     when the stream's pyramid is a newer gold's the viewer opens it at the same place.
//   * A missing tile is blank. The worker does not write a tile with no data, so the edge answers its HTML 404 page for it; OpenSeadragon's complaint
//     about it is counted, not shown.

import { html } from "../dom.js";
import { streamLabel } from "../format.js";
import {
  deepZoomStreams, viewerChoices, choiceLabel, pickStream, sourceFor, describe, sourceKey, loadOpenSeadragon, OsdUnavailable, UNAVAILABLE_TEXT,
} from "../deep-zoom.js";

/** What survives a repaint of the page's body. */
const memory = { key: null, viewer: null, stage: null, srcKey: "", O: null, state: "idle", error: "", loaded: 0, failed: 0 };

/** For tests: forget everything. */
export function resetDeepZoom() {
  try { memory.viewer?.destroy(); } catch { /* ignore */ }
  Object.assign(memory, { key: null, viewer: null, stage: null, srcKey: "", O: null, state: "idle", error: "", loaded: 0, failed: 0 });
}

const stageLabel = (choice) => (choice.preview ? "Viewer of the combined preview" : `Full-resolution viewer of the ${streamLabel(choice.key)} gold stack`);

const reducedMotion = () => globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;

/** The section's HTML; "" when no stream has a pyramid (the previews are all there is). */
export function deepZoomSectionHtml(view) {
  if (!deepZoomStreams(view).length) return "";
  const choices = viewerChoices(view);
  const cur = pickStream(choices, memory.key);
  return html`
    <section class="section--tight" aria-labelledby="dz-h" style="margin-top:36px" id="dz-section">
      <div class="section-header"><h2 id="dz-h">Explore full resolution</h2>
        <p>Pan and zoom each filter's gold stack at its full size, rendered from the gold tiles, or choose Preview for the auto-stretched combined preview. The 2048 px previews above show the whole field at once.</p></div>
      <div class="dz" id="dz">
        <div class="dz__bar">
          <div class="form__row" style="width:auto"><label for="dz-stream">Image</label>
            <select id="dz-stream"${choices.length > 1 ? "" : html` disabled`}>${choices.map((s) => html`<option value="${s.key}"${s.key === cur.key ? html` selected` : ""}>${choiceLabel(s)}</option>`)}</select></div>
          <div class="row" role="group" aria-label="Viewer controls">
            <button type="button" class="btn btn-ghost btn--sm" data-dz="in" disabled>Zoom in</button>
            <button type="button" class="btn btn-ghost btn--sm" data-dz="out" disabled>Zoom out</button>
            <button type="button" class="btn btn-ghost btn--sm" data-dz="fit" disabled>Fit to window</button>
            <button type="button" class="btn btn-ghost btn--sm" data-dz="full" disabled hidden>Full screen</button>
            <button type="button" class="btn btn-primary btn--sm" id="dz-open">Explore full resolution</button>
          </div>
        </div>
        <p class="muted dz__info" id="dz-info" role="status"></p>
        <div id="dz-slot"></div>
        <p class="muted dz__help" id="dz-help">Drag to pan; scroll, pinch or double-click to zoom. From the keyboard: Tab to the image, then the arrow keys pan, + and − zoom and 0 fits the window.</p>
      </div>
    </section>`;
}

/** Wire the section after it is in the page (no-op when it is not). `opts.onFullscreen` is for tests. */
export function wireDeepZoom(root, view) {
  const sec = root.querySelector("#dz-section");
  if (!sec) { memory.stage?.remove(); return; }
  const streams = viewerChoices(view);
  const pick = sec.querySelector("#dz-stream");
  const info = sec.querySelector("#dz-info");
  const open = sec.querySelector("#dz-open");
  const slot = sec.querySelector("#dz-slot");
  const buttons = Object.fromEntries([...sec.querySelectorAll("[data-dz]")].map((b) => [b.dataset.dz, b]));
  const fullscreenOk = !!document.fullscreenEnabled;
  if (fullscreenOk) buttons.full.hidden = false;

  const current = () => pickStream(streams, memory.key);
  memory.key = current()?.key ?? null;

  function paintState() {
    const s = current();
    const d = s ? describe(s) : null;
    const live = memory.state === "ready" || memory.state === "loading";
    open.hidden = live;
    for (const b of Object.values(buttons)) b.disabled = memory.state !== "ready";
    if (fullscreenOk) buttons.full.hidden = memory.state !== "ready";
    sec.querySelector("#dz-help").hidden = memory.state !== "ready";
    const text = memory.state === "loading" ? "Loading the viewer…"
      : memory.state === "error" ? memory.error
      : d ? `${d.text} ${d.behind}` : "";
    info.textContent = text.trim();
    sec.dataset.dzState = memory.state;
    sec.dataset.dzLoaded = String(memory.loaded);
    sec.dataset.dzFailed = String(memory.failed);
  }

  /** Open the current stream's pyramid in the viewer (creating it if need be), keeping the place if it is the same image shifted by a newer gold. */
  async function show() {
    const s = current();
    if (!s) return;
    const key = sourceKey(s);
    if (memory.viewer && memory.srcKey === key) return;
    if (!memory.viewer) {
      memory.state = "loading"; memory.error = "";
      paintState();
      try {
        memory.O = await loadOpenSeadragon();
      } catch (e) {
        memory.state = "error";
        memory.error = e instanceof OsdUnavailable ? e.message : UNAVAILABLE_TEXT.load_failed;
        paintState();
        return;
      }
      const O = memory.O;
      // OpenSeadragon logs every failed tile with console.error. Here a missing tile is expected (no data), so count it instead.
      const base = O.console || globalThis.console;
      O.console = { log: (...a) => base.log?.(...a), debug: (...a) => base.debug?.(...a), info: (...a) => base.info?.(...a), warn: (...a) => base.warn?.(...a), assert: (...a) => base.assert?.(...a), error: () => {} };
      const stage = document.createElement("div");
      stage.className = "dz__stage";
      stage.id = "dz-stage";
      stage.setAttribute("role", "group");
      stage.setAttribute("aria-label", stageLabel(s));
      stage.setAttribute("aria-describedby", "dz-help");
      memory.stage = stage;
      slot.append(stage);
      const quick = reducedMotion();
      let viewer;
      try {
        viewer = new O.Viewer({
          element: stage,
          tileSources: [sourceFor(s)],
          showNavigationControl: false,
          showNavigator: true,
          navigatorPosition: "BOTTOM_RIGHT",
          navigatorSizeRatio: 0.18,
          minZoomImageRatio: 0.9,
          maxZoomPixelRatio: 3,
          visibilityRatio: 0.4,
          constrainDuringPan: true,
          animationTime: quick ? 0 : 0.8,
          springStiffness: quick ? 100 : 6.5,
          blendTime: quick ? 0 : 0.2,
          imageLoaderLimit: 6,
          timeout: 30000,
          gestureSettingsTouch: { pinchRotate: false },
          gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: true },
        });
      } catch {
        stage.remove(); memory.stage = null;
        memory.state = "error"; memory.error = UNAVAILABLE_TEXT.no_api;
        paintState();
        return;
      }
      memory.viewer = viewer;
      memory.srcKey = key;
      viewer.addHandler("tile-loaded", () => { memory.loaded++; sec.dataset.dzLoaded = String(memory.loaded); });
      viewer.addHandler("tile-load-failed", () => { memory.failed++; sec.dataset.dzFailed = String(memory.failed); });
      viewer.addHandler("open", () => { memory.state = "ready"; paintState(); });
      // For the browser smoke and for anyone inspecting the page: where the viewer is, as text.
      viewer.addHandler("zoom", () => { const sec2 = document.getElementById("dz-section"); if (sec2) sec2.dataset.dzZoom = viewer.viewport.getZoom().toFixed(3); });
      viewer.addHandler("open-failed", () => { memory.state = "error"; memory.error = "The image could not be opened. The 2048 px preview above shows the whole stack."; paintState(); });
      // "r" rotates, "f" goes full page, "j" and "k" step through image sources: none of them has a place here.
      viewer.addHandler("canvas-key", (ev) => { const k = ev.originalEvent?.key; if (k && k.length === 1 && "rRfFjJkK".includes(k)) ev.preventDefaultAction = true; });
      return;
    }
    // Same viewer, another pyramid (another filter, or a newer gold of this one): keep the place when the picture has the same size.
    const prev = memory.viewer.world.getItemAt(0)?.getContentSize();
    const saved = prev ? { c: memory.viewer.viewport.getCenter(), z: memory.viewer.viewport.getZoom(), w: prev.x, h: prev.y } : null;
    memory.srcKey = key;
    memory.state = "loading";
    paintState();
    memory.viewer.addOnceHandler("open", () => {
      if (saved && s.deepZoom && saved.w === s.deepZoom.width && saved.h === s.deepZoom.height) { memory.viewer.viewport.zoomTo(saved.z, null, true); memory.viewer.viewport.panTo(saved.c, true); }
    });
    memory.viewer.open(sourceFor(s));
    memory.stage?.setAttribute("aria-label", stageLabel(s));
  }

  // After a repaint: put the viewer's element back and let it measure itself again.
  if (memory.viewer && memory.stage) {
    slot.append(memory.stage);
    memory.viewer.forceResize();
    // A newer gold may have arrived with this refresh.
    show();
  }
  paintState();

  open.addEventListener("click", async () => { await show(); sec.querySelector("#dz-stage")?.querySelector(".openseadragon-canvas")?.focus?.(); });
  pick.addEventListener("change", async () => {
    memory.key = pick.value;
    paintState();
    if (memory.viewer) await show();
  });
  const zoom = (f) => { const v = memory.viewer?.viewport; if (!v) return; v.zoomBy(f); v.applyConstraints(); };
  buttons.in.addEventListener("click", () => zoom(1.8));
  buttons.out.addEventListener("click", () => zoom(1 / 1.8));
  buttons.fit.addEventListener("click", () => memory.viewer?.viewport.goHome());
  buttons.full.addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else memory.stage?.requestFullscreen?.().catch(() => {});
  });
}

/** Whether the page should leave its body alone this refresh: a visitor looking at the viewer full screen would lose it. */
export const deepZoomIsFullscreen = () => !!memory.stage && document.fullscreenElement === memory.stage;
