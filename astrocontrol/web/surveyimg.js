/* Survey cutouts for the page, fetched through one place (2026-11-03).
 *
 * The window's browser allows six connections to this program. When the
 * survey service (CDS) is down, every cutout used to wait out a dead
 * connection, a redraw asked for the same picture again, and six such
 * requests held every connection: join, sign out and every other click
 * queued behind them. Here:
 *  - each picture is fetched once; redraws while it loads share that fetch;
 *  - at most two cutouts are in flight, so four connections stay free;
 *  - one fetch gives up after 15 s (the program's own timeout is 12 s);
 *  - after a failure (503 + Retry-After from the program, a timeout, or no
 *    answer) no cutout is asked for for two minutes, or as long as the
 *    program says, and callers get an immediate rejection instead.
 */
(function () {
  const MAX_IN_FLIGHT = 2;
  const TIMEOUT_MS = 15000;
  const DOWN_MS = 120000;

  function create(fetchImpl, decode, now) {
    const done = new Map();       // url -> decoded image
    const inflight = new Map();   // url -> promise
    const queue = [];
    let running = 0;
    let downUntil = 0;
    let downReason = '';

    const downError = () => {
      const error = new Error(downReason || 'the survey service is unreachable');
      error.surveyDown = true;
      error.retryInMs = Math.max(0, downUntil - now());
      return error;
    };

    function pump() {
      while (running < MAX_IN_FLIGHT && queue.length) {
        const job = queue.shift();
        if (now() < downUntil) { job.reject(downError()); continue; }
        running++;
        run(job.url).then(job.resolve, job.reject).finally(() => { running--; pump(); });
      }
    }

    async function run(url) {
      const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
      const timer = setTimeout(() => controller && controller.abort(), TIMEOUT_MS);
      let response;
      try {
        response = await fetchImpl(url, controller ? { signal: controller.signal } : {});
      } catch (error) {
        downUntil = now() + DOWN_MS;
        downReason = 'the survey service did not answer';
        throw downError();
      } finally {
        clearTimeout(timer);
      }
      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try { const body = await response.json(); if (body && body.detail) detail = body.detail; } catch { /* not JSON */ }
        if (response.status >= 500) {
          const after = Number(response.headers && response.headers.get && response.headers.get('Retry-After'));
          downUntil = now() + (after > 0 ? after * 1000 : DOWN_MS);
          downReason = detail;
          throw downError();
        }
        throw new Error(detail);          // a bad request; the service is fine
      }
      const image = await decode(await response.blob());
      done.set(url, image);
      return image;
    }

    return {
      /** The decoded picture for `url`, or a rejection (error.surveyDown when backing off). */
      image(url) {
        if (done.has(url)) return Promise.resolve(done.get(url));
        if (inflight.has(url)) return inflight.get(url);
        if (now() < downUntil) return Promise.reject(downError());
        const promise = new Promise((resolve, reject) => { queue.push({ url, resolve, reject }); pump(); })
          .finally(() => inflight.delete(url));
        inflight.set(url, promise);
        return promise;
      },
      cached(url) { return done.get(url) || null; },
      isDown() { return now() < downUntil; },
      retryInMs() { return Math.max(0, downUntil - now()); },
      _stats() { return { running, queued: queue.length, inflight: inflight.size }; },
    };
  }

  const decodeBlob = (blob) => new Promise((resolve, reject) => {
    const element = new Image();
    element.onload = () => resolve(element);
    element.onerror = () => reject(new Error('the image could not be decoded'));
    element.src = URL.createObjectURL(blob);
  });

  const root = typeof window !== 'undefined' ? window : globalThis;
  root.surveyImagesCreate = create;
  if (typeof window !== 'undefined' && typeof fetch !== 'undefined') {
    root.surveyImages = create((u, o) => fetch(u, o), decodeBlob, () => Date.now());
  }
})();
