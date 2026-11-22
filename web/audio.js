let acousticModule;
const INTERRUPTION_FADE_SECONDS = 0.08;

export class Player {
  constructor(context, send, onDone = () => {}) {
    this.context = context;
    this.send = send;
    this.onDone = onDone;
    this.output = context.createGain();
    this.output.connect(context.destination);
    this.voiceGain = null;
    this.fading = new Set();
    this.epoch = 0;
    this.blocked = new Set();
    this.units = new Map();
    this.nextAt = 0;
    this.timer = setInterval(() => this.report(), 70);
  }
  begin(epoch) {
    if (epoch < this.epoch || this.blocked.has(epoch)) return;
    this.stop();
    this.epoch = epoch;
    for (const tail of this.fading) this.nextAt = Math.max(this.nextAt, tail.end);
    this.voiceGain = this.context.createGain();
    this.voiceGain.connect(this.output);
  }
  unit(event) {
    if (event.epoch !== this.epoch || this.blocked.has(event.epoch)) return;
    this.units.set(event.unit, {id:event.unit, segments:[], received:0, seq:0, total:null, done:false});
  }
  packet(buffer) {
    if (buffer.byteLength < 14 || buffer.byteLength % 2) return;
    const view = new DataView(buffer);
    const epoch = view.getUint32(0, true), id = view.getUint32(4, true), seq = view.getUint32(8, true);
    if (epoch !== this.epoch || this.blocked.has(epoch)) return;
    const unit = this.units.get(id);
    if (!unit || seq !== unit.seq) return;
    unit.seq++;
    const count = (buffer.byteLength - 12) / 2;
    const audio = this.context.createBuffer(1, count, 24000);
    const data = audio.getChannelData(0);
    for (let i=0; i<count; i++) data[i] = view.getInt16(12 + i*2, true) / 32768;
    const source = this.context.createBufferSource();
    source.buffer = audio;
    source.connect(this.voiceGain);
    const start = Math.max(this.context.currentTime + .025, this.nextAt);
    const end = start + count / 24000;
    unit.segments.push({source, start, end, count});
    unit.received += count;
    this.nextAt = end;
    source.onended = () => { try { source.disconnect(); } catch {} };
    source.start(start);
  }
  finish(event) {
    if (event.epoch !== this.epoch || this.blocked.has(event.epoch)) return;
    const unit = this.units.get(event.unit);
    if (unit && event.samples === unit.received) unit.total = event.samples;
    this.report();
  }
  progress() {
    const now = this.context.currentTime;
    return [...this.units.values()].filter(u => !u.done).map(unit => ({
      unit: unit.id,
      samples: unit.segments.reduce((n, segment) => n + Math.min(segment.count,
        Math.max(0, Math.floor((now - segment.start) * 24000 + .01))), 0),
    }));
  }
  isPlaying() {
    const now = this.context.currentTime;
    // Include a short acoustic tail after scheduled playback ends.
    return [...this.fading].some(tail => now < tail.end) || [...this.units.values()].some(u => u.segments.some(s =>
      now >= s.start - .03 && now < s.end + .15));
  }
  report() {
    if (this.blocked.has(this.epoch)) return;
    const now = this.context.currentTime;
    for (const unit of this.units.values()) {
      if (unit.startedReported) continue;
      const start = unit.segments.find(s => now >= s.start);
      if (start) {
        unit.startedReported = true;
        this.send({type:'playback_started', epoch:this.epoch, unit:unit.id,
          client_ms:performance.now() - (now-start.start)*1000});
      }
    }
    for (const progress of this.progress()) {
      const unit = this.units.get(progress.unit);
      const done = unit.total !== null && progress.samples === unit.total;
      this.send({type:'played', epoch:this.epoch, ...progress, done});
      if (done) { unit.done = true; this.onDone(this.epoch, unit.id); }
    }
  }
  stop(block = false, fade = block) {
    const progress = this.progress();
    if (block && this.epoch) this.blocked.add(this.epoch);
    const gain = this.voiceGain;
    const now = this.context.currentTime;
    const segments = [...this.units.values()].flatMap(unit => unit.segments);
    const end = now + INTERRUPTION_FADE_SECONDS;
    const audible = fade && segments.some(s => s.start <= now && now < s.end);
    const tail = {gain, end, sources: new Set()};
    if (audible) {
      gain.gain.setValueAtTime(1, now);
      gain.gain.linearRampToValueAtTime(0, end);
      this.fading.add(tail);
    }
    for (const {source, start, end: segmentEnd} of segments) {
      if (audible && segmentEnd > now && start < end) {
        tail.sources.add(source);
        source.onended = () => {
          source.disconnect();
          tail.sources.delete(source);
          if (!tail.sources.size) { gain.disconnect(); this.fading.delete(tail); }
        };
        source.stop(Math.min(end, segmentEnd));
      } else {
        try { source.stop(); source.disconnect(); } catch {}
      }
    }
    if (!audible) gain?.disconnect();
    this.voiceGain = null;
    this.units.clear();
    this.nextAt = 0;
    return progress;
  }
  cancel(epoch) { if (epoch === this.epoch) this.stop(true); else this.blocked.add(epoch); }
  close() {
    clearInterval(this.timer);
    this.stop(true, false);
    for (const {gain, sources} of this.fading) {
      for (const source of sources) { source.stop(); source.disconnect(); }
      gain.disconnect();
    }
    this.fading.clear();
    this.output.disconnect();
  }
}

