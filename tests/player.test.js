import {test} from 'node:test';
import assert from 'node:assert/strict';
import {CallConnection} from '../web/connection.js';
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
test('an interrupt fades the active utterance and drops its queued packets', () => {
  const ctx = context(), gains = [], sources = [];
  ctx.createGain = () => {
    const gain = {connect(){}, disconnect(){}, events:[], gain:{
      setValueAtTime(value, time) { gain.events.push([value, time]); },
      linearRampToValueAtTime(value, time) { gain.events.push([value, time]); }
    }};
    gains.push(gain);
    return gain;
  };
  ctx.createBufferSource = () => {
    const node = {connect(){}, disconnect(){}, start(time) { node.startAt=time; },
      stop(time) { node.stopAt=time ?? 0; }};
    sources.push(node);
    return node;
  };
  const player = new Player(ctx, () => {});
  try {
    player.begin(1); player.unit({epoch:1, unit:1});
    for (let i=0;i<6;i++) player.packet(packet(1,1,i));
    ctx.currentTime = .08;
    const [receipt] = player.stop(true);
    assert.equal(receipt.samples, 1320);
    assert.deepEqual(gains[1].events, [[1,.08],[0,.16]]);
    assert.equal(sources[3].stopAt,.16);
    assert.equal(sources[4].stopAt,0);
    player.packet(packet(1,1,6));
    assert.equal(sources.length,6);
    player.begin(2); player.unit({epoch:2,unit:1}); player.packet(packet(2,1,0));
    assert.ok(sources[6].startAt >= .16);
    player.cancel(1);
    assert.deepEqual(gains[2].events,[]);
  } finally { player.close(); }
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

test('dial waits for a capture frame from a live unmuted microphone, including silence', async t => {
  const track = {readyState: 'live', muted: true, stop() {}};
  const stream = {getAudioTracks: () => [track], getTracks: () => [track]};
  let attach;
  const attached = new Promise(resolve => { attach = resolve; });
  const originals = new Map();
  function replace(name, value) {
    originals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, {value, configurable: true, writable: true});
  }
  t.after(() => {
    for (const [name, previous] of originals) {
      if (previous) Object.defineProperty(globalThis, name, previous);
      else delete globalThis[name];
    }
  });
  replace('fetch', async () => ({ok: true, arrayBuffer: async () =>
    Uint8Array.of(0, 97, 115, 109, 1, 0, 0, 0).buffer}));
  replace('navigator', {mediaDevices: {getUserMedia: async () => stream}});
  replace('AudioWorkletNode', class {
    port = {postMessage() {}};
    connect() { attach(this.port); }
    disconnect() {}
  });
  const ctx = {...context(), audioWorklet: {async addModule() {}},
    createMediaStreamSource: () => ({connect() {}, disconnect() {}})};
  const speaker = {connect() {}, disconnect() {}};
  const mic = new Microphone(ctx, {readyState: 3}, () => {}, speaker);
  const opening = mic.open();
  let ready = false;
  opening.then(() => { ready = true; }, () => {});
  try {
    const port = await attached;
    const audio = hasInput => port.onmessage({data: {type: 'audio', hasInput}});
    port.onmessage({data: {type: 'ready'}});
    audio(true);
    await new Promise(setImmediate);
    assert.equal(ready, false);

    track.muted = false;
    audio(false);
    await new Promise(setImmediate);
    assert.equal(ready, false);

    audio(true);
    await opening;
    assert.equal(ready, true);
  } finally { mic.close(); }
});

test('visible speech activity can appear over the opening while its echo filter warms', () => {
  const levels = [];
  const mic = new Microphone(context(), {readyState: 3}, () => {}, {}, level => levels.push(level));
  const frame = {warming: true, playback: true, speech: true, clean_rms: .02};
  mic.frame({...frame, echo_score: .9});
  mic.frame({...frame, echo_score: .2});
  assert.deepEqual(levels.map(level => level.active), [false, true]);
});

test('a cancelled connection settles promptly and ignores its former close handler', async t => {
  const previousLocation = globalThis.location;
  const previousSocket = globalThis.WebSocket;
  let closed = 0;
  class Socket {
    static OPEN = 1;
    static CLOSING = 2;
    readyState = 0;
    close() { this.readyState = 3; }
  }
  globalThis.location = new URL('https://example.test/call');
  globalThis.WebSocket = Socket;
  t.after(() => { globalThis.location = previousLocation; globalThis.WebSocket = previousSocket; });
  const options = {onClose: () => closed++, onMessage() {}, onError() {}};
  const first = new CallConnection(options);
  const pending = first.open();
  await first.close();
  await assert.rejects(pending, {name:'AbortError'});
  const second = new CallConnection(options);
  const opening = second.open();
  second.socket.readyState = Socket.OPEN;
  second.socket.onopen();
  await opening;
  const late = second.socket.onclose;
  await second.close();
  late();
  assert.equal(closed, 0);
});
