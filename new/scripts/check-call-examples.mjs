import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const out = path.resolve('runtime/validation');
await fs.mkdir(out, {recursive: true});
const url = pathToFileURL(path.resolve('demo/index.html')).href;
const browser = await chromium.launch({channel: 'msedge', headless: true});
const errors = [], remote = [], report = {calls: []};
try {
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (/^https?:/.test(request.url())) remote.push(request.url()); });
  await page.goto(url + '?library=1');
  assert.equal(await page.locator('#libraryDialog').evaluate(el => el.open), true);
  assert.equal(await page.locator('#callExamples .example-card').count(), 6);
  assert.match(await page.locator('#libraryDescription').textContent(), /无声回放/);
  const fixtureList = await page.evaluate(() => window.CALL_EXAMPLES.map(item => ({id: item.id, title: item.title, turns: item.turns})));
  await page.screenshot({path: path.join(out, 'call-examples-library.png')});
  report.library_opens_from_main_entry = true;

  for (const [index, example] of fixtureList.entries()) {
    if (index) await page.locator('#library').click();
    await page.locator(`[data-example="${example.id}"]`).click();
    assert.equal(await page.locator('#libraryDialog').evaluate(el => el.open), false);
    assert.equal(new URL(page.url()).searchParams.get('scene'), example.id);
    if (!index) {
      await page.locator('#speed').click(); await page.locator('#speed').click();
      await page.waitForFunction(() => document.querySelector('[data-id="call-0"] .copy')?.textContent.length > 3);
      await page.locator('#library').click();
      const frozen = await page.locator('#conversation').textContent();
      await page.waitForTimeout(350);
      assert.equal(await page.locator('#conversation').textContent(), frozen);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#conversation').getAttribute('data-playback'), 'playing');
      report.library_pauses_and_resumes_playback = true;
    }
    await page.waitForFunction(() => document.querySelector('#conversation').dataset.playback === 'ended', {}, {timeout: 60000});
    await page.waitForTimeout(500);
    const text = await page.locator('.message .copy').allTextContents();
    assert.deepEqual(text, example.turns.map(turn => turn[1]), `${example.id} must replay every turn exactly once`);
    assert.equal(await page.locator('.candidate').count(), 0);
    assert.equal(await page.locator('.microphone').getAttribute('data-state'), 'idle');
    assert.match(text.at(-1), /再见/);
    assert.equal(await page.locator('#play').getAttribute('aria-label'), '重新播放');
    report.calls.push({id: example.id, turns: text.length, ended: true});
  }
  await page.locator('#viewport').hover();
  await page.mouse.wheel(0, -1600);
  await page.waitForTimeout(450);
  await page.waitForFunction(() => !document.querySelector('#latest').hidden);
  await page.locator('#latest').click();
  await page.waitForTimeout(650);
  assert.equal(await page.locator('#latest').isVisible(), false);
  report.long_call_can_review_history_and_return = true;
  await page.screenshot({path: path.join(out, 'call-example-long-finished.png')});

  await page.locator('#replay').click();
  await page.waitForFunction(() => document.querySelectorAll('.message').length === 1);
  assert.equal(await page.locator('#conversation').getAttribute('data-playback'), 'playing');
  await page.locator('#play').click();
  const replayPaused = await page.locator('#conversation').textContent();
  await page.waitForTimeout(350);
  assert.equal(await page.locator('#conversation').textContent(), replayPaused);
  report.replay_resets_and_pause_holds = true;

  const mobile = await browser.newPage({viewport: {width: 320, height: 640}});
  mobile.on('pageerror', error => errors.push(error.message));
  await mobile.goto(url + '?scene=jd-busy&library=1');
  assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.ok(await mobile.locator('#libraryDialog').evaluate(el => el.scrollHeight > el.clientHeight));
  await mobile.locator('[data-example="jd-goodbye"]').click();
  assert.ok(await mobile.evaluate(() => [...document.querySelectorAll('footer button')].filter(el => !el.hidden).every(el => {
    const r = el.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth;
  })));
  assert.equal(new URL(mobile.url()).searchParams.get('scene'), 'jd-goodbye');
  await mobile.locator('#library').click();
  await mobile.screenshot({path: path.join(out, 'call-examples-mobile.png')});
  report.mobile_library_and_controls_fit = true;
  assert.deepEqual(remote, []);
  assert.deepEqual(errors, []);
  report.remote_requests = 0; report.passed = true;
} finally {
  await fs.writeFile(path.join(out, 'call-examples-check.json'), JSON.stringify(report, null, 2));
  await browser.close();
}
console.log(JSON.stringify(report, null, 2));
