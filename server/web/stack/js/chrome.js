// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// The shared header, footer, skip link and theme toggle, injected into placeholder elements so every page can be plain
// static HTML with no build step.
//
//   <header id="site-nav"></header>   <main id="main">...</main>   <footer id="site-footer"></footer>
//
// Loaded as a module on every page, after js/site-config.js.

import { config } from "./config.js";
import { html, setHtml } from "./dom.js";
import { MAIN_NAV, FOOTER_NAV } from "./nav-data.js";
import { getCachedUser, whoami } from "./session.js";

const here = () => (location.pathname.replace(/\/index\.html$/, "").replace(/\/+$/, "") || "/");

const linkHtml = (l) => html`<a href="${l.href}"${here() === l.href ? html` aria-current="page"` : ""}>${l.label}</a>`;

function navHtml(user) {
  return html`
    <div class="container nav__row">
      <a href="/stack/" class="nav__brand"><img src="/stack/favicon.svg" alt="" width="28" height="28"><span>${config.siteName}</span></a>
      <nav class="nav__links" id="primary-nav" aria-label="Primary">${MAIN_NAV.map(linkHtml)}${user?.name ? html`<span class="muted">${user.name}</span>` : ""}</nav>
      <button class="nav__toggle" type="button" aria-expanded="false" aria-controls="primary-nav" aria-label="Menu">☰</button>
    </div>`;
}

const THEMES = ["auto", "light", "dark"];
function currentTheme() {
  const t = document.documentElement.getAttribute("data-theme");
  return t === "light" || t === "dark" ? t : "auto";
}
function applyTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  try { if (t === "auto") localStorage.removeItem("sf_theme"); else localStorage.setItem("sf_theme", t); } catch { /* ignore */ }
}

function footerHtml() {
  return html`
    <div class="container footer__row">
      <div>
        <a href="/stack/" class="nav__brand"><img src="/stack/favicon.svg" alt="" width="20" height="20"><span>${config.siteName}</span></a>
        <p class="footer__legal">© ${new Date().getFullYear()} EKE Services LLC. Read-only progress pages of a Starfront collaboration.</p>
      </div>
      <nav class="footer__nav" aria-label="Footer">
        ${FOOTER_NAV.map((l) => html`<a href="${l.href}">${l.label}</a>`)}
        <button type="button" class="theme-toggle" id="theme-toggle" aria-label="Colour theme"></button>
      </nav>
    </div>`;
}

function paintHeader(user) {
  const el = document.getElementById("site-nav");
  if (!el) return;
  el.className = "nav";
  setHtml(el, navHtml(user));
  const btn = el.querySelector(".nav__toggle");
  btn?.addEventListener("click", () => {
    const open = el.classList.toggle("is-open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
}

function paintFooter() {
  const el = document.getElementById("site-footer");
  if (!el) return;
  el.className = "footer";
  setHtml(el, footerHtml());
  const tb = document.getElementById("theme-toggle");
  const label = () => { tb.textContent = "Theme: " + currentTheme(); };
  label();
  tb.addEventListener("click", () => {
    applyTheme(THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length]);
    label();
  });
}

function skipLink() {
  const main = document.querySelector("main");
  if (!main) return;
  if (!main.id) main.id = "main";
  const a = document.createElement("a");
  a.className = "skip-link";
  a.href = "#" + main.id;
  a.textContent = "Skip to content";
  document.body.insertBefore(a, document.body.firstChild);
}

skipLink();
const painted = getCachedUser();
paintHeader(painted);
paintFooter();
whoami().then((user) => {
  // Repaint only if the answer differs from what the cached first paint showed.
  if ((user?.name ?? null) !== (painted?.name ?? null)) paintHeader(user);
  document.dispatchEvent(new CustomEvent("sf:user", { detail: user }));
});
