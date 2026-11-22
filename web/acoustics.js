import {instantiate} from './vendor/webrtc-audio-processing/index.js';

export function rms(samples) {
  let sum = 0;
  for (const value of samples) sum += value * value;
  return Math.sqrt(sum / samples.length);
}

class EchoReference {
  constructor() {
    this.reference=new Float32Array(1600);
    this.capture=new Float32Array(240);
    this.power=new Float64Array(1601);
  }
  compare(reference,capture,active,reflections=1) {
    this.reference.copyWithin(0,40);
    this.capture.copyWithin(0,40);
    for(let i=0;i<40;i++) {
      let far=0,near=0;
      for(let j=0;j<8;j++){far+=reference[i*8+j];near+=capture[i*8+j];}
      this.reference[1560+i]=far/8;this.capture[200+i]=near/8;
    }
    if(!active)return 0;
    let energy=0;
    for(const sample of this.capture)energy+=sample*sample;
    if(energy<.0001)return 0;
    for(let i=0;i<1600;i++)this.power[i+1]=this.power[i]+this.reference[i]**2;
    const residual=this.capture.slice();
    let remaining=energy;
    for(let pass=0;pass<reflections;pass++) {
      let best=0,bestAt=0,bestFraction=0,bestGain=0;
      // Search up to 680 ms of changing acoustic delay at 2 kHz, on a 120 ms
      // window. AEC residuals that still match the loudspeaker are not a new user.
      for(let start=0;start<=1360;start++) {
        const farEnergy=this.power[start+240]-this.power[start];
        if(farEnergy<.0001)continue;
        let dot=0;
        for(let i=0;i<240;i++)dot+=residual[i]*this.reference[start+i];
        const score=Math.abs(dot)/Math.sqrt(remaining*farEnergy);
        if(score>best){best=score;bestAt=start;bestGain=dot/farEnergy;}
      }
      // MediaStream/device delays are not integer milliseconds. Refine the best
      // lag instead of mistaking a half-sample phase offset for independent speech.
      const coarseAt=bestAt;
      for(let start=Math.max(0,coarseAt-1);start<=Math.min(1359,coarseAt);start++) {
        for(const fraction of [.25,.5,.75]) {
          let dot=0,farEnergy=0;
          for(let i=0;i<240;i++){
            const ref=this.reference[start+i]*(1-fraction)+this.reference[start+i+1]*fraction;
            dot+=residual[i]*ref;farEnergy+=ref*ref;
          }
          const score=farEnergy>.0001?Math.abs(dot)/Math.sqrt(remaining*farEnergy):0;
          if(score>best){best=score;bestAt=start;bestFraction=fraction;bestGain=dot/farEnergy;}
        }
      }
      if(reflections===1)return best;
      // A room produces several delayed copies, not just one. Remove the best
      // matching copy and measure how much independent sound remains.
      remaining=0;
      for(let i=0;i<240;i++) {
        const ref=this.reference[bestAt+i]*(1-bestFraction)+(this.reference[bestAt+i+1]||0)*bestFraction;
        residual[i]-=bestGain*ref;remaining+=residual[i]**2;
      }
      if(remaining<1e-10)break;
    }
    return Math.sqrt(Math.max(0,1-remaining/energy));
  }
}

// Shared by the AudioWorklet and offline acoustic regression. No cloud calls,
// sample recording or model inference on a remote service takes place here.
export class AcousticInput {
  constructor(module) {
    this.aec = instantiate(module).create({sampleRate:16000, noiseSuppression:'high', vad:2});
    this.cleaned = new Float32Array(320);
    this.captureDelay=[new Float32Array(320),new Float32Array(320)];
    this.echo=new EchoReference();
    this.rawEcho=new EchoReference();
    this.rawHistory=new Float32Array(1344);
    this.farTime=0;
    this.farTail = 0;
  }
  process(mic, reference) {
    // Give the adaptive filter reference look-ahead even for near-zero-delay
    // acoustic paths. Total capture delay is 40 + 24 ms, accounted for upstream.
    const delayed=this.captureDelay.shift();this.captureDelay.push(mic.slice());
    this.aec.cancel(delayed, reference, this.cleaned);
    const stats = this.aec.stats();
    if (stats.errors || stats.nonFiniteSamples) throw new Error('Audio processing failed');
    const referenceRms = rms(reference);
    this.farTail = referenceRms > .001 ? 500 : Math.max(0, this.farTail - 20);
    if(referenceRms>.001)this.farTime+=20;
    this.rawHistory.copyWithin(0,320);this.rawHistory.set(mic,1024);
    const raw=this.rawHistory.subarray(0,320);
    const echoScore=this.echo.compare(reference,this.cleaned,this.farTail>0);
    const rawEchoScore=this.rawEcho.compare(reference,raw,this.farTail>0,this.farTime<1600?3:1);
    const pcm = new Int16Array(320);
    const rawPcm = new Int16Array(320);
    for (let i=0; i<320; i++) pcm[i] = Math.round(Math.max(-1,Math.min(1,this.cleaned[i]))*32767);
    for (let i=0; i<320; i++) rawPcm[i] = Math.round(Math.max(-1,Math.min(1,raw[i]))*32767);
    return {pcm:pcm.buffer, speech:stats.voice === true, playback:this.farTail>0, echo_score:echoScore,
      raw_pcm:rawPcm.buffer,raw_echo_score:rawEchoScore,warming:this.farTime<1600,
      raw_rms:rms(raw), clean_rms:rms(this.cleaned), reference_rms:referenceRms};
  }
  close() { this.aec.destroy(); }
}