export class Microphone {
  constructor(context, socket, onError, playbackNode, onLevel = () => {}) {
    this.context = context;
    this.socket = socket;
    this.onError = onError;
    this.playbackNode = playbackNode;
    this.onLevel = onLevel;
    this.seq = 0;
    this.muted = false;
    this.closed = false;
  }
  async open() {
    if (!acousticModule) {
      const response = await fetch('/static/vendor/webrtc-audio-processing/webrtc-audio-processing.wasm');
      if (!response.ok) throw new Error('消回声组件未能载入，请刷新后重试。');
      acousticModule = await WebAssembly.compile(await response.arrayBuffer());
    }
    this.checkOpen();
    const stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,
      echoCancellation:true, noiseSuppression:false, autoGainControl:false}, video:false});
    if (this.closed) stream.getTracks().forEach(track => track.stop());
    this.checkOpen();
    this.stream = stream;
    await this.context.audioWorklet.addModule('/static/capture.js');
    this.checkOpen();
    this.source = this.context.createMediaStreamSource(this.stream);
    this.worklet = new AudioWorkletNode(this.context, 'capture-16k', {
      numberOfInputs:2, numberOfOutputs:1, outputChannelCount:[1],
      channelCount:1, channelCountMode:'explicit', processorOptions:{module:acousticModule},
    });
    this.source.connect(this.worklet, 0, 0);
    this.playbackNode.connect(this.worklet, 0, 1);
    this.worklet.connect(this.context.destination);
    let timeout;
    try {
      await new Promise((resolve, reject) => {
        this.cancelReady = reject;
        timeout = setTimeout(() => reject(new Error('消回声组件启动超时，请重新开始通话。')), 5000);
        const fail = () => {
          const message = '声音处理出现问题，请重新开始通话。';
          if (this.cancelReady) reject(new Error(message));
          else this.onError(message);
        };
        this.worklet.onprocessorerror = fail;
        this.worklet.port.onmessage = ({data}) => {
          if (data.type === 'ready') resolve();
          else if (data.type === 'error') fail();
          else if (data.type === 'audio') this.frame(data);
        };
      });
    } finally {
      clearTimeout(timeout);
      this.cancelReady = null;
    }
  }
  checkOpen() {
    if (this.closed) throw new DOMException('通话已结束', 'AbortError');
  }
  frame(frame) {
    if (this.closed) return;
    this.onLevel({active: !this.muted && !!frame.speech && !frame.warming,
      level: this.muted ? 0 : Math.min(1, Math.sqrt(Math.max(0, frame.clean_rms || 0)) * 3)});
    if (this.socket.readyState !== WebSocket.OPEN) return;
    const pcm = frame.pcm;
    const samples = new Int16Array(pcm);
    if (this.muted) { samples.fill(0); new Int16Array(frame.raw_pcm).fill(0); }
    if (this.socket.bufferedAmount > 128000) {
      if (!this.overloaded) this.onError('音频上传暂时跟不上，请停顿一下后重说。');
      this.overloaded = true;
      this.seq++; // The backend detects the gap and rejects the incomplete clip.
      return;
    }
    this.overloaded = false;
    // One authoritative onset gate lives on the server. Local energy bumps
    // must never bypass AEC/VAD and irrevocably stop scheduled audio.
    const packet = new ArrayBuffer(1316);
    const view = new DataView(packet);
    view.setUint32(0, this.seq++, true);
    view.setFloat64(4, performance.now() - 64, true); // Reference look-ahead + AEC3/NS.
    view.setUint32(12, (frame.speech && !this.muted ? 1 : 0) | (frame.playback ? 2 : 0) | (frame.warming ? 4 : 0), true);
    view.setFloat32(16, this.muted ? 0 : frame.raw_rms, true);
    view.setFloat32(20, this.muted ? 0 : frame.clean_rms, true);
    view.setFloat32(24, frame.reference_rms, true);
    view.setFloat32(28, frame.echo_score, true);
    view.setFloat32(32, frame.raw_echo_score, true);
    new Uint8Array(packet, 36, 640).set(new Uint8Array(pcm));
    new Uint8Array(packet, 676, 640).set(new Uint8Array(frame.raw_pcm));
    this.socket.send(packet);
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.cancelReady?.(new DOMException('通话已结束', 'AbortError'));
    if (this.worklet) {
      this.worklet.onprocessorerror = null;
      this.worklet.port.onmessage = null;
      this.worklet.port.postMessage({type:'close'});
      try { this.playbackNode.disconnect(this.worklet); } catch {}
      this.worklet.disconnect();
    }
    this.source?.disconnect();
    this.stream?.getTracks().forEach(track => track.stop());
  }
}
