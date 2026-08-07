/* meerail power save: stand the app down while it is not the one in front.

   A mail client in the background is still a mail client doing work. This one
   polls /api/sync/status, holds an SSE stream open, retimes a delayed send once
   a second, and — whenever the agent is mid-pass, which on a large mailbox is
   most of the time — runs a spinner that repaints at the display's refresh rate
   for as long as the pass lasts. None of it is visible behind another window,
   and on a laptop all of it is fan noise.

   So the background state is explicit rather than incidental: this module owns
   one flag, and everything that costs something registers to be told when it
   flips. Modules stop themselves — the alternative is this file reaching into
   five others' timers, which would rot the first time one of them changed.

   Two signals feed it, because neither covers the other:

     - `visibilitychange` is the portable one and the only one a browser has. It
       fires for a minimised window or a backgrounded tab. It does not fire for
       a window that is merely behind another, which is the common case on a
       desktop and the one worth catching.
     - The desktop shell reports the window's focus directly (electron/main.js
       dispatches `meerail:blur` / `meerail:focus`). That is what "the app in
       front" actually means, and it is the signal the user asked for.

   Resuming is always immediate and always does a full reload: while stood down
   the stream was closed, so what is on screen is however stale the pause was
   long, and the honest fix is to go and ask rather than to trust it. */

App.power = (function () {
  /* How long a focus loss has to last before the app stands down.

     Alt-tabbing out and straight back is common, and resuming is not free — it
     reopens the stream and reloads the list — so flapping costs more than the
     three seconds of polling it saves. Losing *visibility* skips the wait:
     there is no flash to avoid behind a minimised window, and nothing on screen
     to be interrupted. */
  const GRACE = 3000;

  let suspended = false;
  let focused = true;      // the shell tells us; a plain browser leaves it true
  let timer = null;
  const hooks = { suspend: [], resume: [] };

  function overlay() { return document.getElementById("power-save"); }

  /* One module throwing must not strand the others, and above all must not
     leave the overlay up over an app that is running again. */
  function run(fn) {
    try { fn(); } catch (e) { console.error("power-save hook failed", e); }
  }

  function render() {
    const el = overlay();
    if (el) el.hidden = !suspended;
    // What stops the animations — see the .is-power-save rules in mail.css. A
    // spinner nobody can see still costs a repaint every frame, and this app's
    // sync spinner is up for the whole of a pass.
    document.documentElement.classList.toggle("is-power-save", suspended);
  }

  function background() { return document.hidden || !focused; }

  function suspend() {
    clearTimeout(timer);
    timer = null;
    // Re-checked rather than assumed: the grace timer was set on a signal that
    // may since have been undone, and standing down over a window the user is
    // now looking at is the one unacceptable outcome.
    if (suspended || !background()) return;
    suspended = true;
    render();
    hooks.suspend.forEach(run);
  }

  function resume() {
    clearTimeout(timer);
    timer = null;
    if (!suspended) return;
    suspended = false;
    render();
    hooks.resume.forEach(run);
  }

  function defer() {
    clearTimeout(timer);
    if (!background()) return;
    timer = setTimeout(suspend, GRACE);
  }

  function init() {
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) suspend();
      else { focused = true; resume(); }
    });
    window.addEventListener("meerail:blur", () => { focused = false; defer(); });
    window.addEventListener("meerail:focus", () => { focused = true; resume(); });

    // A click lands on the overlay before it lands on the window beneath, and
    // on some setups reaches us ahead of the shell's focus event. Treating it
    // as a wake means the pointer never appears to hit a dead screen.
    const el = overlay();
    if (el) el.addEventListener("mousedown", () => { focused = true; resume(); });

    render();
  }

  return {
    init,
    /* Called when the app stands down. Stop timers, close streams, and expect
       to be told to start again — not to poll for it. */
    whenSuspended: (fn) => hooks.suspend.push(fn),
    /* Called when it comes back. Assume everything on screen is stale. */
    whenResumed: (fn) => hooks.resume.push(fn),
    isSuspended: () => suspended,
  };
})();
