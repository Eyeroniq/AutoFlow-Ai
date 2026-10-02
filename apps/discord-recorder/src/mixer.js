// Mixes every speaker into one mono 16 kHz track on a shared timeline.
//
// `add` takes decoded PCM (48 kHz, stereo, 16-bit little endian: what an Opus decoder gives for a
// Discord voice packet) with the time it arrived. A speaker's frames are laid end to end; when the
// next frame arrives clearly later than the previous one ended, the speaker was silent or packets
// were lost, so it is placed at its arrival time and the gap stays silent. Samples are summed and
// clipped. The WAV is padded with silence to the recording's wall-clock length.

export const SAMPLE_RATE = 16000;
const SOURCE_RATE = 48000;
const DECIMATE = SOURCE_RATE / SAMPLE_RATE; // 3
const CHUNK = SAMPLE_RATE * 60; // grow by a minute of samples
// A frame later than this after the previous one ended starts a new stretch (two lost frames).
const GAP_SECONDS = 0.05;

export class Mixer {
  constructor(clock = Date.now) {
    this.clock = clock;
    this.chunks = []; // Int16Array pieces of CHUNK samples
    this.cursors = new Map(); // user id -> next sample position
    this.start = null;
    this.stop = null;
    this.frames = 0;
    this.speechSamples = 0;
    this.length = 0; // furthest sample written
  }

  begin() {
    this.start = this.clock();
  }

  end() {
    if (this.stop === null) this.stop = this.clock();
  }

  /** Wall-clock length in seconds (to now while it runs). */
  get seconds() {
    if (this.start === null) return 0;
    return ((this.stop ?? this.clock()) - this.start) / 1000;
  }

  /** Seconds of speech received, summed over speakers. */
  get speechSeconds() {
    return this.speechSamples / SAMPLE_RATE;
  }

  add(userId, pcm, now = this.clock()) {
    // 3840 bytes = 960 stereo frames of 16-bit; anything that isn't whole 3-frame groups is cut.
    const stereoFrames = Math.floor(pcm.length / 4 / DECIMATE) * DECIMATE;
    const count = stereoFrames / DECIMATE;
    if (count === 0) return;
    if (this.start === null) this.start = now;
    const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
    const mono = new Int32Array(count);
    for (let i = 0; i < count; i += 1) {
      let sum = 0;
      for (let k = 0; k < DECIMATE; k += 1) {
        const at = (i * DECIMATE + k) * 4;
        sum += view.getInt16(at, true) + view.getInt16(at + 2, true);
      }
      mono[i] = Math.round(sum / (DECIMATE * 2)); // the mean of 3 frames x 2 channels
    }
    const arrived = Math.round(((now - this.start) / 1000) * SAMPLE_RATE);
    const nominal = Math.max(arrived - count, 0);
    let cursor = this.cursors.get(userId);
    if (cursor === undefined || nominal - cursor > GAP_SECONDS * SAMPLE_RATE) cursor = nominal;
    this.#mixIn(cursor, mono);
    this.cursors.set(userId, cursor + count);
    this.length = Math.max(this.length, cursor + count);
    this.frames += 1;
    this.speechSamples += count;
  }

  #mixIn(position, samples) {
    const end = position + samples.length;
    while (this.chunks.length * CHUNK < end) this.chunks.push(new Int16Array(CHUNK));
    for (let i = 0; i < samples.length; i += 1) {
      const at = position + i;
      const chunk = this.chunks[Math.floor(at / CHUNK)];
      const slot = at % CHUNK;
      const mixed = chunk[slot] + samples[i];
      chunk[slot] = mixed > 32767 ? 32767 : mixed < -32768 ? -32768 : mixed;
    }
  }

  /** True if anything audible was recorded. */
  hasAudio() {
    return this.chunks.some((chunk) => chunk.some((sample) => sample !== 0));
  }

  /** The recording as a 16-bit mono WAV file. */
  toWav() {
    const total = Math.max(this.length, Math.round(this.seconds * SAMPLE_RATE));
    const data = Buffer.alloc(total * 2);
    for (let i = 0; i < this.length; i += 1) {
      data.writeInt16LE(this.chunks[Math.floor(i / CHUNK)][i % CHUNK], i * 2);
    }
    const header = Buffer.alloc(44);
    header.write('RIFF', 0);
    header.writeUInt32LE(36 + data.length, 4);
    header.write('WAVEfmt ', 8);
    header.writeUInt32LE(16, 16); // PCM header size
    header.writeUInt16LE(1, 20); // PCM
    header.writeUInt16LE(1, 22); // mono
    header.writeUInt32LE(SAMPLE_RATE, 24);
    header.writeUInt32LE(SAMPLE_RATE * 2, 28);
    header.writeUInt16LE(2, 32);
    header.writeUInt16LE(16, 34);
    header.write('data', 36);
    header.writeUInt32LE(data.length, 40);
    return Buffer.concat([header, data]);
  }
}
