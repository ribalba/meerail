/* meerail dictation. Speak into the composer.

   The recording is made here and transcribed by a Whisper service that this
   install runs on its own network ([dictation] in meerail.toml, see
   app/dictation.py). As with the grammar checker, the browser never talks to
   that service. Every piece of audio goes to /api/dictation/transcribe on
   meerail's own server, which is the only thing that knows where the service
   is, and which refuses to send a recording to a public address unless the
   operator said so.

   Whisper is not a streaming model. It transcribes a finished stretch of
   audio, and on a CPU the large models need a few seconds for each one.
   Recording the whole dictation and sending it at the end would make the
   writer wait for every sentence at once. So the recording is cut at the
   speaker's own pauses instead, and each piece is sent as soon as it is cut,
   while the next one is still being spoken. Text appears a sentence at a time
   shortly after each pause, and pressing stop leaves only the last sentence to
   wait for, however long the dictation was.

   Each piece costs the service about the same few seconds however short it
   is, because Whisper always works on a thirty-second window. So how often
   the recording is cut follows how busy the service is. While it is free, the
   first pause after a phrase cuts, and the text follows promptly. While it is
   still writing the piece before, the next one grows until the service is
   free again or the piece is long. Somebody who talks on and on gets fewer,
   longer pieces, rather than a queue that falls further behind with every
   sentence.

   Three things decide the quality, and they shaped most of what is below.

     - Pieces end on pauses, never inside a word. The pause detection is a
       plain energy gate whose floor follows the room's noise, and a pause only
       closes a piece once the piece holds enough speech to be worth
       transcribing on its own. A long run without pauses is still cut before
       Whisper's thirty-second window, at the next short pause if there is one.
     - Each piece goes out with the text just before the insertion point. The
       server hands it to Whisper as the preceding text, which keeps casing,
       punctuation and the spelling of names consistent from one piece to the
       next. That is also why the pieces are sent one at a time. A piece waits
       for the one before it, so that its context includes what was just said.
     - Audio is sent at 16 kHz, the rate Whisper works at, resampled by the
       browser's own resampler (an OfflineAudioContext).

   Nothing is kept. A piece lives in memory until its text is in the draft.

   The recording does not register with App.power. Power save stands down what
   the app does on its own behalf, and a microphone the writer switched on is
   not that. Somebody dictating while they read a document in another window
   would otherwise lose what they were saying the moment the window lost focus. */

