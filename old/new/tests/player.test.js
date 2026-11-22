import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Player, Microphone} from '../web/audio.js';

function context() {
  return {currentTime:0,destination:{},createGain:()=>({connect(){},disconnect(){}}),
    createBuffer:(_,count)=>({getChannelData:()=>new Float32Array(count)}),
    createBufferSource:()=>({connect(){},disconnect(){},start(){},stop(){}})};
}
function packet(epoch,unit,seq,count=960) {
  const data=new ArrayBuffer(12+count*2), view=new DataView(data);
  view.setUint32(0,epoch,true);view.setUint32(4,unit,true);view.setUint32(8,seq,true);
  return data;
}
test('canceled audio and a late cancel cannot affect the new response',()=>{
  const ctx=context(), events=[], p=new Player(ctx,e=>events.push(e));
  p.begin(1);p.unit({epoch:1,unit:1});p.packet(packet(1,1,0));p.cancel(1);
  p.packet(packet(1,1,1));assert.equal(p.units.size,0);
  p.begin(2);p.unit({epoch:2,unit:1});p.packet(packet(2,1,0));p.cancel(1);
  assert.equal(p.units.get(1).received,960);p.close();
});
test('a network gap is not counted as played audio',()=>{
  const ctx=context(), events=[], p=new Player(ctx,e=>events.push(e));
  p.begin(1);p.unit({epoch:1,unit:1});p.packet(packet(1,1,0));
  ctx.currentTime=1;p.packet(packet(1,1,1));
  assert.equal(p.progress()[0].samples,960);
  p.finish({epoch:1,unit:1,samples:1920});
  assert.equal(events.some(e=>e.type==='played' && e.done),false);
  ctx.currentTime=2;p.report();
  assert.equal(events.filter(e=>e.type==='played').at(-1).samples,1920);
  assert.equal(events.filter(e=>e.type==='played').at(-1).done,true);p.close();
});

test('capture transports cleaned audio and VAD evidence without cancelling playback locally',()=>{
  globalThis.WebSocket = {OPEN:1};
  const packets=[], socket={readyState:1,bufferedAmount:0,send:p=>packets.push(p)};
  const mic = new Microphone(context(), socket, ()=>{}, {});
  for(let i=0;i<30;i++) mic.frame({pcm:new Int16Array(320).fill(3000).buffer,
    raw_pcm:new Int16Array(320).fill(4000).buffer,
    speech:false, playback:true, raw_rms:.1, clean_rms:.09, reference_rms:.1,echo_score:.9,raw_echo_score:.95});
  assert.equal(packets.length,30);
  assert.ok(packets.every(p=>p instanceof ArrayBuffer && p.byteLength===1316));
  assert.equal(new DataView(packets[0]).getUint32(12,true),2);
});

test('muting clears both cleaned audio and the speech decision',()=>{
  globalThis.WebSocket = {OPEN:1};
  const packets=[], socket={readyState:1,bufferedAmount:0,send:p=>packets.push(p)};
  const mic = new Microphone(context(), socket, ()=>{}, {});
  mic.muted=true;
  mic.frame({pcm:new Int16Array(320).fill(400).buffer, raw_pcm:new Int16Array(320).fill(800).buffer,
    speech:true,playback:false,raw_rms:.1,clean_rms:.01,reference_rms:0,echo_score:0,raw_echo_score:0});
  assert.equal(new DataView(packets[0]).getUint32(12,true),0);
  assert.ok(new Int16Array(packets[0],36).every(s=>s===0));
});
