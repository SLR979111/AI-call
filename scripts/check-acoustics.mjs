// Synthetic acoustic mixtures through the SAME DSP used in the AudioWorklet.
import fs from 'node:fs/promises';
import path from 'node:path';
import {AcousticInput, rms} from '../web/acoustics.js';

const directory=path.resolve('runtime/validation/acoustics');
await fs.mkdir(directory,{recursive:true});
const module=await WebAssembly.compile(await fs.readFile('web/vendor/webrtc-audio-processing/webrtc-audio-processing.wasm'));
async function fixture(name) {
  const b=await fs.readFile(`runtime/validation/${name}.pcm`);
  const out=new Float32Array(Math.floor(b.length/3));
  for(let i=0;i<out.length;i++) {
    const at=i*1.5, a=Math.floor(at), t=at-a;
    out[i]=((1-t)*b.readInt16LE(a*2)+t*b.readInt16LE(Math.min(a+1,b.length/2-1)*2))/32768;
  }
  return out;
}
const far=await fixture('jd-opening'), near=await fixture('user-interrupt');
const cases=[];
for(const delay of [0,40,120,300]) for(const gain of [.3,.8,1.2])
  cases.push({name:`echo_${delay}ms_${gain}`,delay,gain,noise:.006});
cases.push(
  {name:'echo_delay_change',delay:100,gain:.7,noise:.006,delayChange:240},
  {name:'echo_volume_change',delay:100,gain:.3,noise:.006,gainChange:1.1},
  {name:'echo_clipped',delay:120,gain:1.5,noise:.006,clip:.18},
  {name:'fan',gain:0,noise:.012,fan:true},
  {name:'clicks',gain:0,clicks:true},
  {name:'near_with_noise',gain:0,noise:.012,nearGain:.7,nearAt:3},
  {name:'quiet_near',gain:0,noise:.003,nearGain:.15,nearAt:3},
  {name:'double_talk',delay:120,gain:.8,noise:.006,nearGain:.7,nearAt:3},
  {name:'early_double_talk',delay:120,gain:.8,noise:.006,nearGain:.7,nearAt:.2},
);
const results=[];
for(const config of cases) {
  const dsp=new AcousticInput(module), packets=[], rawPcm=[], cleanPcm=[];
  let seed=777, inputPower=0, outputPower=0, firstNearMs=null;
  const random=()=>{seed=(seed*1664525+1013904223)>>>0;return seed/4294967296*2-1;};
  const start=performance.now();
  for(let at=0,seq=0;at<16000*12;at+=320,seq++) {
    const mic=new Float32Array(320), reference=new Float32Array(320), local=new Float32Array(320);
    for(let i=0;i<320;i++) {
      const n=at+i;
      const delay=Math.round((n>=64000 && config.delayChange!==undefined ? config.delayChange : config.delay||0)*16);
      const gain=n>=64000 && config.gainChange!==undefined ? config.gainChange : config.gain;
      reference[i]=config.gain ? far[n]||0 : 0;
      let echo=((far[n-delay]||0)+.45*(far[n-delay-480]||0)+.18*(far[n-delay-1100]||0))*gain;
      if(config.clip) echo=Math.max(-config.clip,Math.min(config.clip,echo));
      local[i]=(near[n-Math.round((config.nearAt||0)*16000)]||0)*(config.nearGain||0);
      let value=echo+random()*(config.noise||0)+local[i];
      if(config.fan) value+=.06*Math.sin(n*2*Math.PI*120/16000)+.025*Math.sin(n*2*Math.PI*180/16000);
      if(config.clicks && n%8000<800) value+=random()*.3;
      mic[i]=Math.max(-1,Math.min(1,value));
    }
    if(firstNearMs===null && rms(local)>.004) firstNearMs=at/16;
    const frame=dsp.process(mic,reference);
    const packet=Buffer.alloc(1316);
    packet.writeUInt32LE(seq,0);packet.writeDoubleLE(at/16,4);
    packet.writeUInt32LE((frame.speech?1:0)|(frame.playback?2:0)|(frame.warming?4:0),12);
    packet.writeFloatLE(frame.raw_rms,16);packet.writeFloatLE(frame.clean_rms,20);packet.writeFloatLE(frame.reference_rms,24);
    packet.writeFloatLE(frame.echo_score,28);
    packet.writeFloatLE(frame.raw_echo_score,32);
    Buffer.from(frame.pcm).copy(packet,36);
    Buffer.from(frame.raw_pcm).copy(packet,676);
    packets.push(packet);
    const raw=new Int16Array(320);
    for(let i=0;i<320;i++){raw[i]=Math.round(mic[i]*32767);inputPower+=mic[i]**2;outputPower+=dsp.cleaned[i]**2;}
    rawPcm.push(Buffer.from(raw.buffer));cleanPcm.push(Buffer.from(frame.pcm));
  }
  await fs.writeFile(path.join(directory,config.name+'.frames'),Buffer.concat(packets));
  if(config.nearGain) {
    await fs.writeFile(path.join(directory,config.name+'-clean.pcm'),Buffer.concat(cleanPcm));
    await fs.writeFile(path.join(directory,config.name+'-raw.pcm'),Buffer.concat(rawPcm));
  }
  const result={...config,expectsSpeech:!!config.nearGain,firstNearMs,
    reductionDb:Math.round(10*Math.log10(inputPower/Math.max(outputPower,1e-12))*10)/10,
    processingMs:Math.round(performance.now()-start),stats:dsp.aec.stats()};
  results.push(result);dsp.close();
  console.log(JSON.stringify(result));
}
await fs.writeFile(path.join(directory,'dsp.json'),JSON.stringify(results,null,2));
