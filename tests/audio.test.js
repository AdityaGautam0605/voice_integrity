import test from 'node:test';
import assert from 'node:assert/strict';
import { PcmResampler, encodeFloat32LE, canSendAudio } from '../src/audio/pcm.js';

function convert(rate, frequency, chunkSize) {
  const resampler = new PcmResampler(rate);
  const signal = Float32Array.from({ length: rate }, (_, i) => .5 * Math.sin(2 * Math.PI * frequency * i / rate));
  const frames = [];
  for (let i = 0; i < signal.length; i += chunkSize) frames.push(...resampler.push(signal.slice(i, i + chunkSize)));
  return frames;
}
for (const rate of [44100, 48000]) {
  test(`${rate} Hz: duration, level and chunk continuity survive resampling`, () => {
    const frames = convert(rate, 440, 128);
    assert.ok(frames.every((frame) => frame.length === 320));
    assert.equal(frames.length, 49); // final incomplete 20 ms packet is retained
    const samples = frames.flatMap((frame) => Array.from(frame));
    const differentlyChunked = convert(rate, 440, 4096).flatMap((frame) => Array.from(frame));
    assert.equal(samples.length, differentlyChunked.length);
    const error = samples.reduce((sum, value, i) => sum + (value - differentlyChunked[i]) ** 2, 0) / samples.length;
    assert.ok(error < 1e-10, `chunk boundary error ${error}`);
    const expectedError = samples.slice(100).reduce((sum, value, i) => sum + (value - .5 * Math.sin(2 * Math.PI * 440 * (i + 100) / 16000)) ** 2, 0) / (samples.length - 100);
    assert.ok(expectedError < 1e-5, `resampled waveform error ${expectedError}`);
  });
}
test('downsampling attenuates frequencies above the target Nyquist limit', () => {
  const samples = convert(48000, 12000, 128).flatMap((frame) => Array.from(frame)).slice(100);
  const rms = Math.sqrt(samples.reduce((sum, x) => sum + x * x, 0) / samples.length);
  assert.ok(rms < .01, `alias level ${rms}`);
});
test('silence remains silence; PCM has an explicit little-endian representation', () => {
  const frames = new PcmResampler(48000).push(new Float32Array(48000));
  assert.ok(frames.every((frame) => frame.every((value) => value === 0)));
  const bytes = encodeFloat32LE(new Float32Array([0, 1, -.5]));
  assert.deepEqual([...new Uint8Array(bytes)], [0,0,0,0,0,0,128,63,0,0,0,191]);
});
test('backpressure admits at most one second of queued audio', () => {
  assert.equal(canSendAudio({ readyState: 1, bufferedAmount: 62720 }, 1280, 16000), true);
  assert.equal(canSendAudio({ readyState: 1, bufferedAmount: 64000 }, 1280, 16000), false);
  assert.equal(canSendAudio({ readyState: 3, bufferedAmount: 0 }, 1280, 16000), false);
});
