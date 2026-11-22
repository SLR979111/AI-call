// Fixed inputs on the live voice path. Send as soon as the opening finishes,
// independent of candidate availability. Never use a person's microphone.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {setTimeout as delay} from 'node:timers/promises';
import {chromium} from 'playwright';

const base = process.env.CALL_URL || 'http://127.0.0.1:8088';
const label = process.argv[2] || 'current';
const cases = [
  ['这次资料被退回了，您想先了解哪一部分？', '呃，是的呢。'],
  ['这次资料被退回了，您想先了解哪一部分？', '嗯，方便的。'],
  ['这次资料被退回了，您想先了解哪一部分？', '哦，好的呀。'],
  ['这次资料被退回了，您想先了解哪一部分？', '具体缺什么材料？'],
  ['这次资料被退回了，您想先了解哪一部分？', '不是，我不负责，而且还有一个别的问题。'],
  ['这次资料被退回了，您想先了解哪一部分？', '只是保存，还没有提交。'],
  ['这次资料被退回了，您想先了解哪一部分？', '已经提交了，显示审核中。'],
  ['这次资料被退回了，您想先了解哪一部分？', '品牌方直接给我们公司授权了。'],
  ['这次资料被退回了，您想先了解哪一部分？', '授权书上的公司名写错了。'],
  ['这次资料被退回了，您想先了解哪一部分？', '商标状态截图要包含什么？'],
  ['这次资料被退回了，您想先了解哪一部分？', '我还要补经销商授权吗？'],
  ['这次资料被退回了，您想先了解哪一部分？', '没关系，我先忙，晚点再说。'],
];

const browser = await chromium.launch({channel: 'msedge', args: ['--autoplay-policy=no-user-gesture-required']});
const rows = [];
try {
  for (const [opening, input] of cases) {
    const page = await browser.newPage();
    const inEvents = [], outEvents = [];
    page.on('websocket', socket => {
      for (const [event, events] of [['framereceived', inEvents], ['framesent', outEvents]]) {
        socket.on(event, ({payload}) => {
          if (typeof payload === 'string') events.push({...JSON.parse(payload), at: performance.now()});
        });
      }
    });
    await page.addInitScript(opening => {
      localStorage.setItem('g2-task', JSON.stringify({
        title: '资料补交通知', goal: '解释缺少的材料和补交方式，回答对方的问题。',
        facts: '缺少商标状态截图，需要在原提交入口上传截图再点提交。只保存不会提交。',
        checkpoints: [{id: 'reason', description: '说明缺少商标状态截图'},
          {id: 'next_step', description: '说明在原提交入口上传截图再点提交'}], opening}));
      navigator.mediaDevices.getUserMedia = async () => {
        const ctx = new AudioContext(); await ctx.resume();
        return ctx.createMediaStreamDestination().stream;
      };
    }, opening);
    async function until(predicate) {
      const deadline = performance.now() + 45000;
      while (!predicate()) {
        const error = inEvents.find(e => e.type === 'error');
        if (error) throw new Error(error.message);
        assert.ok(performance.now() < deadline, 'Timed out waiting for ' + input);
        await delay(20);
      }
    }
    await page.goto(base);
    await page.locator('#startVoice:enabled').waitFor();
    await page.locator('#editTask').click();
    await page.locator('#prediction').check();
    await page.locator('#closeDialog').click();
    await page.locator('#startVoice').click();
    const toggle = page.locator('#toggleText');
    if (await toggle.isVisible().catch(() => false)) {
      await toggle.click();
    } else {
      await page.evaluate(() => {
        const form = document.getElementById('textForm');
        if (form) form.hidden = false;
      });
    }
    await page.locator('#textInput').fill(input);
    await until(() => inEvents.some(e => e.type === 'reply_done' && e.epoch === 1));
    await page.locator('#sendText').click();
    await until(() => inEvents.some(e => e.type === 'reply_done' && e.epoch > 1));
    const start = outEvents.find(e => e.type === 'text').at;
    const done = inEvents.find(e => e.type === 'reply_done' && e.epoch > 1);
    const units = inEvents.filter(e => e.type === 'utterance' && e.epoch === done.epoch && !e.filler);
    const play = outEvents.find(e => e.type === 'playback_started' && e.epoch === done.epoch && e.unit === units[0].unit);
    const available = inEvents.filter(e => e.type === 'candidates' && e.epoch === 1 && e.at <= start).at(-1)?.items.length || 0;
    const latency = Math.round(play.at - start);
    const row = {input, available, reused: done.reused, firstBodyPlaybackReceiptMs: latency,
      answer: units.map(u => u.text).join(''), session: inEvents.find(e => e.type === 'ready').session_id};
    if (input.includes('别的问题')) assert.equal(row.reused, false, 'A new question needs a fresh turn');
    rows.push(row); console.log(row);
    await page.close();
  }
  await fs.mkdir('runtime/validation', {recursive: true});
  await fs.writeFile('runtime/validation/forecast-hits-' + label + '.json', JSON.stringify(rows, null, 2));
} finally { await browser.close(); }
