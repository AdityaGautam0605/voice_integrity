/* global AudioWorkletProcessor, registerProcessor, sampleRate */
import { PcmResampler } from './pcm.js';
class CallerPcmProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.resampler = new PcmResampler(sampleRate, options.processorOptions.sampleRate);
  }
  process(inputs) {
    const channels = inputs[0];
    if (channels.length) {
      const mono = new Float32Array(channels[0].length);
      for (const channel of channels) {
        for (let i = 0; i < mono.length; i++) mono[i] += channel[i] / channels.length;
      }
      for (const frame of this.resampler.push(mono)) this.port.postMessage(frame, [frame.buffer]);
    }
    // Silence here: playback has an independent audio element.
    return true;
  }
}
registerProcessor('caller-pcm', CallerPcmProcessor);
