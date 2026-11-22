const RATES = [16000, 48000];
const LEVELS = { low: 1, moderate: 2, high: 3, veryHigh: 4 };
const STATS = 5;
const LATENCY = { 16000: [288, 96], 48000: [910, 288] };
const ESPIPE = 70;

function finite(buf, from, to) {
  let bad = 0;
  for (let i = from; i < to; i++) {
    const v = buf[i];
    if (v - v !== 0) {
      buf[i] = 0;
      bad++;
    }
  }
  return bad;
}

function compile(wasm) {
  if (wasm instanceof WebAssembly.Module) return wasm;
  if (wasm instanceof ArrayBuffer || ArrayBuffer.isView(wasm)) return new WebAssembly.Module(wasm);
  throw new TypeError('webrtc-audio-processing: wasm must be a WebAssembly.Module, an ArrayBuffer or a typed array');
}

function noiseLevel(value) {
  if (value === undefined || value === false) return 0;
  if (value === true) return LEVELS.moderate;
  const level = LEVELS[value];
  if (level === undefined) throw new RangeError(`webrtc-audio-processing: noiseSuppression must be a boolean or one of ${Object.keys(LEVELS).join(', ')}`);
  return level;
}

function vadMode(value) {
  if (value === undefined || value === false) return -1;
  if (value === true) return 1;
  if (Number.isInteger(value) && value >= 0 && value <= 3) return value;
  throw new RangeError('webrtc-audio-processing: vad must be a boolean or an aggressiveness from 0 to 3');
}

