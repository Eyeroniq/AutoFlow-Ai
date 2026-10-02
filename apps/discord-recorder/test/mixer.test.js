import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Mixer, SAMPLE_RATE } from '../src/mixer.js';

/** 48 kHz stereo 16-bit PCM of a constant level, `seconds` long. */
function tone(seconds, level) {
  const frames = Math.round(48000 * seconds);
  const buffer = Buffer.alloc(frames * 4);
  for (let i = 0; i < frames; i += 1) {
    buffer.writeInt16LE(level, i * 4);
    buffer.writeInt16LE(level, i * 4 + 2);
  }
  return buffer;
}

function samplesOf(wav) {
  return new Int16Array(wav.buffer.slice(wav.byteOffset + 44, wav.byteOffset + wav.length));
}

function clocked() {
  const state = { now: 1_000_000 };
  const mixer = new Mixer(() => state.now);
  mixer.begin();
  return { mixer, state };
}

test('writes a mono 16 kHz 16-bit WAV with a correct header', () => {
  const { mixer, state } = clocked();
  state.now += 1000;
  mixer.add(1, tone(0.02, 1000));
  mixer.end();
  const wav = mixer.toWav();
  assert.equal(wav.toString('ascii', 0, 4), 'RIFF');
  assert.equal(wav.toString('ascii', 8, 12), 'WAVE');
  assert.equal(wav.readUInt16LE(22), 1, 'mono');
  assert.equal(wav.readUInt32LE(24), SAMPLE_RATE);
  assert.equal(wav.readUInt16LE(34), 16);
  assert.equal(wav.readUInt32LE(40), wav.length - 44);
  assert.equal(samplesOf(wav).length, SAMPLE_RATE, 'padded to the one second of wall-clock time');
});

test('speakers are summed and loud overlap is clipped, not wrapped', () => {
  const { mixer, state } = clocked();
  state.now += 20;
  mixer.add(1, tone(0.02, 3000));
  mixer.add(2, tone(0.02, 2000));
  mixer.end();
  assert.equal(samplesOf(mixer.toWav())[10], 5000);

  const loud = clocked();
  loud.state.now += 20;
  loud.mixer.add(1, tone(0.02, 30000));
  loud.mixer.add(2, tone(0.02, 30000));
  loud.mixer.end();
  assert.equal(samplesOf(loud.mixer.toWav())[10], 32767);
});

test('a pause stays silent and later speech lands after it', () => {
  const { mixer, state } = clocked();
  for (let n = 0; n < 50; n += 1) {
    state.now += 20;
    mixer.add(1, tone(0.02, 3000)); // 1 s of speech
  }
  state.now += 2000; // two seconds of nothing
  for (let n = 0; n < 25; n += 1) {
    state.now += 20;
    mixer.add(1, tone(0.02, 3000));
  }
  mixer.end();
  const samples = samplesOf(mixer.toWav());
  assert.equal(samples.length, Math.round(mixer.seconds * SAMPLE_RATE));
  assert.ok(Math.abs(samples[SAMPLE_RATE / 2]) === 3000, 'speech at 0.5 s');
  assert.equal(Math.max(...samples.slice(Math.round(1.2 * SAMPLE_RATE), Math.round(2.8 * SAMPLE_RATE)).map(Math.abs)), 0, 'the pause is silent');
  assert.ok(Math.abs(samples[Math.round(3.2 * SAMPLE_RATE)]) === 3000, 'speech resumes after the pause');
});

test('frames that arrive back to back are joined without gaps', () => {
  const { mixer, state } = clocked();
  for (let n = 0; n < 10; n += 1) {
    state.now += 20;
    mixer.add(1, tone(0.02, 1500));
  }
  mixer.end();
  const samples = samplesOf(mixer.toWav());
  assert.ok(samples.slice(0, 10 * 320).every((s) => s === 1500));
  assert.equal(mixer.frames, 10);
  assert.ok(Math.abs(mixer.speechSeconds - 0.2) < 1e-9);
});

test('hasAudio is false for an empty recording and true after speech', () => {
  const { mixer, state } = clocked();
  assert.equal(mixer.hasAudio(), false);
  state.now += 20;
  mixer.add(1, tone(0.02, 800));
  assert.equal(mixer.hasAudio(), true);
});

test('incomplete frames are ignored and a long recording grows past one chunk', () => {
  const { mixer, state } = clocked();
  mixer.add(1, Buffer.alloc(8)); // fewer than 3 stereo frames
  assert.equal(mixer.frames, 0);
  state.now += 61_000;
  mixer.add(1, tone(0.02, 500));
  mixer.end();
  assert.equal(samplesOf(mixer.toWav()).length, 61 * SAMPLE_RATE);
});
