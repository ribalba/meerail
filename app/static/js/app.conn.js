/* meerail connection watchdog: the red "server unreachable" bar at the bottom.

   Nothing in the app reports the server being gone — a fetch that never lands
   just leaves a button spinning, and EventSource reconnects in silence. This
   module is the one place that says so out loud.

   Two things feed it: every API call reports its outcome (`ok` / `fail`), and
   the SSE stream reports open/error. Neither is trusted on its own — a single
   failed request is often just one bad endpoint, and EventSource errors fire
   on ordinary reconnects. So a failure only *suspects* an outage; the bar
   appears when a probe of /healthz confirms it, and disappears the moment
   anything succeeds. */

App.conn = (function () {
  const PROBE_MIN = 2000;    // first retry, and the delay before we accuse
  const PROBE_MAX = 15000;   // backoff ceiling while it stays down

  let down = false;
  let probing = false;
  let delay = PROBE_MIN;
  let timer = null;
  let wakeNow = null;   // resolves the current backoff wait early
  const restoreHooks = [];

  function bar() { return document.getElementById("conn-bar"); }

  function render() {
    const el = bar();
    if (!el) return;
    el.hidden = !down;
  }

  // A probe is the only thing that flips the bar on. Every failure path funnels
  // here rather than showing the bar directly, so one flaky request or a routine
  // SSE reconnect never flashes red at the user.
  async function probe() {
    try {
      const res = await fetch("/healthz", { cache: "no-store" });
      if (!res.ok) throw new Error("unhealthy");
      return true;
    } catch (_) {
      return false;
    }
  }

  async function loop() {
    probing = true;
    while (probing) {
      if (await probe()) { ok(); return; }
      if (!down) { down = true; render(); }
      await new Promise((r) => { wakeNow = r; timer = setTimeout(r, delay); });
      wakeNow = null;
      delay = Math.min(delay * 2, PROBE_MAX);
    }
  }

  /* Retry immediately instead of waiting out the backoff — used when the
     browser tells us the network came back, or the tab becomes visible. */
  function retryNow() {
    delay = PROBE_MIN;
    if (wakeNow) { clearTimeout(timer); wakeNow(); }
  }

  /* Something couldn't reach the server. Starts the probe loop, which decides
     whether that was a real outage. Safe to call on every failure. */
  function fail() {
    if (probing) return;
    delay = PROBE_MIN;
    loop();
  }

  /* Proof the server answered. Clears the bar and stops probing. */
  function ok() {
    probing = false;
    clearTimeout(timer);
    if (!down) return;
    down = false;
    render();
    const hooks = restoreHooks.splice(0);
    hooks.forEach((fn) => fn());
  }

  /* Run once when the server comes back. For work that cannot simply be retried
     in place — a boot that never finished, say. */
  function whenRestored(fn) { restoreHooks.push(fn); }

  function init() {
    render();
    // The browser losing the network is a free, instant signal — no need to
    // wait for a request to time out first.
    window.addEventListener("offline", fail);
    window.addEventListener("online", () => { retryNow(); fail(); });
    // A laptop waking from sleep gets no "online" event but its sockets are
    // dead; checking on focus turns a 15s wait into an instant recovery.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && probing) retryNow();
    });
  }

  return { init, fail, ok, whenRestored, isDown: () => down };
})();
