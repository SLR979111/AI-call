/* Local, silent fixtures only. No microphone, model, socket or backend. */
(() => {
  const $ = id => document.getElementById(id);
  const viewport = $('viewport'), root = $('conversation');
  const engine = new TextLayoutEngine(root, viewport);
  const handles = new Map();
  const names = {full: '完整演示', stream: '流式文字', revision: '识别修正', prediction: '候选命中', interrupt: '中途打断'};
  const callExamples = new Map((window.CALL_EXAMPLES || []).map(example => [example.id, example]));
  for (const example of callExamples.values()) names[example.id] = example.title;
  const replies = ['我帮您核对一下授权链。', '您可以先看一下授权有效期。', '方便说一下审核提示吗？'];
  let scene = 'full', events = [], cursor = 0, elapsed = 0, speed = 1, playing = true;
  let done = false;
  let resumeAfterLibrary = false;
  const LETTER_MS = 110;

  // Fixtures send events only. Rendering and state transitions belong to the engine.
  function message(id, role, text, focus = true) {
    handles.set(id, engine.begin(id, role, {text, history: !focus}));
  }
  function text(id, value) { engine.revise(handles.get(id), value); }
  function signal(id, type) {
    engine.setInput({active: !!id && type === 'mic', level: 1});
  }
  function candidates() { engine.setCandidates(replies.map((text, index) => ({id: String(index), text}))); }
  function promote(index = 0) {
    const handle = engine.promote(String(index), 'answer');
    if (handle) handles.set('answer', handle);
    signal(null);
  }
  engine.addEventListener('candidatepick', event => {
    // Complete calls follow their script. Keep interactive candidate picking
    // for the short animation fixtures, where it is part of the demonstration.
    if (callExamples.has(scene)) return;
    events = []; done = true; playing = false; engine.pause(false);
    promote(event.detail.id); updateControls();
  });
  function at(time, action) { events.push({time, action}); }
  function stream(id, value, start, prefix = 0) {
    const chars = engine.graphemes(value);
    for (let i = prefix + 1; i <= chars.length; i++) {
      const chunk = chars[i - 1];
      at(start + (i - prefix - 1) * LETTER_MS, () => engine.append(handles.get(id), chunk));
    }
    return start + (chars.length - prefix) * LETTER_MS;
  }
  function seed(kind) {
    message('h1', 'assistant', '您好，我来帮您看一下资质材料。', false);
    message('h2', 'user', '我的店铺审核一直没通过。', false);
    message('h3', 'assistant', '收到，我们一起看一下。', false);
    if (kind === 'interrupt') message('question', 'user', '品牌授权要怎么补？');
    else message('question', 'assistant', '是品牌授权，还是营业执照的问题？');
  }
  function build(kind) {
    if (callExamples.has(kind)) return buildCall(callExamples.get(kind));
    seed(kind);
    if (kind === 'full' || kind === 'prediction') {
      at(600, () => { message('user', 'user', ''); signal('user', 'mic'); });
      const end = stream('user', '品牌授权。补了一次，还是没通过。', 850);
      at(1800, candidates);
      at(end + 1400, () => signal('question', 'thinking', '正在核对'));
      at(end + 3000, () => promote(0));
      const responseEnd = stream('answer', `${replies[0]}是缺少上一级授权，还是名称不一致？`, end + 4300, engine.graphemes(replies[0]).length);
      at(responseEnd + 300, () => signal(null));
      if (kind === 'full') {
        at(responseEnd + 1700, () => { message('user2', 'user', ''); signal('user2', 'mic'); });
        const next = stream('user2', '上面写的是公司明称不一致。', responseEnd + 1950);
        at(next + 700, () => text('user2', '上面写的是公司名称不一致。'));
        at(next + 2300, () => { message('answer2', 'assistant', ''); signal(null); });
        const answerEnd = stream('answer2', '明白，先核对授权书和营业执照上的公司名称。', next + 2550);
        at(answerEnd - 400, () => {
          // The interrupted stream is invalidated at the source timeline.
          engine.cancel(handles.get('answer2'));
          message('interrupt', 'user', ''); signal('interrupt', 'mic');
        });
        const interruptionEnd = stream('interrupt', '等一下，我想先确认另一件事。', answerEnd - 250);
        at(interruptionEnd + 600, () => signal(null));
        let nextStart = interruptionEnd + 2600;
        const continuation = [
          ['assistant', '好，您先说。我听着。'],
          ['user', '这家公司的名字，上个月刚改过。'],
          ['assistant', '那我们先看名称变更证明。您现在手边有这份材料吗？'],
          ['user', '有的。我上传的是变更前的版本。'],
          ['assistant', '明白了。可以先换成新的证明，再核对授权书上的名称。'],
          ['user', '那授权日期也要一起改吗？'],
          ['assistant', '先不用改日期。我们只核对它是否仍在有效期内。'],
          ['user', '我看到了，还没有到期。'],
          ['assistant', '好，这一项就先保留。我们再看授权链有没有缺失。'],
          ['user', '可以，你带我一项一项看。'],
          ['assistant', '我们先从品牌方开始，沿着每一级授权往下核对。'],
          ['user', '好，我把第一份授权书打开了。']
        ];
        continuation.forEach(([role, words], index) => {
          const id = `continuation-${index}`;
          at(nextStart, () => { message(id, role, ''); signal(role === 'user' ? id : null, 'mic'); });
          const end = stream(id, words, nextStart + 200);
          at(end + 600, () => signal(null)); nextStart = end + 3200;
        });
        at(nextStart, finish);
      } else at(responseEnd + 1000, () => finish());
    } else if (kind === 'stream') {
      at(500, () => { message('user', 'user', ''); signal('user', 'mic'); });
      const end = stream('user', '品牌授权。补了一次，还是没通过。', 750);
      at(end + 1300, () => { message('answer', 'assistant', ''); signal(null); });
      const end2 = stream('answer', '我帮您核对一下。审核提示里，有提到具体缺少哪一级授权吗？', end + 1600);
      at(end2 + 700, () => finish());
    } else if (kind === 'revision') {
      at(500, () => { message('user', 'user', ''); signal('user', 'mic'); });
      const end = stream('user', '提示是公司明称不一致。', 750);
      at(end + 900, () => text('user', '提示是公司名称不一致。'));
      at(end + 2600, () => text('user', '提示是公司名称不一致，不是授权过期。'));
      at(end + 4300, () => text('user', '提示是公司名称不一致，不是授权过期。对，就是这个。'));
      at(end + 6000, () => finish());
    } else {
      at(500, () => { message('answer', 'assistant', ''); signal(null); });
      const source = '您需要先检查品牌方到店铺的完整授权链，以及每一份授权的有效期。';
      const chars = engine.graphemes(source);
      // Stop the producer early, so delayed chunks cannot revive this reply.
      stream('answer', chars.slice(0, 21).join(''), 750);
      at(3100, () => { message('user', 'user', ''); signal('user', 'mic'); });
      const end = stream('user', '等一下，我已经补交过了。', 3300);
      at(end + 1100, () => { message('answer2', 'assistant', ''); signal(null); });
      const end2 = stream('answer2', '好，那我们直接看这次的审核提示。', end + 1350);
      at(end2 + 900, () => finish());
    }
    events.sort((a, b) => a.time - b.time);
    root.dataset.durationMs = events.at(-1).time;
  }
  function buildCall(example) {
    let nextStart = 450;
    example.turns.forEach(([role, words], index) => {
      const id = `call-${index}`;
      at(nextStart, () => { message(id, role, ''); signal(role === 'user' ? id : null, 'mic'); });
      const end = stream(id, words, nextStart + 140);
      at(end + 220, () => { engine.finish(handles.get(id)); signal(null); });
      nextStart = end + (role === 'user' ? 1050 : 1400);
    });
    at(nextStart, finish);
    events.sort((a, b) => a.time - b.time);
    root.dataset.durationMs = nextStart;
  }
  function finish() {
    signal(null); done = true; playing = false; root.dataset.playback = 'ended'; updateControls();
    $('demoStatus').textContent = callExamples.has(scene) ? '这通电话已结束，可重新播放或载入另一通。' : '演示已结束。';
  }
  function tick(delta) {
    if (playing && !done) {
      elapsed += delta * speed;
      while (cursor < events.length && events[cursor].time <= elapsed) {
        events[cursor++].action();
      }
    }
  }
  function updateControls() {
    $('play').setAttribute('aria-label', done ? '重新播放' : playing ? '暂停演示' : '继续演示');
    $('playIcon').setAttribute('d', playing && !done ? 'M7 5v10M13 5v10' : 'M7 4l8 6-8 6Z');
    if (!done) root.dataset.playback = playing ? 'playing' : 'paused';
    $('currentExample').textContent = `当前：${names[scene]}`;
    for (const button of document.querySelectorAll('[data-example]')) {
      button.setAttribute('aria-current', String(button.dataset.example === scene));
    }
  }
  function load(kind) {
    engine.reset(); handles.clear();
    scene = kind; events = []; cursor = 0; elapsed = 0; done = false; playing = true;
    root.dataset.scene = scene;
    $('latest').hidden = true;
    build(kind); engine.onFrame(tick); updateControls();
    $('demoStatus').textContent = `已载入：${names[kind]}。`;
  }
  function toggle() { if (done) return load(scene); playing = !playing; engine.pause(!playing); updateControls(); }
  $('play').addEventListener('click', toggle);
  $('replay').addEventListener('click', () => load(scene));
  $('speed').addEventListener('click', () => {
    speed = speed === 1 ? 2 : speed === 2 ? 4 : 1;
    $('speed').setAttribute('aria-label', `播放速度 ${speed} 倍`); $('speed').title = `播放速度 ${speed} 倍`;
    $('demoStatus').textContent = `播放速度 ${speed} 倍。`;
  });
  function openLibrary() {
    if ($('libraryDialog').open) return;
    resumeAfterLibrary = playing && !done;
    playing = false; engine.pause(true); updateControls();
    $('libraryDialog').showModal();
  }
  function chooseExample(kind) {
    resumeAfterLibrary = false;
    $('libraryDialog').close();
    const url = new URL(location.href); url.searchParams.set('scene', kind); url.searchParams.delete('library');
    history.replaceState(null, '', url);
    load(kind); $('play').focus();
  }
  function addExampleButton(container, id, title, description = '', meta = '') {
    const button = document.createElement('button');
    button.className = 'example-card'; button.dataset.example = id;
    for (const [className, content] of [['example-title', title], ['example-description', description], ['example-meta', meta]]) {
      if (!content) continue;
      const label = document.createElement('span'); label.className = className; label.textContent = content; button.append(label);
    }
    button.addEventListener('click', () => chooseExample(id)); container.append(button);
  }
  for (const example of callExamples.values()) {
    const ms = example.turns.reduce((duration, [role, words]) => duration + engine.graphemes(words).length * LETTER_MS + 140 + (role === 'user' ? 1050 : 1400), 450);
    const seconds = Math.round(ms / 1000);
    const duration = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    addExampleButton($('callExamples'), example.id, example.title, example.description, `${example.turns.length} 段对话 · ${duration}`);
  }
  for (const id of ['full', 'stream', 'revision', 'prediction', 'interrupt']) {
    addExampleButton($('motionExamples'), id, names[id]);
  }
  $('library').addEventListener('click', openLibrary);
  $('libraryClose').addEventListener('click', () => $('libraryDialog').close());
  $('libraryDialog').addEventListener('close', () => {
    if (resumeAfterLibrary && !done) { playing = true; engine.pause(false); }
    if (done) engine.pause(false);
    resumeAfterLibrary = false; updateControls();
  });
  document.addEventListener('keydown', event => {
    if ($('libraryDialog').open || event.target.closest('button, a, input, textarea, summary') || event.repeat) return;
    if (event.code === 'Space') { event.preventDefault(); toggle(); }
    if (event.key.toLowerCase() === 'r') load(scene);
  });
  engine.addEventListener('followchange', event => { $('latest').hidden = event.detail.following; });
  $('latest').addEventListener('click', () => engine.followLatest());
  window.addEventListener('pagehide', () => engine.destroy());
  const requested = new URLSearchParams(location.search).get('scene');
  load(Object.hasOwn(names, requested) ? requested : 'full');
  if (new URLSearchParams(location.search).get('library') === '1') openLibrary();
})();
