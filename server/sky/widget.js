/* Starfront sky: where the observatory's telescopes are pointing tonight.
 *
 * Drop-in for any web page. Put an element on the page and this script
 * beside it:
 *
 *   <div data-starfront-sky></div>
 *   <script src="https://collab.starfront.space/sky/widget.js" async></script>
 *
 * It draws the sky as a planetarium does - the dome overhead, the bright
 * stars in place, the horizon as a line with the compass points on it and
 * everything below it darkened - and on it every telescope that shares its
 * position: a mark where it points now, its name and target. Drag to look around,
 * scroll or pinch to zoom, press "Overhead" to come back to the whole sky.
 *
 * Options go on the element as data attributes:
 *   data-server="https://collab.starfront.space"  where to ask (default: this script's own host)
 *   data-hours="14"      how far back the tracks go
 *   data-refresh="60"    seconds between refreshes
 *   data-lat="31.9" data-lon="-99.1"  the observatory, for the horizon
 *   data-title="Tonight at Starfront"  the heading; blank for none
 *   data-height="auto"   pixels, or "auto" for two thirds of the width
 *   data-fov="196"       degrees of sky across the view to start with; 180 is horizon to horizon
 *   data-tracks="1"      also draw each telescope's track through the night (off by default)
 *
 * Everything it asks for is public and read-only. Nothing is written
 * anywhere, nothing is stored in the visitor's browser.
 */