export function instantiate(wasm, options = {}) {
  const module = compile(wasm);
  const log = typeof options.log === 'function' ? options.log : null;
  let memory = null;
  let text = '';
  const view = () => new DataView(memory.buffer);
  const imports = {
    env: {
      emscripten_notify_memory_growth() {},
    },
    wasi_snapshot_preview1: {
      clock_time_get(id, precision, out) {
        view().setBigUint64(out, BigInt(Math.round(Date.now() * 1e6)), true);
        return 0;
      },
      fd_write(fd, iovs, count, written) {
        const dv = view();
        let total = 0;
        for (let i = 0; i < count; i++) {
          const ptr = dv.getUint32(iovs + i * 8, true);
          const len = dv.getUint32(iovs + i * 8 + 4, true);
          if (log) {
            for (let k = 0; k < len; k++) {
              const c = dv.getUint8(ptr + k);
              if (c === 10) {
                log(text);
                text = '';
              } else {
                text += String.fromCharCode(c);
              }
            }
          }
          total += len;
        }
        dv.setUint32(written, total, true);
        return 0;
      },
      fd_close() {
        return 0;
      },
      fd_seek() {
        return ESPIPE;
      },
    },
  };
  const instance = new WebAssembly.Instance(module, imports);
  const x = instance.exports;
  memory = x.memory;
  x._initialize();

  function create(opts = {}) {
    const sampleRate = opts.sampleRate;
    if (!RATES.includes(sampleRate)) throw new RangeError('webrtc-audio-processing: sampleRate must be 16000 or 48000');
    const vad = vadMode(opts.vad);
    const ns = noiseLevel(opts.noiseSuppression);
    let handle = x.wap_create(sampleRate, ns, vad);
    if (!handle) throw new Error('webrtc-audio-processing: the audio processing module could not be created');
    const frame = x.wap_frame(handle);
    const micPtr = x.wap_mic(handle);
    const refPtr = x.wap_ref(handle);
    const outPtr = x.wap_out(handle);
    const statsPtr = x.wap_alloc(STATS * 8);
    const refCap = sampleRate + frame;
    const refRing = new Float32Array(refCap);
    let refStart = 0;
    let refSize = 0;
    let refDropped = 0;
    let refShort = 0;
    let nonFinite = 0;
    let fill = 0;
    let buffer = null;
    let mic = null;
    let ref = null;
    let out = null;
    let failed = 0;

    function views() {
      if (buffer !== memory.buffer) {
        buffer = memory.buffer;
        mic = new Float32Array(buffer, micPtr, frame);
        ref = new Float32Array(buffer, refPtr, frame);
        out = new Float32Array(buffer, outPtr, frame);
      }
    }

    function pushRef(samples) {
      let src = samples;
      if (src.length > refCap) {
        refDropped += src.length - refCap;
        src = src.subarray(src.length - refCap);
      }
      const over = refSize + src.length - refCap;
      if (over > 0) {
        refStart = (refStart + over) % refCap;
        refSize -= over;
        refDropped += over;
      }
      let at = (refStart + refSize) % refCap;
      let i = 0;
      while (i < src.length) {
        const k = Math.min(src.length - i, refCap - at);
        refRing.set(src.subarray(i, i + k), at);
        nonFinite += finite(refRing, at, at + k);
        i += k;
        at = (at + k) % refCap;
      }
      refSize += src.length;
    }

    function takeRef() {
      const n = Math.min(frame, refSize);
      let i = 0;
      while (i < n) {
        const k = Math.min(n - i, refCap - refStart);
        ref.set(refRing.subarray(refStart, refStart + k), i);
        i += k;
        refStart = (refStart + k) % refCap;
      }
      refSize -= n;
      if (n < frame) {
        ref.fill(0, n);
        refShort += frame - n;
      }
    }

    function live() {
      if (!handle) throw new Error('webrtc-audio-processing: the canceller was destroyed');
    }

    return {
      sampleRate,
      frameSize: frame,
      latencySamples: LATENCY[sampleRate][0] + (ns ? LATENCY[sampleRate][1] : 0),
      cancel(micIn, refIn, target) {
        live();
        if (!(micIn instanceof Float32Array) || !(refIn instanceof Float32Array)) {
          throw new TypeError('webrtc-audio-processing: cancel(mic, reference) takes two Float32Arrays');
        }
        const n = micIn.length;
        const result = target === undefined ? new Float32Array(n) : target;
        if (!(result instanceof Float32Array) || result.length < n) {
          throw new TypeError('webrtc-audio-processing: the output must be a Float32Array at least as long as mic');
        }
        const refs = refIn.length;
        views();
        let j = 0;
        while (j < n) {
          const k = Math.min(frame - fill, n - j);
          if (j < refs) pushRef(refIn.subarray(j, Math.min(j + k, refs)));
          mic.set(micIn.subarray(j, j + k), fill);
          nonFinite += finite(mic, fill, fill + k);
          result.set(out.subarray(fill, fill + k), j);
          fill += k;
          j += k;
          if (fill === frame) {
            takeRef();
            if (x.wap_process(handle) !== 0) failed++;
            views();
            fill = 0;
          }
        }
        if (n < refs) pushRef(refIn.subarray(n));
        return result;
      },
      stats() {
        live();
        const have = x.wap_stats(handle, statsPtr);
        const s = new Float64Array(memory.buffer, statsPtr, STATS);
        const voice = vad < 0 ? null : x.wap_voice(handle);
        return {
          delayMs: have & 1 ? s[0] : null,
          erleDb: have & 2 ? s[1] : null,
          erlDb: have & 4 ? s[2] : null,
          divergentFilterFraction: have & 8 ? s[3] : null,
          delayMedianMs: have & 16 ? s[4] : null,
          voice: voice === null || voice < 0 ? null : voice === 1,
          referenceDropped: refDropped,
          referenceShort: refShort,
          nonFiniteSamples: nonFinite,
          errors: failed,
        };
      },
      destroy() {
        if (!handle) return;
        x.wap_destroy(handle);
        x.wap_free(statsPtr);
        handle = 0;
      },
    };
  }

  return { create, memory };
}

export function create(options) {
  if (!options || options.wasm === undefined) {
    throw new TypeError('webrtc-audio-processing: create({ wasm, sampleRate }) needs the wasm (a WebAssembly.Module or its bytes); in Node, import create from "@ai-ecoverse/wasm-webrtc-audio-processing/node"');
  }
  return instantiate(options.wasm, options).create(options);
}
