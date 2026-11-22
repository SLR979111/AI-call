import * as store from './store.js';

const $ = id => document.getElementById(id);
const seconds = ms => Number.isFinite(ms) ? (ms / 1000).toFixed(2) + ' 秒' : '—';

function median(samples) {
  const values = samples.filter(Number.isFinite).sort((a, b) => a - b);
  if (!values.length) return null;
  return (values[Math.floor((values.length - 1) / 2)] + values[Math.floor(values.length / 2)]) / 2;
}

export function initTimingUi(h) {
  const view = new window.TextLayoutEngine($('transcript'), $('viewport'), { interactiveCandidates: false });
  const cases = $('contestCase');
  const source = $('inputSource');
  const audioFile = $('audioFile');
  const dialog = $('taskDialog');
  const input = $('textInput');
  const result = $('result');
  const timing = new Map();
  const lines = new Map();
  const timingEnabled = new URLSearchParams(location.search).has('timing');
  let mode = 'mic';
  let fileMode = false;
  let composing = false;

  $('timingPanel').hidden = !timingEnabled;
  source.value = h.initialSource;
  $('startVoice').onclick = () => start(source.value);
  $('startText').onclick = () => start('text');
  $('stop').onclick = () => h.onHangup();
  $('interrupt').onclick = () => h.onBarge();
  $('mute').onclick = () => showMute(h.onMute());
  $('toggleText').onclick = () => showText($('textForm').hidden);
  $('editTask').onclick = () => dialog.showModal();
  $('closeDialog').onclick = () => dialog.close();
  $('taskForm').onsubmit = event => { event.preventDefault(); dialog.close(); };
  $('latest').onclick = () => view.followLatest();
  view.addEventListener('followchange', event => { $('latest').hidden = event.detail.following; });
  input.oncompositionstart = () => { composing = true; };
  input.oncompositionend = () => { composing = false; };
  $('textForm').onsubmit = event => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || composing) return;
    h.onText(text);
    input.value = '';
  };
  audioFile.onchange = () => {
    const file = audioFile.files?.[0];
    audioFile.value = '';
    if (file) h.onAudioFile(file);
  };

  function showText(show) {
    $('textForm').hidden = !show;
    $('toggleText').setAttribute('aria-expanded', String(show));
    if (show) input.focus();
  }

  function showMute(muted) {
    const label = muted ? '恢复麦克风' : '暂停麦克风';
    $('mute').setAttribute('aria-pressed', String(muted));
    $('mute').setAttribute('aria-label', label);
    $('mute').title = label;
  }

  function start(nextMode) {
    mode = nextMode;
    timing.clear();
    lines.clear();
    view.reset();
    showMute(false);
    input.value = '';
    result.textContent = '';
    result.hidden = true;
    showTiming();
    h.onStart(mode);
  }

  function syncCallControls(locked, connected) {
    cases.disabled = locked || !cases.options.length;
    source.disabled = locked;
    $('startVoice').disabled = cases.disabled;
    $('startText').disabled = cases.disabled;
    $('startControls').hidden = connected;
    $('callControls').hidden = !connected;
  }

  function showTiming() {
    if (!timingEnabled) return;
    const rows = [...timing.values()];
    $('timingSummary').textContent = rows.length
      ? `本通话 ${rows.length} 轮 · 首声 P50 ${seconds(median(rows.map(row => row.first)))} · 正文 P50 ${seconds(median(rows.map(row => row.answer)))}`
      : '本通话时延 · 等待语音输入';
    $('timingRows').replaceChildren(...rows.map(({ first, answer }, index) => {
      const row = document.createElement('tr');
      for (const value of [index + 1, seconds(first), seconds(answer), 'POP 对话策略']) {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.append(cell);
      }
      return row;
    }));
  }

  store.subscribe(state => {
    for (const line of state.lines) {
      if (!line.text) continue;
      let entry = lines.get(line.id);
      if (!entry) {
        const handle = view.begin(line.id, line.who === 'user' ? 'user' : 'assistant', { text: line.text });
        entry = { handle, text: line.text };
        lines.set(line.id, entry);
      } else if (line.text !== entry.text) {
        view.revise(entry.handle, line.text);
        entry.text = line.text;
      }
      if (line.struck) view.cancel(entry.handle);
      else if (line.who === 'user' && line.final) view.finish(entry.handle);
    }
    // The receipt is set by the server's successful writeback and stays visible after hangup.
    if (state.result?.recordId) {
      result.textContent = `模拟回写已完成 · ${state.result.recordId}`;
      result.hidden = false;
    } else if (state.result?.outcome === 'writeback_failed' && result.hidden) {
      result.textContent = '模拟回写失败';
      result.hidden = false;
    }
    $('phase').textContent = ({ LISTENING: '在听，请说', THINKING: '正在组织回答', SPEAKING: '正在说话' })[state.turnState] || '';
    $('transport').textContent = state.transport;
    $('version').textContent = state.releaseId;
    const micActive = state.phase === 'connected' && mode === 'mic' && !fileMode &&
      $('mute').getAttribute('aria-pressed') === 'false';
    view.setInput({ active: micActive, level: Math.max(0, (state.db + 65) / 45) });
  });

  return {
    setContestCases(options, defaultId) {
      cases.replaceChildren(...options.map(({ id, label }) => new Option(label, id)));
      cases.value = defaultId;
      syncCallControls(false, false);
    },
    selectedCase() { return cases.value; },
    setStarting() { syncCallControls(true, false); },
    setConnected(ok, useFile = false) {
      const showTextOnConnect = ok && mode === 'text' && $('callControls').hidden;
      fileMode = useFile;
      syncCallControls(ok, ok);
      $('connection').textContent = ok ? '已连接' : '等待通话';
      $('mute').hidden = mode !== 'mic' || useFile;
      audioFile.hidden = !ok || !useFile || mode === 'text';
      audioFile.disabled = audioFile.hidden;
      if (!ok || showTextOnConnect) showText(showTextOnConnect);
      if (!ok) view.setInput({ active: false });
    },
    status(text, kind = '') {
      $('error').hidden = kind !== 'err';
      $('error').textContent = kind === 'err' ? text : '';
      $('callNotice').hidden = kind === 'err' || !text;
      $('callNotice').textContent = kind === 'err' ? '' : text;
    },
    recordTiming(turnId, elapsed, formal) {
      if (!timingEnabled || elapsed < 0) return;
      const row = timing.get(turnId) || { first: elapsed };
      if (formal && row.answer == null) row.answer = elapsed;
      timing.set(turnId, row);
      showTiming();
    }
  };
}