(function () {
  'use strict';

  var script = document.currentScript;
  var scriptOrigin = '';
  try { scriptOrigin = script && script.src ? new URL(script.src).origin : ''; } catch (e) { scriptOrigin = ''; }

  var STATE_COLOURS = {
    imaging: '#5be39a', exposing: '#5be39a', slewing: '#f0b35b', centring: '#f0b35b',
    centering: '#f0b35b', focusing: '#8ec5ff', solving: '#8ec5ff', guiding: '#5be39a',
    flipping: '#f0b35b', idle: '#8a93a6', parked: '#8a93a6', cooling: '#8ec5ff'
  };
  var DEG = Math.PI / 180;

  function palette(index) {
    var hues = [152, 32, 205, 280, 0, 60, 330, 100, 180, 20, 240, 300];
    return 'hsl(' + hues[index % hues.length] + ', 75%, 65%)';
  }

  /* ---------------------------------------------------------- astronomy */

  // Greenwich mean sidereal time in hours, good to a few seconds.
  function gmstHours(date) {
    var jd = date.getTime() / 86400000 + 2440587.5;
    var t = (jd - 2451545.0) / 36525.0;
    var gmst = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t;
    gmst = ((gmst % 360) + 360) % 360;
    return gmst / 15;
  }

  // Altitude in degrees of a point (RA hours, Dec degrees) from a site.
  function altitude(raHours, dec, latDeg, lstHours) {
    var ha = ((lstHours - raHours) * 15) * DEG;
    var lat = latDeg * DEG;
    var d = dec * DEG;
    var s = Math.sin(lat) * Math.sin(d) + Math.cos(lat) * Math.cos(d) * Math.cos(ha);
    return Math.asin(Math.max(-1, Math.min(1, s))) / DEG;
  }

  // The RA (hours) and Dec of a point at an altitude and azimuth from a site.
  function skyAt(altDeg, azDeg, latDeg, lstHours) {
    var alt = altDeg * DEG, az = azDeg * DEG, lat = latDeg * DEG;
    var sinDec = Math.sin(alt) * Math.sin(lat) + Math.cos(alt) * Math.cos(lat) * Math.cos(az);
    var dec = Math.asin(Math.max(-1, Math.min(1, sinDec)));
    var y = -Math.sin(az) * Math.cos(alt);
    var x = Math.cos(lat) * Math.sin(alt) - Math.sin(lat) * Math.cos(alt) * Math.cos(az);
    var ha = Math.atan2(y, x) / DEG / 15;
    return { ra: ((lstHours - ha) % 24 + 24) % 24, dec: dec / DEG };
  }

  /* ------------------------------------------------------------- widget */

  function build(host) {
    var opts = {
      server: host.getAttribute('data-server') || scriptOrigin || '',
      hours: Number(host.getAttribute('data-hours')) || 14,
      refresh: Math.max(15, Number(host.getAttribute('data-refresh')) || 60),
      lat: host.hasAttribute('data-lat') ? Number(host.getAttribute('data-lat')) : 31.9,
      lon: host.hasAttribute('data-lon') ? Number(host.getAttribute('data-lon')) : -99.1,
      title: host.hasAttribute('data-title') ? host.getAttribute('data-title') : 'Tonight at Starfront',
      height: host.getAttribute('data-height') || 'auto',
      fov: Math.max(20, Math.min(220, Number(host.getAttribute('data-fov')) || 196)),
      tracks: host.getAttribute('data-tracks') === '1'
    };

    host.classList.add('starfront-sky');
    host.innerHTML = '';
    var style = document.createElement('style');
    style.textContent = [
      '.starfront-sky{font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#dfe5f0;background:#0b0e14;border-radius:10px;padding:14px 16px 12px;box-sizing:border-box;}',
      '.starfront-sky *{box-sizing:border-box;}',
      '.starfront-sky .sf-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:0 0 8px;}',
      '.starfront-sky .sf-title{font-size:18px;font-weight:600;margin:0;}',
      '.starfront-sky .sf-sub{color:#8a93a6;font-size:13px;}',
      '.starfront-sky .sf-stage{position:relative;}',
      '.starfront-sky canvas{display:block;width:100%;height:auto;border-radius:6px;background:#05070c;cursor:grab;touch-action:none;user-select:none;-webkit-user-select:none;}',
      '.starfront-sky canvas.sf-dragging{cursor:grabbing;}',
      '.starfront-sky .sf-tools{position:absolute;right:8px;top:8px;display:flex;gap:4px;}',
      '.starfront-sky .sf-tools button{font:12px system-ui,sans-serif;color:#dfe5f0;background:rgba(11,14,20,.75);border:1px solid rgba(140,155,190,.35);border-radius:5px;padding:3px 9px;cursor:pointer;}',
      '.starfront-sky .sf-tools button:hover{background:rgba(31,90,60,.8);border-color:#2f7a50;}',
      '.starfront-sky .sf-list{list-style:none;margin:10px 0 0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:4px 16px;}',
      '.starfront-sky .sf-list li{display:flex;align-items:center;gap:8px;min-width:0;font-size:13px;}',
      '.starfront-sky .sf-dot{width:10px;height:10px;border-radius:50%;flex:none;}',
      '.starfront-sky .sf-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
      '.starfront-sky .sf-what{color:#8a93a6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
      '.starfront-sky .sf-off{opacity:.45;}',
      '.starfront-sky .sf-foot{color:#5c6577;font-size:11px;margin-top:8px;}'
    ].join('');
    host.appendChild(style);

    var head = document.createElement('div');
    head.className = 'sf-head';
    if (opts.title) {
      var title = document.createElement('h3');
      title.className = 'sf-title';
      title.textContent = opts.title;
      head.appendChild(title);
    }
    var sub = document.createElement('span');
    sub.className = 'sf-sub';
    sub.textContent = 'loading…';
    head.appendChild(sub);
    host.appendChild(head);

    var stage = document.createElement('div');
    stage.className = 'sf-stage';
    var canvas = document.createElement('canvas');
    stage.appendChild(canvas);
    var tools = document.createElement('div');
    tools.className = 'sf-tools';
    var zoomIn = document.createElement('button'); zoomIn.textContent = '+'; zoomIn.title = 'Zoom in';
    var zoomOut = document.createElement('button'); zoomOut.textContent = '−'; zoomOut.title = 'Zoom out';
    var overhead = document.createElement('button'); overhead.textContent = 'Overhead'; overhead.title = 'The whole sky, straight up';
    tools.appendChild(zoomOut); tools.appendChild(zoomIn); tools.appendChild(overhead);
    stage.appendChild(tools);
    host.appendChild(stage);
    var list = document.createElement('ul');
    list.className = 'sf-list';
    host.appendChild(list);
    var foot = document.createElement('div');
    foot.className = 'sf-foot';
    foot.textContent = 'The sky over the observatory right now, north up and east to the left as you see it looking up. Drag to look around, scroll to zoom. The dark part is below the horizon.'
      + (opts.tracks ? ' Tracks show where each telescope has pointed through the night.' : '');
    host.appendChild(foot);

    var stars = [];
    var data = null;
    var ctx = canvas.getContext('2d');
    // Where the view looks: right ascension in hours and declination, and
    // how many degrees of sky fit across it. Starts straight up.
    var view = { ra: 0, dec: opts.lat, fov: opts.fov, followZenith: true };

    function lstNow() { return gmstHours(new Date()) + opts.lon / 15; }

    function size() {
      var width = Math.max(240, host.clientWidth - 32);
      var height = opts.height === 'auto' ? Math.round(width * 2 / 3) : Math.max(120, Number(opts.height) || Math.round(width * 2 / 3));
      var ratio = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
        canvas.style.height = height + 'px';
      }
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      return { w: width, h: height };
    }

    /* A stereographic projection about the view's centre: the sky as a
       planetarium shows it, shapes kept, north up, east to the left. */
    function projector(w, h) {
      var ra0 = view.ra * 15 * DEG, dec0 = view.dec * DEG;
      var sin0 = Math.sin(dec0), cos0 = Math.cos(dec0);
      // Scaled so that `fov` degrees of sky span the picture's shorter side:
      // a point c degrees from the centre lands 2 tan(c/2) radii out, so the
      // edge of the view, fov/2 from the centre, lands at the picture's edge.
      // At 180 the horizon ring just fits; Overhead starts a little wider.
      var radius = (Math.min(w, h) / 2) / (2 * Math.tan(view.fov * DEG / 4));
      return {
        w: w, h: h, radius: radius,
        to: function (raHours, dec) {
          var a = raHours * 15 * DEG - ra0, d = dec * DEG;
          var sd = Math.sin(d), cd = Math.cos(d), ca = Math.cos(a);
          var cosc = sin0 * sd + cos0 * cd * ca;
          if (cosc < -0.75) return null;                // too far round the sphere to draw
          var k = 2 / (1 + cosc);
          var x = k * cd * Math.sin(a);
          var y = k * (cos0 * sd - sin0 * cd * ca);
          return { x: w / 2 - x * radius, y: h / 2 - y * radius, k: k };
        },
        from: function (X, Y) {
          var x = (w / 2 - X) / radius, y = (h / 2 - Y) / radius;
          var rho = Math.sqrt(x * x + y * y);
          if (rho < 1e-9) return { ra: view.ra, dec: view.dec };
          var c = 2 * Math.atan(rho / 2);
          var sc = Math.sin(c), cc = Math.cos(c);
          var dec = Math.asin(Math.max(-1, Math.min(1, cc * sin0 + y * sc * cos0 / rho)));
          var a = Math.atan2(x * sc, rho * cos0 * cc - y * sin0 * sc);
          return { ra: (((ra0 + a) / DEG / 15) % 24 + 24) % 24, dec: dec / DEG };
        }
      };
    }

    /* A curve on the sky drawn as a polyline, broken where it leaves the
       projection or jumps across it. */
    function curve(P, points, style, width, dash) {
      ctx.strokeStyle = style;
      ctx.lineWidth = width;
      ctx.setLineDash(dash || []);
      ctx.beginPath();
      var pen = false, last = null;
      for (var i = 0; i < points.length; i++) {
        var p = P.to(points[i].ra, points[i].dec);
        if (!p || Math.abs(p.x) > P.w * 4 || Math.abs(p.y) > P.h * 4) { pen = false; last = null; continue; }
        if (last && (Math.abs(p.x - last.x) > P.w || Math.abs(p.y - last.y) > P.h)) pen = false;
        if (!pen) { ctx.moveTo(p.x, p.y); pen = true; } else ctx.lineTo(p.x, p.y);
        last = p;
      }
      ctx.stroke();
      ctx.setLineDash([]);
    }

    function draw() {
      var box = size();
      var w = box.w, h = box.h;
      var lst = lstNow();
      if (view.followZenith) { view.ra = lst; view.dec = opts.lat; }
      var P = projector(w, h);

      ctx.clearRect(0, 0, w, h);
      var sky = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.max(w, h) * 0.7);
      sky.addColorStop(0, '#0a1020');
      sky.addColorStop(1, '#03050a');
      ctx.fillStyle = sky;
      ctx.fillRect(0, 0, w, h);

      // Lines of right ascension and declination, faint, as curves.
      var i, j, pts;
      for (var hr = 0; hr < 24; hr += 2) {
        pts = [];
        for (j = -88; j <= 88; j += 2) pts.push({ ra: hr, dec: j });
        curve(P, pts, 'rgba(120, 140, 180, 0.13)', 1);
      }
      for (var dd = -60; dd <= 60; dd += 30) {
        pts = [];
        for (j = 0; j <= 360; j += 2) pts.push({ ra: j / 15, dec: dd });
        curve(P, pts, dd === 0 ? 'rgba(120, 140, 180, 0.22)' : 'rgba(120, 140, 180, 0.13)', 1);
      }

      // Stars, brightest largest, faint ones fading out as the view widens.
      var fade = Math.min(1, 90 / view.fov);
      for (i = 0; i < stars.length; i++) {
        var s = stars[i];
        var sp = P.to(s[0] / 15, s[1]);
        if (!sp || sp.x < -4 || sp.x > w + 4 || sp.y < -4 || sp.y > h + 4) continue;
        var r = Math.max(0.5, (5.8 - s[2]) * (0.3 + 0.5 * fade));
        var alpha = Math.min(1, 0.3 + (5.5 - s[2]) * 0.18);
        if (s[2] > 4 && view.fov > 90) alpha *= 0.6;
        ctx.fillStyle = 'rgba(230, 236, 250, ' + alpha + ')';
        ctx.beginPath(); ctx.arc(sp.x, sp.y, r, 0, Math.PI * 2); ctx.fill();
      }

      // What is below the horizon: each cell of the picture asked where it
      // is on the sky and how high that is right now.
      var cell = Math.max(6, Math.round(w / 110));
      ctx.fillStyle = 'rgba(0, 0, 0, 0.62)';
      for (var y = 0; y < h; y += cell) {
        for (var x = 0; x < w; x += cell) {
          var at = P.from(x + cell / 2, y + cell / 2);
          var there = P.to(at.ra, at.dec);
          if (!there || altitude(at.ra, at.dec, opts.lat, lst) < 0) {
            ctx.fillRect(x, y, cell + 0.5, cell + 0.5);
          }
        }
      }

      // The horizon itself, with the compass points on it.
      pts = [];
      for (j = 0; j <= 360; j += 1) pts.push(skyAt(0, j, opts.lat, lst));
      curve(P, pts, 'rgba(126, 231, 165, 0.55)', 1.5);
      ctx.fillStyle = 'rgba(200, 215, 235, 0.85)';
      ctx.font = '600 12px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      var compass = [['N', 0], ['E', 90], ['S', 180], ['W', 270]];
      for (i = 0; i < compass.length; i++) {
        var c = skyAt(2, compass[i][1], opts.lat, lst);
        var cp = P.to(c.ra, c.dec);
        if (cp && cp.x > 8 && cp.x < w - 8 && cp.y > 8 && cp.y < h - 8) ctx.fillText(compass[i][0], cp.x, cp.y);
      }
      ctx.textBaseline = 'alphabetic';

      if (!data) return;
      var rows = data.telescopes || [];
      // The night's tracks, under the marks, only when asked for: a slew
      // from one target to the next draws a line right across the sky, and
      // a dozen of them read as a tangle rather than a story.
      if (opts.tracks) {
        rows.forEach(function (t, index) {
          var colour = palette(index);
          var trail = t.trail || [];
          if (trail.length > 1) {
            var tp = [];
            for (var k = 0; k < trail.length; k++) tp.push({ ra: trail[k][1], dec: trail[k][2] });
            ctx.globalAlpha = t.online ? 0.75 : 0.3;
            curve(P, tp, colour, 1.5);
            ctx.globalAlpha = 1;
          }
        });
      }
      rows.forEach(function (t, index) {
        var colour = palette(index);
        var p = P.to(t.ra, t.dec);
        if (!p || p.x < -20 || p.x > w + 20 || p.y < -20 || p.y > h + 20) return;
        ctx.globalAlpha = t.online ? 1 : 0.4;
        ctx.strokeStyle = colour;
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(p.x, p.y, 6, 0, Math.PI * 2); ctx.stroke();
        ctx.fillStyle = STATE_COLOURS[(t.state || '').toLowerCase()] || colour;
        ctx.beginPath(); ctx.arc(p.x, p.y, 2.5, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = '#eef2fa';
        ctx.font = '600 11px system-ui, sans-serif';
        // The label sits to the right of the mark unless it would run off
        // the chart there, in which case it sits to the left.
        var label = t.name + (t.target ? ' · ' + t.target : '');
        var flip = p.x + 9 + ctx.measureText(label).width > w - 4;
        ctx.textAlign = flip ? 'right' : 'left';
        ctx.shadowColor = 'rgba(0, 0, 0, 0.9)';
        ctx.shadowBlur = 3;
        ctx.fillText(label, p.x + (flip ? -9 : 9), p.y + 4);
        ctx.shadowBlur = 0;
        ctx.globalAlpha = 1;
      });
      ctx.textAlign = 'left';
    }

    /* Looking around: drag moves the sky under the pointer, the wheel and
       the buttons zoom, Overhead puts the zenith back in the middle. */
    var drag = null;
    canvas.addEventListener('pointerdown', function (event) {
      if (event.button !== undefined && event.button !== 0) return;
      var box = size();
      var P = projector(box.w, box.h);
      var rect = canvas.getBoundingClientRect();
      drag = { P: P, grab: P.from(event.clientX - rect.left, event.clientY - rect.top), ra: view.ra, dec: view.dec };
      try { canvas.setPointerCapture(event.pointerId); } catch (e) { /* fine */ }
      canvas.classList.add('sf-dragging');
      event.preventDefault();
    });
    canvas.addEventListener('pointermove', function (event) {
      if (!drag) return;
      var rect = canvas.getBoundingClientRect();
      var now = drag.P.from(event.clientX - rect.left, event.clientY - rect.top);
      // Keep the point grabbed under the pointer: the centre moves by the
      // opposite of how far the grabbed point moved.
      var dra = now.ra - drag.grab.ra;
      if (dra > 12) dra -= 24; else if (dra < -12) dra += 24;
      view.followZenith = false;
      view.ra = ((drag.ra - dra) % 24 + 24) % 24;
      view.dec = Math.max(-89, Math.min(89, drag.dec - (now.dec - drag.grab.dec)));
      draw();
    });
    function release() { drag = null; canvas.classList.remove('sf-dragging'); }
    canvas.addEventListener('pointerup', release);
    canvas.addEventListener('pointercancel', release);
    canvas.addEventListener('lostpointercapture', release);
    function zoom(factor) {
      view.fov = Math.max(20, Math.min(220, view.fov * factor));
      draw();
    }
    canvas.addEventListener('wheel', function (event) {
      event.preventDefault();
      zoom(Math.pow(1.15, event.deltaY > 0 ? 1 : -1));
    }, { passive: false });
    zoomIn.addEventListener('click', function () { zoom(1 / 1.3); });
    zoomOut.addEventListener('click', function () { zoom(1.3); });
    overhead.addEventListener('click', function () { view.followZenith = true; view.fov = opts.fov; draw(); });

    function describe(t) {
      var bits = [];
      if (t.target) bits.push(t.target);
      if (t.state) bits.push(t.state);
      if (!t.online) {
        var mins = Math.round((t.ageSeconds || 0) / 60);
        bits.push(mins < 120 ? mins + ' min ago' : Math.round(mins / 60) + ' h ago');
      }
      return bits.join(' · ');
    }

    function fill() {
      list.innerHTML = '';
      if (!data) return;
      var rows = data.telescopes || [];
      rows.forEach(function (t, index) {
        var li = document.createElement('li');
        if (!t.online) li.className = 'sf-off';
        var dot = document.createElement('span');
        dot.className = 'sf-dot';
        dot.style.background = palette(index);
        var name = document.createElement('span');
        name.className = 'sf-name';
        name.textContent = t.name;
        var what = document.createElement('span');
        what.className = 'sf-what';
        what.textContent = describe(t);
        li.appendChild(dot); li.appendChild(name); li.appendChild(what);
        list.appendChild(li);
      });
      var when = new Date((data.serverTime || Date.now() / 1000) * 1000);
      sub.textContent = (data.online || 0) + ' telescope' + (data.online === 1 ? '' : 's') + ' on the sky, '
        + (data.imaging || 0) + ' imaging · as of ' + when.toUTCString().slice(17, 22) + ' UTC';
    }

    function fetchJson(url) {
      return fetch(url, { cache: 'no-store' }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
    }

    function refresh() {
      fetchJson(opts.server + '/api/v1/sky?hours=' + encodeURIComponent(opts.hours))
        .then(function (payload) { data = payload; fill(); draw(); })
        .catch(function (err) { sub.textContent = 'the observatory is not answering right now'; draw(); });
    }

    fetchJson(opts.server + '/sky/stars.json')
      .then(function (payload) { stars = payload.stars || []; draw(); })
      .catch(function () { draw(); });
    refresh();
    setInterval(refresh, opts.refresh * 1000);
    // The sky turns even when nothing else changes.
    setInterval(draw, 30 * 1000);
    window.addEventListener('resize', draw);
    draw();
  }

  function start() {
    var hosts = document.querySelectorAll('[data-starfront-sky]');
    for (var i = 0; i < hosts.length; i++) {
      if (!hosts[i].getAttribute('data-sf-built')) {
        hosts[i].setAttribute('data-sf-built', '1');
        build(hosts[i]);
      }
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();

  // A page builder that re-renders its sections - Shopify's theme editor
  // does, on every change - replaces the element without running this
  // script again. Any sky element that appears later is built as it
  // arrives, and the page may ask directly: window.StarfrontSky.start().
  if (window.MutationObserver) {
    new MutationObserver(function () { start(); }).observe(document.documentElement, { childList: true, subtree: true });
  }
  document.addEventListener('shopify:section:load', start);
  window.StarfrontSky = { start: start };
})();
