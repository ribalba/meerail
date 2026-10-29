/* meerail dictation. The microphone tap, run on the audio thread.

   Loaded by app.dictation.js through audioWorklet.addModule(), never with a
   <script> tag. A worklet module runs in its own global scope with no `App`,
   no DOM and no fetch, so all this file does is hand the samples over. The
   pause detection, the cutting and the sending all happen in app.dictation.js
   on the main thread, where they can be read and changed alongside the rest.

   The audio thread calls process() with 128 frames at a time, which is close
   to four hundred calls a second at 48 kHz. Posting every one of them would
   wake the main thread just as often, so they are gathered into batches of
   BATCH frames first. 1024 frames is about 21 ms at 48 kHz, fine enough for
   telling a pause from a word and coarse enough to cost nothing. */

const BATCH = 1024;

class DictationTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(BATCH);
    this.n = 0;
  }

  process(inputs) {
    // The first channel of the first input. The page asks for a mono stream,
    // and a stereo microphone's second channel is the same voice again.
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    let i = 0;
    while (i < ch.length) {
      const take = Math.min(ch.length - i, BATCH - this.n);
      this.buf.set(ch.subarray(i, i + take), this.n);
      this.n += take;
      i += take;
      if (this.n === BATCH) {
        // Transferred, not copied. The batch belongs to the main thread from
        // here on, and a fresh one is started for the next frames.
        this.port.postMessage(this.buf, [this.buf.buffer]);
        this.buf = new Float32Array(BATCH);
        this.n = 0;
      }
    }
    return true;
  }
}

registerProcessor("meerail-dictation-tap", DictationTap);
