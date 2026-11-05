// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Runs in <head>, before first paint, so a visitor who chose a theme never sees the other one flash.
// Classic script (not a module) on purpose: modules are deferred. Storage may be blocked; fail quietly.
(function () {
  try {
    var t = localStorage.getItem("sf_theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage unavailable: follow the system */ }
})();