App.dictation = (function () {
  const $ = (s) => document.querySelector(s);

  // --- Tuning --------------------------------------------------------------

  // Silence that ends a piece. Long enough that the gap between two phrases
  // of one sentence does not cut it in half, short enough that the text
  // follows the end of a sentence promptly.
  const PAUSE_MS = 700;
  // A piece needs this much speech before a pause may close it. "Hallo
  // Peter," alone gives Whisper too little to go on, and it is transcribed
  // better as the start of the next sentence's piece than as a piece of its own.
  const MIN_PIECE_SPEECH_MS = 1200;
  // While the service is free, a pause this long closes a piece however little
  // it holds, so a greeting followed by a long think still turns up while the
  // writer thinks.
  const LONG_PAUSE_MS = 2000;
  // While the service is busy with the piece before, a pause only closes a
  // piece that holds this much. Measured with large-v3-turbo on a laptop CPU,
  // a 3 s piece took 4.7 s to write and a 10 s piece 5.3 s. Pieces cut at
  // every pause would arrive faster than they are written. At this length a
  // piece is written in about half the time it took to say.
  const BUSY_PIECE_MS = 10000;
  // Past SOFT_MAX_MS any short pause will do, and at HARD_MAX_MS the piece is
  // cut wherever it is. Whisper looks at thirty seconds at a time, and the
  // margin leaves room for the padding the server's own VAD adds.
  const SOFT_MAX_MS = 15000;
  const SHORT_PAUSE_MS = 300;
  const HARD_MAX_MS = 28000;
  // Audio kept from before the first word, so that the first syllable is not
  // clipped by the moment it took the gate to open.
  const PREROLL_MS = 300;
  // A piece with less speech than this is a click, a cough or a chair. It is
  // dropped rather than sent.
  const MIN_SPEECH_MS = 200;
  // A microphone left on by mistake switches itself off.
  const IDLE_STOP_MS = 30000;
  // How much of the draft before the insertion point goes along as context.
  // The server keeps only the end of it, and app/dictation.py says why. This
  // just bounds what is sent.
  const CONTEXT_CHARS = 1000;
  // The gate. A batch of samples counts as voiced above HI times the noise
  // floor and as quiet below LO times it. The absolute minimums keep a muted
  // microphone's digital silence from turning every breath into a word.
  const HI = 3, LO = 2;
  const MIN_HI = 0.008, MIN_LO = 0.005;
  // How fast the noise floor follows the signal. Down quickly, so the floor
  // finds the gaps between words. Up slowly, so that speech does not lift it,
  // but a fan that starts up is part of the floor within about ten seconds.
  // Faster at first, while the floor is still a guess from the first batch.
  const FLOOR_DOWN = 0.3, FLOOR_UP = 0.002, FLOOR_UP_WARM = 0.02, WARM_MS = 1500;
  const RATE_OUT = 16000;

  let config = null;        // GET /api/dictation/config. null until it answers
  let editor = null;        // the composer's App.markdown editor
  let el = null;            // #compose-body
  let languageNames = null; // Intl.DisplayNames for the Settings menu, if the browser has it

  // The open microphone, from the press that starts it to the one that stops
  // it. `starting` covers the moment in between, while the browser asks.
  let rec = null;
  let starting = false;

  // Pieces cut and waiting for their text, oldest first. Each is {wav}, a
  // promise of the encoded audio. The first one is on the wire while
  // `inflight` is set.
  let queue = [];
  let inflight = null;      // AbortController of the piece being transcribed
  let failure = "";         // why the last piece could not be written
  let note = "";            // a one-off sentence for the status line
  let noteIsError = false;  // whether that sentence is a refusal or just news

  // Where the next piece's text goes when the caret is not in the draft, and
  // the draft as it was when that was worked out. Between them the insertion
  // point follows edits made elsewhere in the draft (see place()).
  let anchor = -1;
  let lastText = null;
  // The language this draft is spoken in. The setting's until a piece sent
  // as "auto" comes back with text, and from then on the language Whisper
  // found in it. A short piece is the hardest thing to tell a language from,
  // so once one is known it is kept for the rest of the draft.
  let language = "auto";

  let shownLevel = -1;

  function available() {
    return !!(config && config.available && config.enabled && editor);
  }

  function recording() {
    return !!rec || starting;
  }

  // --- The microphone ------------------------------------------------------

  // Why the browser would not hand over the microphone, said so that the
  // writer knows what to do. Pressing the microphone again is the retry.
  function micError(e) {
    const name = e && e.name;
    if (name === "NotAllowedError" || name === "SecurityError") {
      return "The microphone was not allowed. Allow it for this site in the browser's settings, then press the microphone again.";
    }
    if (name === "NotFoundError" || name === "OverconstrainedError") {
      return "No microphone was found. Connect one, then press the microphone again.";
    }
    if (name === "NotReadableError") {
      return "Another program is using the microphone, or the system would not hand it over.";
    }
    return (e && e.message) || "The microphone could not be opened.";
  }

  function release(stream, ctx) {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (ctx) ctx.close().catch(() => {});
  }

  async function start() {
    if (recording() || !available()) return;
    note = "";
    noteIsError = true;
    // The browser only offers the microphone to a secure page, which is
    // https:// or http:// on localhost. Anywhere else the API is missing.
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.AudioWorkletNode) {
      note = window.isSecureContext === false
        ? "The browser only allows the microphone on https:// or on localhost. Open meerail at one of those to dictate."
        : "This browser cannot record from the microphone.";
      render();
      return;
    }
    starting = true;
    render();
    let stream = null, ctx = null;
    try {
      // Made before anything is awaited, while the press still counts as the
      // writer's own gesture. A context made after the permission prompt can
      // start suspended under the browser's autoplay rules, and a suspended
      // context hears nothing.
      ctx = new AudioContext();
      // The browser's own echo cancellation keeps a call or a video playing
      // on the speakers out of the draft. Noise suppression and gain control
      // help the pause detection more than they cost Whisper.
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      await ctx.audioWorklet.addModule("/static/js/app.dictation.worklet.js");
      if (ctx.state === "suspended") await ctx.resume();
    } catch (e) {
      release(stream, ctx);
      if (!starting) return;           // the draft went away while the browser asked
      starting = false;
      note = micError(e);
      render();
      return;
    }
    // Stopped again, or the draft went away, while the browser was asking.
    if (!starting) { release(stream, ctx); return; }
    starting = false;
    // No outputs. The tap only listens, and nothing is played back.
    const node = new AudioWorkletNode(ctx, "meerail-dictation-tap",
      { numberOfInputs: 1, numberOfOutputs: 0 });
    const source = ctx.createMediaStreamSource(stream);
    rec = {
      stream, ctx, node, source,
      rate: ctx.sampleRate,
      floor: -1,              // the noise floor. The first batch sets it
      heardMs: 0,             // audio seen so far, for the warm-up of the floor
      voicedRun: 0,           // voiced batches in a row
      piece: null,            // {chunks, ms, speechMs, quietMs} while a piece is open
      preroll: [], prerollMs: 0,
      lastSpeech: performance.now(),
    };
    node.port.onmessage = (e) => onBatch(e.data);
    source.connect(node);
    // The microphone can also go away on its own, when it is unplugged or the
    // permission is taken back while it records.
    stream.getAudioTracks().forEach((t) => t.addEventListener("ended", () => stop()));
    render();
  }

  // Close the microphone. A piece still open is lost unless cut() ran first.
  function closeMic() {
    const r = rec;
    rec = null;
    if (!r) return;
    r.node.port.onmessage = null;
    try { r.source.disconnect(); } catch (_) { /* already gone with the context */ }
    release(r.stream, r.ctx);
    showLevel(0);
  }

  // The press that ends a dictation. What was said up to it is cut as the last
  // piece and still written. Only the microphone closes now.
  function stop() {
    if (starting) { starting = false; render(); return; }
    if (!rec) return;
    cut();
    closeMic();
    render();
  }

  // The button and Alt+Shift+D. After a failure the same press first sends
  // the pieces that are still waiting, then listens again.
  function toggle() {
    if (!available()) return;
    if (recording()) { stop(); return; }
    if (failure) { failure = ""; pump(); }
    start();
  }

  // --- Cutting at pauses ---------------------------------------------------

  function keepPreroll(r, x, ms) {
    r.preroll.push(x);
    r.prerollMs += ms;
    while (r.preroll.length > 1 && r.prerollMs - r.preroll[0].length / r.rate * 1000 >= PREROLL_MS) {
      r.prerollMs -= r.preroll.shift().length / r.rate * 1000;
    }
  }

  // One batch from the worklet, about 21 ms of audio.
  function onBatch(x) {
    const r = rec;
    if (!r) return;
    const ms = x.length / r.rate * 1000;
    let sum = 0;
    for (let i = 0; i < x.length; i++) sum += x[i] * x[i];
    const rms = Math.sqrt(sum / x.length);
    showLevel(rms);

    r.heardMs += ms;
    if (r.floor < 0) r.floor = Math.min(rms, MIN_LO);
    else if (rms < r.floor) r.floor += (rms - r.floor) * FLOOR_DOWN;
    else r.floor += (rms - r.floor) * (r.heardMs < WARM_MS ? FLOOR_UP_WARM : FLOOR_UP);
    const voiced = rms > Math.max(r.floor * HI, MIN_HI);
    const quiet = rms < Math.max(r.floor * LO, MIN_LO);
    r.voicedRun = voiced ? r.voicedRun + 1 : 0;

    if (!r.piece) {
      keepPreroll(r, x, ms);
      // Two voiced batches in a row open a piece. One alone is more often a
      // click than a word. Both are in the preroll already.
      if (r.voicedRun >= 2) {
        r.piece = { chunks: r.preroll, ms: r.prerollMs, speechMs: 2 * ms, quietMs: 0 };
        r.preroll = [];
        r.prerollMs = 0;
        r.lastSpeech = performance.now();
      } else if (performance.now() - r.lastSpeech > IDLE_STOP_MS) {
        note = "Dictation stopped after 30 seconds of silence.";
        noteIsError = false;
        stop();
      }
      return;
    }

    const p = r.piece;
    // A batch between the two thresholds is neither. It keeps a pause from
    // counting up without being speech itself.
    p.quietMs = quiet ? p.quietMs + ms : 0;
    if (quiet && p.quietMs > LONG_PAUSE_MS) {
      // A piece only gets this far into a pause while it waits for the
      // service, because a free one would have had it cut by now. The rest of
      // the silence is not worth sending, and kept in the piece it would bring
      // HARD_MAX_MS on early and cut the next sentence in the middle. Only its
      // last moment is kept, as the preroll of whatever is said next.
      keepPreroll(r, x, ms);
    } else {
      if (r.preroll.length) {
        p.chunks.push(...r.preroll);
        p.ms += r.prerollMs;
        r.preroll = [];
        r.prerollMs = 0;
      }
      p.chunks.push(x);
      p.ms += ms;
    }
    if (voiced) { p.speechMs += ms; r.lastSpeech = performance.now(); }
    // Checked on every batch, so a piece held back by a busy service is cut
    // within one batch of the service coming free, if it is in a pause by then.
    if (ends(p)) cut();
  }

  // Whether the open piece ends at this batch. The two maximums hold whatever
  // the service is doing. Below them, a busy service raises the bar for an
  // ordinary pause (see BUSY_PIECE_MS). The queue holds the piece on the wire
  // as well as the ones waiting, so an empty queue is a free service.
  function ends(p) {
    if (p.ms >= HARD_MAX_MS) return true;
    if (p.ms >= SOFT_MAX_MS && p.quietMs >= SHORT_PAUSE_MS) return true;
    if (p.quietMs < PAUSE_MS) return false;
    if (queue.length) return p.ms >= BUSY_PIECE_MS;
    return p.speechMs >= MIN_PIECE_SPEECH_MS || p.quietMs >= LONG_PAUSE_MS;
  }

  // Close the open piece and queue it. The encoding runs in the background,
  // and the queue keeps the pieces in the order they were spoken whichever
  // finishes encoding first.
  function cut() {
    const r = rec;
    const p = r && r.piece;
    if (!p) return;
    r.piece = null;
    if (p.speechMs < MIN_SPEECH_MS) return;
    const pcm = new Float32Array(p.chunks.reduce((n, c) => n + c.length, 0));
    let o = 0;
    for (const c of p.chunks) { pcm.set(c, o); o += c.length; }
    queue.push({ wav: encode(pcm, r.rate) });
    render();
    pump();
  }

  async function encode(pcm, rate) {
    if (rate !== RATE_OUT && window.OfflineAudioContext) {
      try {
        const off = new OfflineAudioContext(1, Math.ceil(pcm.length * RATE_OUT / rate), RATE_OUT);
        const buf = off.createBuffer(1, pcm.length, rate);
        buf.copyToChannel(pcm, 0);
        const src = off.createBufferSource();
        src.buffer = buf;
        src.connect(off.destination);
        src.start();
        return wav((await off.startRendering()).getChannelData(0), RATE_OUT);
      } catch (_) {
        // Sent at the rate it was recorded at instead. The service runs
        // everything through ffmpeg, which resamples it, so this only costs
        // upload size. That is three times as much at 48 kHz, and still well
        // under the server's limit for the longest piece.
      }
    }
    return wav(pcm, rate);
  }

  // 16-bit PCM, mono, in a WAV container. Whisper hears nothing above 8 kHz,
  // so nothing that matters is lost on the way.
  function wav(samples, rate) {
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const ascii = (at, s) => { for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i)); };
    ascii(0, "RIFF");
    v.setUint32(4, 36 + samples.length * 2, true);
    ascii(8, "WAVE");
    ascii(12, "fmt ");
    v.setUint32(16, 16, true);          // size of the fmt chunk
    v.setUint16(20, 1, true);           // PCM
    v.setUint16(22, 1, true);           // one channel
    v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true);    // bytes per second
    v.setUint16(32, 2, true);           // bytes per frame
    v.setUint16(34, 16, true);          // bits per sample
    ascii(36, "data");
    v.setUint32(40, samples.length * 2, true);
    for (let i = 0, at = 44; i < samples.length; i++, at += 2) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(at, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([buf], { type: "audio/wav" });
  }

  // --- Writing it into the draft --------------------------------------------

  // The edit that turned `before` into `after`, found as the longest common
  // prefix and suffix, applied to a position in `before`. A position ahead of
  // the edit stays, one after it moves by the change in length, and one inside
  // text that was replaced lands at the end of the replacement.
  function follow(before, after, pos) {
    let p = 0;
    const n = Math.min(before.length, after.length);
    while (p < n && before[p] === after[p]) p++;
    let s = 0;
    while (s < n - p && before[before.length - 1 - s] === after[after.length - 1 - s]) s++;
    if (pos <= p) return pos;
    if (pos >= before.length - s) return pos + after.length - before.length;
    return after.length - s;
  }

  // Where to write when nothing says where. That is after what the writer has
  // typed, ahead of the footer and the quote the composer put in.
  function fallbackPlace(text) {
    let at = text.length;
    for (const [start] of App.compose.notYours(text)) at = Math.min(at, start);
    while (at > 0 && /\s/.test(text[at - 1])) at--;
    return at;
  }

  // Where the next piece goes. At the caret when it is in the draft, which is
  // where the writer is looking and where they put it to say so. Otherwise
  // where the last piece ended, carried along by whatever was edited since.
  function place(text) {
    const caret = editor.caret();
    let at;
    if (caret >= 0) at = caret;
    else if (anchor < 0) at = fallbackPlace(text);
    else at = lastText !== null && lastText !== text ? follow(lastText, text, anchor) : anchor;
    anchor = Math.max(0, Math.min(at, text.length));
    lastText = text;
    return anchor;
  }

  function contextFor(text) {
    const at = place(text);
    return text.slice(Math.max(0, at - CONTEXT_CHARS), at);
  }

  function insert(said) {
    const words = said.trim();
    if (!words) return;
    const text = editor.getText();
    const at = place(text);
    const before = text.slice(0, at), after = text.slice(at);
    // Whisper's own spacing is for a transcript that starts from nothing. In
    // a draft the piece needs a space against the text either side of it,
    // and none against punctuation.
    const lead = before && !/\s$/.test(before) && !/^[.,;:!?…)\]}»"'“”]/.test(words) ? " " : "";
    const trail = after && !/^\s/.test(after) && !/^[.,;:!?…)\]}»]/.test(after) ? " " : "";
    const caret = before.length + lead.length + words.length;
    // The caret only moves when it was in the draft. Pulling it in from the
    // subject line, where somebody may be typing, would put their next key
    // into the body.
    const hadCaret = editor.caret() >= 0;
    // One undoable edit, so Ctrl-Z takes a sentence back like anything typed.
    editor.replaceText(before + lead + words + trail + after, hadCaret ? caret : -1);
    anchor = caret;
    lastText = editor.getText();
    // replaceText repaints without an input event, and the input event is how
    // the composer learns that the draft changed and needs saving.
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // --- Sending the pieces ----------------------------------------------------

  async function pump() {
    if (inflight || failure || !queue.length) return;
    const item = queue[0];
    const ac = new AbortController();
    inflight = ac;
    render();
    let res;
    try {
      const audio = await item.wav;
      if (inflight !== ac) return;     // the draft went away while it encoded
      const fd = new FormData();
      fd.append("audio", audio, "dictation.wav");
      fd.append("language", language);
      const context = contextFor(editor.getText());
      if (context.trim()) fd.append("context", context);
      res = await App.api.dictationTranscribe(fd, ac.signal);
    } catch (e) {
      if (inflight !== ac) return;     // abandoned by onHide, which cleaned up
      inflight = null;
      if (e && e.name === "AbortError") return;
      // The piece stays first in the queue, and so does everything after it.
      // Recording stops, because talking on while nothing gets written would
      // only pile up more to lose. The next press sends what is waiting.
      failure = (e && e.message) || "The recording could not be written.";
      if (rec) { cut(); closeMic(); }
      render();
      return;
    }
    if (inflight !== ac) return;
    inflight = null;
    queue.shift();
    const said = res && typeof res.text === "string" ? res.text : "";
    if (said.trim() && language === "auto" && res && typeof res.language === "string" && res.language !== "auto") {
      language = res.language;
    }
    insert(said);
    render();
    pump();
  }

  // --- On screen -------------------------------------------------------------

  // The microphone's level as a ring around the button, so the writer can see
  // that it hears them. Only repainted when it moved visibly.
  function showLevel(rms) {
    const db = 20 * Math.log10(rms + 1e-9);
    const v = Math.max(0, Math.min(1, (db + 55) / 45));
    if (Math.abs(v - shownLevel) < 0.04) return;
    shownLevel = v;
    $("#compose-dictate").style.setProperty("--mic-level", v.toFixed(2));
  }

  function statusText() {
    const waiting = queue.length;
    if (failure) {
      const left = waiting === 1 ? " One recorded piece is not written yet. Press the microphone to try again."
        : waiting > 1 ? ` ${waiting} recorded pieces are not written yet. Press the microphone to try again.`
        : "";
      return `Dictation stopped. ${failure}${left}`;
    }
    if (starting) return "Opening the microphone…";
    if (rec) return waiting ? "Listening… writing what you said" : "Listening…";
    if (waiting) return "Writing what you said…";
    return note;
  }

  function render() {
    const on = available();
    $("#compose-dictation").hidden = !on;
    document.querySelectorAll('#compose-keys [data-ck="dictation"]').forEach((k) => { k.hidden = !on; });
    const btn = $("#compose-dictate");
    const live = recording();
    btn.setAttribute("aria-pressed", live ? "true" : "false");
    btn.classList.toggle("recording", !!rec);
    btn.classList.toggle("starting", starting);
    btn.classList.toggle("writing", !live && queue.length > 0 && !failure);
    btn.classList.toggle("failed", !!failure);
    btn.title = live ? "Stop dictating (Alt+Shift+D)" : "Dictate (Alt+Shift+D)";
    btn.setAttribute("aria-label", live ? "Stop dictating" : "Dictate");
    const status = $("#compose-dictation-status");
    const text = statusText();
    status.textContent = text;
    status.classList.toggle("error", !!failure || (noteIsError && text === note));
    if (!rec) showLevel(0);
  }

  // --- A draft comes and goes ------------------------------------------------

  // From App.compose's show(). Everything about the last draft's dictation
  // was about that draft, so it all starts over, and the language goes back
  // to the setting.
  function onOpen() {
    hush();
    language = (config && config.language) || "auto";
    render();
  }

  // From close() and minimize(), and from a setting switched off. The
  // microphone closes, and pieces not written yet are dropped. The editor is
  // about to hold another draft, and their text would land in that one.
  function onHide() {
    hush();
    render();
  }

  function hush() {
    starting = false;
    closeMic();
    if (inflight) inflight.abort();
    inflight = null;
    queue = [];
    failure = "";
    note = "";
    noteIsError = false;
    anchor = -1;
    lastText = null;
  }

  // --- Configuration ---------------------------------------------------------

  async function loadConfig() {
    try { config = await App.api.dictationConfig(); } catch (_) { config = null; }
    applyConfig();
  }

  function applyConfig() {
    if (!available()) hush();
    else if (!recording() && App.compose.isOpen()) language = config.language || "auto";
    render();
  }

  // Whisper names Javanese "jw", where everybody else says "jv".
  function languageName(code) {
    if (!languageNames) return code;
    try {
      const name = languageNames.of(code === "jw" ? "jv" : code);
      return name && name !== code ? name : code;
    } catch (_) {
      return code;
    }
  }

  function fillSettings() {
    const ok = !!(config && config.available);
    $("#dictation-settings-form").hidden = !ok;
    $("#dictation-settings-off").hidden = ok;
    if (!ok) return;
    $("#dictation-enabled").checked = !!config.enabled;
    const codes = (config.languages || []).slice()
      .sort((a, b) => languageName(a).localeCompare(languageName(b)));
    const sel = $("#dictation-language");
    sel.innerHTML = `<option value="auto">Detect from the speech</option>` +
      codes.map((c) => `<option value="${App.esc(c)}">${App.esc(languageName(c))}</option>`).join("");
    sel.value = config.language || "auto";
    $("#dictation-words").value = (config.words || []).join("\n");
  }

  async function onSettingsOpen() {
    $("#dictation-save-status").textContent = "";
    await loadConfig();
    fillSettings();
  }

  async function saveSettings() {
    const status = $("#dictation-save-status");
    status.classList.remove("error");
    const payload = {
      enabled: $("#dictation-enabled").checked,
      language: $("#dictation-language").value || "auto",
      words: $("#dictation-words").value.split("\n").map((x) => x.trim()).filter(Boolean),
    };
    status.textContent = "Saving…";
    try {
      config = await App.api.saveDictationConfig(payload);
    } catch (e) {
      status.textContent = e.message || "Could not save";
      status.classList.add("error");
      return;
    }
    status.textContent = "Saved";
    setTimeout(() => { status.textContent = ""; }, 2500);
    fillSettings();
    applyConfig();
  }

  // --- Wiring ----------------------------------------------------------------

  async function init() {
    editor = App.compose.editor();
    el = $("#compose-body");
    if (!editor || !el) return;
    try { languageNames = new Intl.DisplayNames(["en"], { type: "language" }); } catch (_) { languageNames = null; }
    const btn = $("#compose-dictate");
    btn.innerHTML = App.icon("mic", 18);
    // Pressing the button must not take the caret out of the draft first.
    // mousedown is where focus moves, and the click still arrives. The caret
    // is what says where the text goes.
    btn.addEventListener("mousedown", (e) => e.preventDefault());
    btn.addEventListener("click", toggle);
    $("#dictation-save").addEventListener("click", saveSettings);
    await loadConfig();
  }

  return { init, toggle, stop, isRecording: recording, onOpen, onHide, onSettingsOpen };
})();
