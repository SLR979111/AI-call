// Real model, TTS and browser audio. The synthetic microphone stays silent.
import assert from 'node:assert/strict';
import {setTimeout as delay} from 'node:timers/promises';
import {chromium} from 'playwright';

const browser = await chromium.launch({channel: 'msedge', args: ['--autoplay-policy=no-user-gesture-required']});
try {
  const page = await browser.newPage();
  const received = [], sent = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('websocket', socket => {
    for (const [event, sink] of [['framereceived', received], ['framesent', sent]]) {
      socket.on(event, ({payload}) => {
        if (typeof payload === 'string') sink.push(JSON.parse(payload));
      });
    }
  });
  async function waitFor(check) {
    const deadline = Date.now() + 50000;
    while (!check()) {
      const failure = received.find(event => event.type === 'error');
      if (failure) throw new Error(failure.message);
      if (Date.now() >= deadline) throw new Error('Timed out: ' + JSON.stringify(received.slice(-8)));
      await delay(25);
    }
  }
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext();
      await context.resume();
      return context.createMediaStreamDestination().stream;
    };
  });
  await page.goto(process.env.CALL_URL || 'http://127.0.0.1:8088');
  await page.locator('#startVoice:enabled').waitFor();
  await page.locator('#editTask').click();
  await page.locator('#addOrder').click();
  await page.locator('#editTitle').fill('图书室开门时间');
  await page.locator('#editGoal').fill('告知周六上午九点开门，对方确认理解后结束。');
  await page.locator('#editFacts').fill('图书室周六上午九点开门。没有其他要说明的事。');
  await page.locator('#editPoints').fill('说明周六上午九点开门');
  await page.locator('#editOpening').fill('您好，这里是社区图书室。我想和您说一下周末安排，您现在方便吗？');
  await page.locator('#toggleCallOptions').click();
  await page.locator('#prediction').uncheck();
  await page.locator('#generateOrder').click();
  await page.locator('#saveTask:enabled').waitFor();
  await page.locator('#saveTask').click();
  await page.evaluate(() => {
    const frame = TextMotion.prototype.frame;
    TextMotion.prototype.frame = function() {
      this.frameId = 0;
      window.resumeTranscript = () => { TextMotion.prototype.frame = frame; this.wake(); };
    };
  });
  await page.locator('#startVoice').click();
  await waitFor(() => sent.some(e => e.type === 'played' && e.epoch === 1 && e.samples >= 32000));
  await page.locator('#interrupt').click();
  await waitFor(() => received.some(e => e.type === 'cancel' && e.epoch === 1));
  const cutoff = sent.find(e => e.type === 'interrupt').progress.find(u => u.unit === 1);
  assert.ok(cutoff.samples >= 32000);
  await page.locator('#toggleText').click();
  const input = async text => {
    await page.locator('#textInput').fill(text);
    await page.locator('#sendText').click();
  };
  await input('刚才你讲到哪里了？接着告诉我周六几点开门。');
  await waitFor(() => received.some(e => e.type === 'reply_done' && e.epoch > 1));
  const body = received.filter(e => e.type === 'utterance' && e.epoch > 1 && !e.filler);
  const opening = received.find(e => e.type === 'utterance' && e.epoch === 1).text;
  const quoted = body[0].text.match(/说到“(.+)”/);
  assert.ok(quoted && opening.startsWith(quoted[1]), 'Read back the interrupted opening prefix');
  assert.match(body.map(e => e.text).join(''), /九点|9[点时]|0?9[:：]00/);
  assert.ok(sent.some(e => e.type === 'played' && e.epoch === body[0].epoch && e.done));
  assert.equal(await page.locator('#transcript .message').count(), 0);
  await page.evaluate(() => resumeTranscript());
  await page.locator('#transcript .message').first().waitFor();
  await input('明白了，周六上午九点开门，我没有别的问题了。');
  await waitFor(() => received.some(e => e.type === 'hangup'));
  const hangup = received.find(e => e.type === 'hangup');
  assert.equal(hangup.reason, 'complete');
  const goodbye = received.filter(e => e.type === 'utterance' && e.epoch === hangup.epoch).at(-1);
  assert.match(goodbye.text, /再见|拜拜/);
  assert.ok(sent.some(e => e.type === 'played' && e.epoch === goodbye.epoch && e.unit === goodbye.unit && e.done));
  await page.locator('#startVoice').waitFor({state: 'visible'});
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({session: received.find(e => e.type === 'ready').session_id,
    cutoffMs: Math.round(cutoff.samples / 24), reply: body.map(e => e.text), goodbye: goodbye.text}));
} finally {
  await browser.close();
}
