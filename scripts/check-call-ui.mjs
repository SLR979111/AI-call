import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';

// Exercise the production page with deterministic socket events. Cloud/audio
// end-to-end acceptance is a separate check-browser.mjs run.
const base = process.env.CALL_URL || 'http://127.0.0.1:8088';
const out = path.resolve('runtime/validation');
await fs.mkdir(out, {recursive: true});
const browser = await chromium.launch({channel: 'msedge', headless: true,
  args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream']});
const report = {}, errors = [], sent = [];
try {
  const page = await browser.newPage({viewport: {width: 1100, height: 800}});
  const config = await (await page.request.get(base + "/api/config")).json();
  await page.route('**/api/config', route => route.fulfill({json: {...config, configured: true}}));
  let releaseFirstRound;
  let preparationGate = new Promise(resolve => { releaseFirstRound = resolve; });
  const preparations = [];
  let failPreparation = false;
  await page.route('**/api/warm', async route => {
    preparations.push(route.request().postDataJSON());
    await preparationGate;
    await route.fulfill({json: {started: true, status: failPreparation ? 'failed' : 'ready'}});
  });
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
      if (typeof raw !== 'string') return;
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
  await page.locator('#cancelDial:visible').waitFor();
  await page.locator('#cancelDial').click();
  await page.locator('#startText:enabled').waitFor();
  assert.equal(sent.length, 0);
  await page.locator('#startText').click();
  await page.locator('#cancelDial:visible').waitFor();
  assert.equal(preparations.length, 1);
  assert.equal(sent.length, 0);
  releaseFirstRound();
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
  // A previous call can still display reply-1 while the new dial waits.
  await page.locator('#callControls:visible').waitFor();
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
  assert.equal(await page.getByRole('link', {name: '观看对话回放'}).getAttribute('href'), '/demo/?library=1');
  await page.getByRole('button', {name: '恢复默认任务', exact: true}).click();
  await page.getByRole('button', {name: '保存任务', exact: true}).click();
  assert.deepEqual(await page.evaluate(() => JSON.parse(localStorage.getItem('g2-task'))), config.default_task);
  report.restore_default_and_library_entry = true;
  await page.getByRole('button', {name: '任务设置', exact: true}).click();
  assert.deepEqual(await page.locator('#taskPack option').evaluateAll(options => options.slice(1).map(o => o.value)),
    Object.keys(config.task_packs));
  assert.ok(Object.keys(config.task_packs).every(id => id.startsWith('jd-')));
  await page.locator('#taskPack').selectOption('jd-category-report');
  await page.locator('#saveTask').click();
  // Simulate a revision to the saved preset between visits.
  await page.evaluate(() => {
    const selected = JSON.parse(localStorage.getItem('g2-task-pack'));
    selected.card.facts = '先前版本的资料';
    localStorage.setItem('g2-task-pack', JSON.stringify(selected));
    localStorage.setItem('g2-task', JSON.stringify(selected.card));
  });
  await page.reload();
  await page.locator('#editTask').click();
  assert.equal(await page.locator('#taskPack').inputValue(), 'jd-category-report');
  assert.equal(await page.locator('#editFacts').inputValue(), config.task_packs['jd-category-report'].facts);
  await page.locator('#taskPack').selectOption('');
  assert.equal(await page.locator('#editTitle').inputValue(), '保留我的自定义任务');
  await page.locator('#closeDialog').click();
  await page.goto(base + '/?pack=jd-brand-mixup');
  await page.locator('#startText').click();
  await page.locator('[data-id="reply-1"] .copy').waitFor();
  assert.deepEqual(sent.filter(event => event.type === 'start').at(-1).task, config.task_packs['jd-brand-mixup']);
  await page.locator('#editTask').click();
  assert.equal(await page.locator('#taskPack').isDisabled(), true);
  await page.locator('#closeDialog').click();
  await page.locator('#stop').click();
  await page.goto(base + '/demo/?library=1');
  const links = await page.locator('#callExamples a').evaluateAll(anchors => anchors.map(a => new URL(a.href).searchParams.get('pack')));
  assert.equal(links.length, 6);
  assert.equal(new Set(links).size, 6);
  assert.ok(links.every(id => Object.hasOwn(config.task_packs, id)));
  await page.locator('#callExamples a').last().click();
  await page.locator('#startText:enabled').waitFor();
  assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem('g2-task')).opening), config.task_packs['jd-long-call'].opening);
  report.interactive_work_orders = true;
  failPreparation = true;
  const priorStarts = sent.filter(event => event.type === 'start').length;
  await page.reload();
  await page.getByText('工单准备失败，点击开始可重试').waitFor();
  await page.locator('#startText').click();
  await page.locator('#error:visible').waitFor();
  assert.equal(sent.filter(event => event.type === 'start').length, priorStarts);
  assert.equal(preparations.at(-1).retry, true);
  failPreparation = false;
  await page.locator('#startText').click();
  await page.locator('[data-id="reply-1"] .copy').waitFor();
  assert.equal(sent.filter(event => event.type === 'start').length, priorStarts + 1);
  report.preparation_wait_cancel_ready_and_retry = true;
  await page.locator('#stop').click();
  let releaseVoice;
  preparationGate = new Promise(resolve => { releaseVoice = resolve; });
  await page.addInitScript(() => {
    window.micRequests = 0;
    const getMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = constraints => {
      window.micRequests++;
      return getMedia(constraints);
    };
  });
  await page.reload();
  await page.locator('#startVoice').click();
  await page.locator('#cancelDial:visible').waitFor();
  assert.equal(await page.evaluate(() => window.micRequests), 0);
  releaseVoice();
  await page.locator('[data-id="reply-1"] .copy').waitFor();
  assert.equal(await page.evaluate(() => window.micRequests), 1);
  assert.equal(sent.filter(event => event.type === 'start').at(-1).voice, true);
  report.microphone_opens_after_preparation = true;

  await page.goto(base + '/static/classic.html');
  await page.waitForFunction(() => document.querySelector('#checkpoints').children.length > 0);
  await page.locator('#editTask').click();
  await page.locator('#editTitle').fill('经典界面工单');
  await page.locator('#taskForm button[type=submit]').click();
  assert.equal(await page.locator('#taskTitle').textContent(), '经典界面工单');
  await page.locator('#startText').click();
  await page.locator('#transcript .message.assistant').waitFor();
  assert.equal(sent.filter(event => event.type === 'start').at(-1).task.title, '经典界面工单');
  emit({type: 'state', epoch: 1, phase: 'listening', delivered: [], status: 'active'});
  await page.getByText('在听，请说', {exact: true}).waitFor();
  await page.locator('#stop').click();
  await page.locator('#startText:visible').waitFor();
  report.classic_task_editor_and_call = true;

  // Hold the first microphone grant until after hangup and redial.
  await page.addInitScript(() => {
    const getMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    let first = true;
    navigator.mediaDevices.getUserMedia = async constraints => {
      const stream = await getMedia(constraints);
      if (first) {
        first = false;
        window.oldTrack = stream.getAudioTracks()[0];
        await new Promise(resolve => { window.releaseGrant = resolve; });
      }
      return stream;
    };
  });
  await page.goto(base + '/static/display.html');
  await page.getByText('点击接听', {exact: true}).last().waitFor();
  await page.locator('#callBtn').click();
  await page.waitForFunction(() => typeof window.releaseGrant === 'function');
  await page.locator('#stopBtn').click();
  await page.locator('#callBtn').click();
  await page.waitForFunction(() => document.querySelector('#words').textContent.includes('您好'));
  await page.evaluate(() => window.releaseGrant());
  await page.waitForFunction(() => window.oldTrack.readyState === 'ended');
  assert.equal(await page.locator('#stopBtn').isVisible(), true);
  await page.locator('#stopBtn').click();
  await page.locator('#callBtn:visible').waitFor();
  report.display_redial_during_microphone_permission = true;

  assert.deepEqual(errors, []); report.errors = errors; report.passed = true;
} finally {
  await fs.writeFile(path.join(out, 'call-ui-check.json'), JSON.stringify(report, null, 2));
  await browser.close();
}
console.log(JSON.stringify(report, null, 2));
