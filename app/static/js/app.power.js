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
   long, and the honest fix is to go and ask rather than to trust it.

   All of it is optional. A machine that is plugged in and a user who wants the
   list already current when they come back are a fair trade against the fan,
   and the setting (Settings → Power) turns the whole module off rather than
   only its banner — a silent pause would be worse than a visible one. */

App.power = (function () {
  /* How long a focus loss has to last before the app stands down.

     Alt-tabbing out and straight back is common, and resuming is not free — it
     reopens the stream and reloads the list — so flapping costs more than the
     three seconds of polling it saves. Losing *visibility* skips the wait:
     there is no flash to avoid behind a minimised window, and nothing on screen
     to be interrupted. */
  const GRACE = 3000;

  // Off means "never stand down". Absent means on: the saving is the point of
  // the feature, and a first run should get it.
  const KEY = "meerail.powersave";

  let suspended = false;
  let focused = true;      // the shell tells us; a plain browser leaves it true
  let timer = null;
  const hooks = { suspend: [], resume: [] };

  function bar() { return document.getElementById("power-save"); }

  /* One module throwing must not strand the others, and above all must not
     leave the bar up over an app that is running again. */
  function run(fn) {
    try { fn(); } catch (e) { console.error("power-save hook failed", e); }
  }

  function render() {
    const el = bar();
    if (el) el.hidden = !suspended;
    // What stops the animations — see the .is-power-save rules in mail.css. A
    // spinner nobody can see still costs a repaint every frame, and this app's
    // sync spinner is up for the whole of a pass.
    document.documentElement.classList.toggle("is-power-save", suspended);
  }

  function background() { return document.hidden || !focused; }

  function enabled() { return localStorage.getItem(KEY) !== "0"; }

  function suspend() {
    clearTimeout(timer);
    timer = null;
    // Re-checked rather than assumed: the grace timer was set on a signal that
    // may since have been undone, and standing down over a window the user is
    // now looking at is the one unacceptable outcome. `enabled` is in the same
    // position — the setting can have been turned off during the grace wait.
    if (suspended || !enabled() || !background()) return;
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
    if (!enabled() || !background()) return;
    timer = setTimeout(suspend, GRACE);
  }

  /* Turning it off has to undo the state as well as the setting: the usual way
     to reach the switch is to have just been annoyed by the bar, and leaving
     the app stood down behind the settings modal would be absurd. Turning it on
     does nothing now — the window it would pause is the one in front. */
  function setEnabled(on) {
    localStorage.setItem(KEY, on ? "1" : "0");
    if (!on) resume();
  }

  function init() {
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) suspend();
      else { focused = true; resume(); }
    });
    window.addEventListener("meerail:blur", () => { focused = false; defer(); });
    window.addEventListener("meerail:focus", () => { focused = true; resume(); });

    // Any use of the app is a wake, and on some setups the first click reaches
    // us ahead of the shell's focus event. Capture, and on the document rather
    // than on the bar: the bar no longer covers anything, so a click meant for
    // a message must not land on a list that stopped being maintained three
    // minutes ago. Cheap while running — `suspended` is false and it returns.
    const wake = () => { if (!suspended) return; focused = true; resume(); };
    document.addEventListener("mousedown", wake, true);
    document.addEventListener("keydown", wake, true);

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
    /* The Power setting: whether the app is allowed to stand down at all. */
    enabled, setEnabled,
  };
})();
