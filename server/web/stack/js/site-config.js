// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Deployment settings: a classic script, loaded before every module, that sets window.COLLAB_CONFIG. Anything left out
// falls back to the defaults in js/config.js. The pages are served by the Starfront server (server/app.py), so
// everything is same-origin under /stack/.
window.COLLAB_CONFIG = {
  apiBase: "/stack/api",
  publicBase: "/stack/public",
  siteName: "Starfront"
};
