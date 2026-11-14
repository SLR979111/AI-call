import {AcousticInput} from './acoustics.js';

// Resample capture and the actual scheduled speaker output on the same clock,
// then run AEC3, noise suppression and VAD before sending anything upstream.
class Capture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.acoustics = new AcousticInput(options.processorOptions.module);
    this.ratio = sampleRate / 16000;
    this.left = this.ratio;
    this.sum = 0;
    this.referenceSum = 0;
    this.frame = new Float32Array(320);
    this.reference = new Float32Array(320);
    this.offset = 0;
    this.closed = false;
    this.port.onmessage = ({data}) => {
      if (data.type === 'close' && !this.closed) { this.closed=true; this.acoustics.close(); }
    };
    this.port.postMessage({type:'ready'});
  }
  process(inputs, outputs) {
    if (this.closed) return false;
    const input = inputs[0]?.[0];
    const reference = inputs[1]?.[0];
    // Keep feeding reference and silence even before the microphone produces
    // its first block. AEC must see far-end audio before its acoustic return.
    const length = input?.length || reference?.length || outputs[0]?.[0]?.length || 128;
    for (let i=0; i<length; i++) {
      const sample = input?.[i] || 0;
      const far = reference?.[i] || 0;
      let remaining = 1;
      while (remaining > 1e-9) {
        const weight = Math.min(remaining, this.left);
        this.sum += sample * weight;
        this.referenceSum += far * weight;
        this.left -= weight;
        remaining -= weight;
        if (this.left < 1e-9) {
          this.frame[this.offset] = this.sum / this.ratio;
          this.reference[this.offset++] = this.referenceSum / this.ratio;
          this.left = this.ratio;
          this.sum = 0;
          this.referenceSum = 0;
          if (this.offset === 320) {
            try {
              const processed = this.acoustics.process(this.frame, this.reference);
              this.port.postMessage({type:'audio', ...processed}, [processed.pcm,processed.raw_pcm]);
            } catch {
              this.port.postMessage({type:'error'});
              this.acoustics.close(); this.closed=true;
              return false;
            }
            this.offset = 0;
          }
        }
      }
    }
    // Connected to the output to keep the worklet running; never monitor the mic.
    for (const channel of outputs[0] || []) channel.fill(0);
    return true;
  }
}
registerProcessor('capture-16k', Capture);
