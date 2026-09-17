// Stateful low-pass resampling, independent of the hardware sample rate.
export class PcmResampler {
  constructor(inputRate, outputRate = 16000, frameSize = outputRate / 50) {
    this.ratio = inputRate / outputRate;
    this.cutoff = 0.45 * Math.min(1, outputRate / inputRate);
    this.half = 24;
    this.samples = new Float32Array(this.half);
    this.position = this.half;
    this.frame = new Float32Array(frameSize);
    this.used = 0;
  }
  push(input) {
    const combined = new Float32Array(this.samples.length + input.length);
    combined.set(this.samples);
    combined.set(input, this.samples.length);
    const frames = [];
    while (this.position + this.half < combined.length) {
      let value = 0, weight = 0;
      const base = Math.floor(this.position);
      for (let i = base - this.half + 1; i <= base + this.half; i++) {
        const distance = i - this.position;
        const x = 2 * this.cutoff * distance;
        const sinc = Math.abs(x) < 1e-8 ? 1 : Math.sin(Math.PI * x) / (Math.PI * x);
        const coefficient = 2 * this.cutoff * sinc * (0.5 + 0.5 * Math.cos(Math.PI * distance / this.half));
        value += combined[i] * coefficient;
        weight += coefficient;
      }
      this.frame[this.used++] = Math.max(-1, Math.min(1, value / weight));
      this.position += this.ratio;
      if (this.used === this.frame.length) {
        frames.push(this.frame);
        this.frame = new Float32Array(this.frame.length);
        this.used = 0;
      }
    }
    const consumed = Math.max(0, Math.floor(this.position) - this.half);
    this.samples = combined.slice(consumed);
    this.position -= consumed;
    return frames;
  }
}
export function encodeFloat32LE(samples) {
  const bytes = new ArrayBuffer(samples.length * 4);
  const view = new DataView(bytes);
  samples.forEach((value, index) => view.setFloat32(index * 4, value, true));
  return bytes;
}
export function canSendAudio(socket, nextBytes, sampleRate) {
  return socket.readyState === 1 && socket.bufferedAmount + nextBytes <= sampleRate * 4;
}
