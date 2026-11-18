import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';

// Exercise the production page with deterministic socket events. Cloud/audio
// end-to-end acceptance is a separate check-browser.mjs run.
const base = process.env.CALL_URL || 'http://127.0.0.1:8088';
const out = path.resolve('runtime/validation');
await fs.mkdir(out, {recursive: true});
const browser = await chromium.launch({channel: 'msedge', headless: true});
const report = {}, errors = [], sent = [];
try {
  const page = await browser.newPage({viewport: {width: 1100, height: 800}});
  const config = await (await page.request.get(base + "/api/config")).json();
  await page.addInitScript(legacy => {
    if (!sessionStorage.getItem('migration-seeded')) {
      localStorage.setItem('g2-task', JSON.stringify(legacy));
      sessionStorage.setItem('migration-seeded', '1');
    }
  }, config.legacy_default_tasks?.at(-1) || config.legacy_default_task);
  page.on('pageerror', error => errors.push(error.message));
  let peer, nextReply = 4;
  const emit = event => peer.send(JSON.stringify(event));
  await page.routeWebSocket('**/ws', ws => {
    peer = ws;
    ws.onMessage(raw => {
      const event = JSON.parse(raw); sent.push(event);
      if (event.type === 'start') {
        emit({type: 'reply_start', epoch: 1});
        emit({type: 'utterance', epoch: 1, unit: 1, text: '您好，我们一起看一下这份材料。'});
        emit({type: 'reply_done', epoch: 1});
      }
      if (event.type === 'text') emit({type: 'transcript', text: event.text, final: true, epoch: nextReply});
    });
  });
  await page.goto(base);
  await page.getByRole('button', {name: '文字试聊', exact: true}).click();
  await page.locator('[data-id="reply-1"] .copy').waitFor();
  assert.equal(sent.find(event => event.type === 'start').task.title, config.default_task.title);
  report.unmodified_legacy_default_migrates = true;
  assert.equal(await page.locator('.ring, header, #scenes, #sceneLabel').count(), 0);
  assert.ok((await page.locator('footer button').allTextContents()).every(text => !text.trim()));
  report.icon_only_controls_and_no_spinner = true;

  emit({type: 'transcript', text: '公司明称', final: false});
  await page.locator('[data-id="input-1"] .glyph').first().waitFor();
  await page.evaluate(() => window.partialGlyph = document.querySelector('[data-id="input-1"] .glyph'));
  emit({type: 'transcript', text: '公司名称不一致。', final: false});
  emit({type: 'transcript', text: '公司名称不一致。', final: true, epoch: 2});
  await page.waitForFunction(() => document.querySelector('[data-id="input-1"] .copy').textContent === '公司名称不一致。');
  assert.equal(await page.locator('.message.user').count(), 1);
  assert.ok(await page.evaluate(() => window.partialGlyph === document.querySelector('[data-id="input-1"] .glyph')));
  report.asr_revises_one_message = true;

  emit({type: 'reply_start', epoch: 2});
  emit({type: 'utterance', epoch: 2, unit: 1, text: '先核对公司名称。'});
  await page.locator('[data-id="reply-2"] .copy').waitFor();
  emit({type: 'utterance', epoch: 2, unit: 2, text: '您有名称变更证明吗？'});
  await page.waitForFunction(() => document.querySelector('[data-id="reply-2"] .copy').textContent === '先核对公司名称。您有名称变更证明吗？');
  assert.equal(await page.locator('[data-id="reply-2"] .copy').count(), 1);
  emit({type: 'cancel', epoch: 2});
  emit({type: 'utterance', epoch: 2, unit: 3, text: '不应该出现'});
  await page.waitForTimeout(100);
  assert.ok(!(await page.locator('#transcript').textContent()).includes('不应该出现'));
  report.joined_reply_and_late_cancelled_units = true;

  emit({type: 'reply_start', epoch: 3});
  emit({type: 'utterance', epoch: 3, unit: 1, text: '好，您先说。'});
  emit({type: 'reply_done', epoch: 3});
  emit({type: 'candidates', epoch: 3, items: [{id: 'c1', text: '请打开新的名称变更证明。'}]});
  await page.locator('.candidate .glyph:not(.candidate-marker)').first().waitFor();
  assert.equal(await page.locator('button.candidate').count(), 0);
  await page.evaluate(() => window.candidateGlyph = document.querySelector('.candidate .glyph:not(.candidate-marker)'));
  await page.getByRole('textbox', {name: '输入一句话', exact: true}).fill('有的，我打开给您看。');
  await page.getByRole('button', {name: '发送消息', exact: true}).click();
  await page.waitForFunction(() => document.querySelectorAll('.message.user').length === 2);
  emit({type: 'reply_start', epoch: 4});
  emit({type: 'utterance', epoch: 4, unit: 1, candidate_id: 'c1', text: '请打开新的名称变更证明。'});
  await page.locator('[data-id="reply-4"] .copy').waitFor();
  assert.ok(await page.evaluate(() => window.candidateGlyph === document.querySelector('[data-id="reply-4"] .glyph')));
  emit({type: 'reply_done', epoch: 4});
  emit({type: 'candidates', epoch: 4, items: [{id: 'c2', text: '再看一下授权书上的公司名称。'}]});
  emit({type: 'cancel', epoch: 2});
  emit({type: 'candidates', epoch: 3, items: [{id: 'stale', text: '过时候选'}]});
  emit({type: 'utterance', epoch: 4, unit: 2, text: '完成后的迟到内容'});
  await page.waitForTimeout(700);
  assert.equal(await page.locator('.candidate .copy').textContent(), '再看一下授权书上的公司名称。');
  assert.ok(!(await page.locator('#transcript').textContent()).includes('完成后的迟到内容'));
  assert.ok(sent.some(event => event.type === 'text' && event.text === '有的，我打开给您看。'));
  report.real_event_candidate_promotion_and_stale_guards = true;
  await page.screenshot({path: path.join(out, 'integrated-call-ui.png')});

  emit({type: 'hangup', epoch: 3, reason: 'complete'});
  await page.waitForTimeout(40);
  assert.equal(await page.getByRole('button', {name: '结束对话', exact: true}).isVisible(), true);
  emit({type: 'hangup', epoch: 4, reason: 'complete'});
  await page.getByRole('button', {name: '文字试聊', exact: true}).waitFor({state: 'visible'});
  assert.equal(await page.locator('#phase').textContent(), '沟通已完成，已道别挂断');
  assert.equal(await page.locator('#callNotice').isVisible(), true);
  assert.equal(await page.locator('#callNotice').textContent(), '沟通已完成，已道别挂断');
  report.server_hangup_restores_start_controls = true;

  await page.getByRole('button', {name: '文字试聊', exact: true}).click();
  await page.locator('[data-id="reply-1"] .copy').waitFor();
  assert.equal(await page.locator('.message').count(), 1);
  assert.equal(await page.locator('.candidate').count(), 0);
  await page.getByRole('button', {name: '结束对话', exact: true}).click();
  for (const width of [320, 390, 600]) {
    await page.setViewportSize({width, height: 844}); await page.waitForTimeout(80);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.ok(await page.evaluate(() => [...document.querySelectorAll('footer button')].filter(button => button.checkVisibility()).every(button => {
      const r = button.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth;
    })));
  }
  report.restart_and_narrow_controls = true;
  await page.evaluate(card => localStorage.setItem('g2-task', JSON.stringify({...card, title: '保留我的自定义任务'})), config.legacy_default_task);
  await page.reload();
  await page.getByRole('button', {name: '文字试聊', exact: true}).click();
  await page.locator('[data-id="reply-1"] .copy').waitFor();
  assert.equal(sent.filter(event => event.type === 'start').at(-1).task.title, '保留我的自定义任务');
  await page.getByRole('button', {name: '结束对话', exact: true}).click();
  report.edited_task_is_preserved = true;
  await page.getByRole('button', {name: '任务设置', exact: true}).click();
  assert.equal(await page.getByRole('link', {name: '电话例子库'}).getAttribute('href'), '/demo/?library=1');
  await page.getByRole('button', {name: '恢复默认任务', exact: true}).click();
  await page.getByRole('button', {name: '保存任务', exact: true}).click();
  assert.deepEqual(await page.evaluate(() => JSON.parse(localStorage.getItem('g2-task'))), config.default_task);
  report.restore_default_and_library_entry = true;
  assert.deepEqual(errors, []); report.errors = errors; report.passed = true;
} finally {
  await fs.writeFile(path.join(out, 'call-ui-check.json'), JSON.stringify(report, null, 2));
  await browser.close();
}
console.log(JSON.stringify(report, null, 2));
