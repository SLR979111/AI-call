// Feed the same saved 24 kHz mono PCM through the real browser capture path.
// Example: node scripts/check-first-response.mjs runtime/validation/user-question.pcm before
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {chromium} from 'playwright';

const [fixture, label = 'current'] = process.argv.slice(2);
const pcm = (await fs.readFile(fixture)).toString('base64');
const browser = await chromium.launch({channel: 'msedge', args: ['--autoplay-policy=no-user-gesture-required']});
const results = [];
try {
  for (let run = 0; run < 3; run++) {
    const page = await browser.newPage();
    await page.addInitScript(() => {
      // Observe the actual playback schedule on the same clock as capture.
      const OriginalSocket = window.WebSocket;
      window.trace = {units: new Map(), starts: new Map(), replies: [], transcripts: []};
      window.WebSocket = class extends OriginalSocket {
        constructor(...args) {
          super(...args);
          this.addEventListener('message', ({data}) => {
            trace.packet = null;
            if (data instanceof ArrayBuffer) {
              const v = new DataView(data);
              trace.packet = v.getUint32(0, true) + ':' + v.getUint32(4, true);
              return;
            }
            const e = JSON.parse(data);
            if (e.type === 'utterance') trace.units.set(e.epoch + ':' + e.unit, {...e, at: performance.now()});
            if (e.type === 'transcript' && e.final) trace.transcripts.push({...e, at: performance.now()});
            if (e.type === 'reply_done') trace.replies.push(e.epoch);
            if (e.type === 'candidates') trace.candidates = (e.items || []).length;
            if (e.type === 'latency' && e.voice_to_playback_ms != null) trace.latency = e;
            if (e.type === 'error') trace.error = e.message;
          });
        }
      };
      const start = AudioBufferSourceNode.prototype.start;
      AudioBufferSourceNode.prototype.start = function(when, ...rest) {
        if (this.buffer?.sampleRate === 24000 && trace.packet && !trace.starts.has(trace.packet)) {
          trace.starts.set(trace.packet, performance.now() + (when - this.context.currentTime) * 1000);
        }
        return start.call(this, when, ...rest);
      };
      navigator.mediaDevices.getUserMedia = async () => {
        const ctx = new AudioContext(); await ctx.resume();
        const dest = ctx.createMediaStreamDestination();
        window.speak = base64 => {
          const data = new Int16Array(Uint8Array.from(atob(base64), c => c.charCodeAt(0)).buffer);
          const b = ctx.createBuffer(1, data.length, 24000);
          b.copyToChannel(Float32Array.from(data, sample => sample / 32768), 0);
          const src = ctx.createBufferSource(); src.buffer = b; src.connect(dest); src.start();
        };
        return dest.stream;
      };
    });
    await page.goto(process.env.CALL_URL || 'http://127.0.0.1:8088');
    await page.locator('#startVoice:enabled').waitFor();
    await page.getByRole('button', {name:'浠诲姟璁剧疆', exact:true}).click();
    await page.locator('#prediction').check();
    await page.locator('#closeDialog').click();
    await page.locator('#startVoice').click();
    await page.waitForFunction(() => trace.replies.length || trace.error, null, {timeout:55000});
    await page.waitForFunction(() => (trace.candidates || 0) > 0 || trace.error, null, {timeout:15000});
    await new Promise(resolve => setTimeout(resolve, 500));
    await page.evaluate(data => speak(data), pcm);
    await page.waitForFunction(() => trace.error || (trace.replies.length >= 2 && trace.latency), null, {timeout:55000});
    const row = await page.evaluate(() => {
      if (trace.error) throw new Error(trace.error);
      const epoch = trace.replies.at(-1);
      const units = [...trace.units].filter(([, u]) => u.epoch === epoch);
      const [id, body] = units.find(([, u]) => !u.filler);
      const voiceEnd = trace.starts.get(units[0][0]) - trace.latency.voice_to_playback_ms;
      return {transcript: trace.transcripts.at(-1).text, firstText: body.text,
        endToTranscriptMs: Math.round(trace.transcripts.at(-1).at - voiceEnd),
        endToBodyTextMs: Math.round(body.at - voiceEnd),
        endToBodyAudioMs: Math.round(trace.starts.get(id) - voiceEnd)};
    });
    assert.ok(row.endToBodyAudioMs > 0 && row.transcript);
    results.push(row); console.log(row);
    await page.close();
  }
  await fs.mkdir('runtime/validation', {recursive:true});
  await fs.writeFile('runtime/validation/first-response-' + label + '.json', JSON.stringify(results, null, 2));
} finally { await browser.close(); }


