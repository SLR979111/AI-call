import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';

// Exercise real browser scroll clamping while a long focused paragraph shrinks.
// Numerical spring tests alone cannot catch the one-frame clamp and rebound.
const out = path.resolve('runtime/validation');
await fs.mkdir(out, {recursive: true});
const browser = await chromium.launch({channel: 'msedge', headless: true});
const report = {}, errors = [];
try {
  const page = await browser.newPage({viewport: {width: 900, height: 720}});
  page.on('pageerror', error => errors.push(error.message));
  await page.setContent('<style>body{margin:0;background:#080808}main{height:660px;overflow:auto}</style><main><div id="stage"></div></main>');
  await page.addStyleTag({path: path.resolve('web/text-engine.css')});
  for (const file of ['text-motion.js', 'text-engine.js']) await page.addScriptTag({path: path.resolve('web', file)});
  await page.evaluate(() => {
    window.engine = new TextLayoutEngine(document.querySelector('#stage'), document.querySelector('main'));
    window.longText = '您好，我是京东商家服务顾问。这通电话是和您确认店铺资质更新材料，目前营业执照名称与店铺备案不一致，您可以在商家后台上传新的证明文件。'.repeat(12);
    window.step = seconds => {
      if (engine.motion.queue.size) engine.motion.flush();
      engine.motion.paused = false; engine.motion.advance(seconds); engine.motion.paused = true;
    };
    window.settle = () => { for (let i = 0; i < 120; i++) step(1 / 60); };
  });

  report.collapse = [];
  for (const width of [900, 390]) {
    await page.setViewportSize({width, height: 720});
    await page.waitForTimeout(50);
    const result = await page.evaluate(() => {
      engine.reset(); engine.pause(true);
      engine.begin('long', 'assistant', {text: longText}); engine.motion.flush(); settle();
      const viewport = engine.viewport, ink = engine.root.querySelector('.ink');
      const before = {scroll: viewport.scrollTop, y: ink.getBoundingClientRect().y};
      engine.begin('next', 'assistant', {text: '好的，那请您先确认一下。'}); engine.motion.flush();
      const immediate = {scroll: viewport.scrollTop, y: ink.getBoundingClientRect().y};
      const frames = [];
      for (let i = 0; i < 90; i++) {
        step(1 / 60);
        frames.push({scroll: viewport.scrollTop, expected: Math.max(0, engine.motion.scroll.value)});
      }
      return {before, immediate, frames, target: engine.motion.scroll.target,
        idleGlyphs: engine.motion.activeGlyphs.size, retained: ink === engine.root.querySelector('.ink')};
    });
    assert.ok(result.before.scroll > 600);
    assert.ok(Math.abs(result.immediate.scroll - result.before.scroll) <= 1, 'A layout shrink must not clamp scrollTop before the first animation frame.');
    assert.ok(Math.abs(result.immediate.y - result.before.y) <= 1, 'Retained text must remain still until animation advances.');
    assert.ok(result.frames.every(frame => Math.abs(frame.scroll - frame.expected) <= 1), 'Native scroll clamping must not fight the retained spring.');
    assert.ok(Math.abs(result.frames.at(-1).scroll - result.target) <= 1);
    assert.ok(result.retained && result.idleGlyphs === 0);
    report.collapse.push({width, before: result.before.scroll, immediate: result.immediate.scroll,
      settled: result.frames.at(-1).scroll, retained: result.retained, idleGlyphs: result.idleGlyphs});
  }

  report.revisions_and_history = await page.evaluate(() => {
    engine.reset(); engine.pause(true);
    for (let i = 0; i < 14; i++) engine.begin(`past-${i}`, i % 2 ? 'user' : 'assistant', {text: longText.slice(0, 150), history: true});
    const handle = engine.begin('live', 'user', {text: longText}); engine.motion.flush(); settle();
    const first = engine.root.querySelector('.glyph');
    let maxImmediateJump = 0;
    for (const length of [120, 720, 80, 400, 60]) {
      const before = engine.viewport.scrollTop;
      engine.revise(handle, longText.slice(0, length)); engine.motion.flush();
      maxImmediateJump = Math.max(maxImmediateJump, Math.abs(engine.viewport.scrollTop - before));
      for (let i = 0; i < 5; i++) step(1 / 60);
    }
    settle();
    engine.viewport.scrollTop -= 20;
    engine.viewport.dispatchEvent(new Event('scroll'));
    const reading = engine.viewport.scrollTop;
    engine.append(handle, '我们一步一步来。'); engine.motion.flush(); settle();
    const keptReading = !engine.motion.follow && Math.abs(engine.viewport.scrollTop - reading) <= 1;
    let splits = 0; const split = engine.motion.split.bind(engine.motion);
    engine.motion.split = text => { splits++; return split(text); };
    engine.append(handle, '谢谢。'); engine.motion.flush();
    engine.motion.split = split;
    engine.followLatest(); settle();
    return {maxImmediateJump, keptReading, splits, retained: first === engine.root.querySelector('.glyph'),
      atLatest: Math.abs(engine.viewport.scrollTop - engine.motion.scroll.target) <= 1,
      noHorizontalOverflow: document.documentElement.scrollWidth <= innerWidth};
  });
  assert.ok(report.revisions_and_history.maxImmediateJump <= 1);
  assert.ok(report.revisions_and_history.keptReading && report.revisions_and_history.retained && report.revisions_and_history.atLatest);
  assert.equal(report.revisions_and_history.splits, 1, 'Streaming one message must not segment every historical paragraph again.');
  assert.ok(report.revisions_and_history.noHorizontalOverflow);

  report.long_promotion = await page.evaluate(() => {
    engine.reset(); engine.pause(true);
    engine.setCandidates([{id: 'long-candidate', text: longText.slice(0, 180)}]); engine.motion.flush(); settle();
    const glyph = engine.root.querySelector('.candidate .glyph:not(.candidate-marker)');
    engine.promote('long-candidate', 'promoted'); engine.motion.flush();
    for (let i = 0; i < 70; i++) step(1 / 60);
    return {retained: glyph === engine.root.querySelector('[data-id="promoted"] .glyph'),
      pending: engine.motion.promotions.size, sizesCleared: [...engine.root.querySelectorAll('.ink')].every(ink => !ink.style.fontSize)};
  });
  assert.deepEqual(report.long_promotion, {retained: true, pending: 0, sizesCleared: true});
  await page.screenshot({path: path.join(out, 'long-text-motion.png')});
  assert.deepEqual(errors, []); report.passed = true;
} finally {
  await fs.writeFile(path.join(out, 'long-text-motion.json'), JSON.stringify({report, errors}, null, 2));
  await browser.close();
}
console.log(JSON.stringify(report, null, 2));
