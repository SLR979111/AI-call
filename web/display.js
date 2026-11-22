/**
 * display.js —— 展示用前端
 *
 * 协议完全沿用 web/app.js（g2）：一条 WS，
 * 上行 start / text / interrupt / played / playback_started / stop，
 * 下行 ready / reply_start / utterance / utterance_end / cancel / transcript /
 *     state / reply_done / latency / response_source / error + 12 字节头二进制音频。
 *
 * 与 app.js 的区别只有「视觉与交互」：
 *   - 来电页：整屏一个大电话图标，点它接听
 *   - 通话页：纯文字舞台（kinetic typography），不同人不同字号/颜色，可旋转/竖排
 *   - 点屏幕任意处 = 打断（走 interrupt，不再等后端 cancel）
 *
 * 复用 audio.js 的 Player / Microphone：AEC3 + 回声参考 + 每 70ms 的 played 回执
 * 驱动服务端 checkpoint，这部分一行都不能省。
 */
import {prepareTask, checkPreparation} from './preparation.js';
import {CallConnection} from './connection.js';
import { SCRIPT } from './display-script.js';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const DEMO = params.get('demo') === '1';

/** 真实话术长度的样片（?long=1 用它预览，验证字号在真实数据下是否合适） */
const REAL = [
  ['ai', '您好，这里是京东商家资质审核模拟客服，想跟您聊一下这次提交的品牌资料。'],
  ['user', '嗯，你说吧，我这边正在看。'],
  ['ai', '您这次提交的商标状态截图还是旧的，需要补一张当前状态页的截图，另外授权书上的主体名称和店铺名称也对不上。'],
  ['user', '那我重新截一张就行吗？'],
  ['ai', '对，截当前状态页，能看到商标名称、注册号和状态那一栏就行，截完回到京麦的资质管理里重新提交。'],
];


const PHASE = {
  ready: '已接通',
  thinking: '在想',
  speaking: '说话中 · 可随时打断',
  listening: '在听',
};

let call = null;
let voice = false;
let connected = false;
let epoch = 0;
let task = null;
let startedAt = 0;
let timer = 0;
let lineSeq = 0;
let interrupted = false;
/** @type {Map<string, HTMLElement>} epoch:unit → 元素 */
const lines = new Map();

/* ============================ 文字舞台 ============================ */

function seeded(n) {
  const x = Math.sin(n * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

/** 舞台同时保留几句、以及每旧一句缩多少（?lines= 可切换紧凑度） */
const MAX_LINES = Math.max(2, Math.min(8, Number(params.get('lines') || 4)));

/** 每个角色最多保留几句：默认总量 4 → AI 2 句 + 用户 2 句 */

const PER_ROLE = Math.max(1, Math.ceil(MAX_LINES / 2));
const DECAY = [
  [1], [1, .7], [1, .7, .52], [1, .7, .52, .4],
  [1, .72, .52, .38, .3], [1, .74, .58, .46, .36, .3],
  [1, .76, .62, .52, .44, .37, .32], [1, .78, .64, .55, .47, .41, .36, .32],
][MAX_LINES - 1];

// 句数多时把行距拉开一点，避免旋转后的宽块互相压住
if (MAX_LINES >= 5) {
  const words = document.getElementById('words');
  if (words) words.style.gap = '1.5vh';
}

/**
 * 字号模型：**年龄决定档位，句长只在档内微调**。
 * 之前是"按句长查表 × 年龄衰减"，两个轴互相打架：老的长句会显得比新的短句还大。
 * 现在 TOP × DECAY[age] × LEN_SCALE[len] —— 最新的一句永远最大，长句自动小一档。
 */
const TOP = 122;   // 最新一句的基准字号（1440 宽下）

/** 句长 → 档内缩放。真实话术一句常 30~60 字，必须压下来，否则整屏都是字 */
const LEN_SCALE = [
  [4, 1], [8, .92], [12, .84], [18, .74], [26, .66], [40, .58], [60, .5], [999, .44],
];

function lenScale(len) {
  for (const [maxLen, k] of LEN_SCALE) if (len <= maxLen) return k;
  return LEN_SCALE[LEN_SCALE.length - 1][1];
}

/** 字号：档位 × 句长系数 × 视口系数 */
function sizeFor(role, len, age) {
  const vw = Math.max(360, window.innerWidth || 1440);
  const factor = DECAY[Math.min(age, DECAY.length - 1)];
  const size = TOP * factor * lenScale(len) * Math.min(1, vw / 1600);
  // 短句额外保证一行放得下（hero 是 nowrap，太长会被裁掉）
  const cap = len <= 8 ? (vw * 0.84) / Math.max(len, 2) : Infinity;
  return Math.max(16, Math.round(Math.min(size, cap)));
}

function ageLines() {
  const all = [...$('words').children];
  all.reverse().forEach((el, i) => {
    const age = Math.min(i, DECAY.length - 1);
    el.dataset.age = String(age);
    // 越旧越淡；透明度也在这里算，CSS 只负责动效
    el.style.opacity = String(DECAY[age]);
    // 字号随年龄变小 → 布局真的收紧，旧句不会把整页顶出屏幕
    const t = el.firstElementChild;
    if (t) t.style.setProperty('--fs', `${sizeFor(el.dataset.role, Number(el.dataset.len) || 0, i)}px`);
  });
}

function trimLines() {
  // 按角色分别限流：AI 最多 PER_ROLE 句、用户最多 PER_ROLE 句（默认各 2 句）。
  // 从**最新往旧**数，超额的才删，所以"正在说的这句"永远留得住。
  const all = [...$('words').children];
  let ai = 0, user = 0;
  for (let i = all.length - 1; i >= 0; i--) {
    const isAi = all[i].dataset.role === 'ai';
    if (isAi) ai++; else user++;
    if ((isAi && ai > PER_ROLE) || (!isAi && user > PER_ROLE)) all[i].remove();
  }
}

/**
 * 画一行。
 * @param {'ai'|'user'} role @param {string} text @param {string|null} key
 */
function addLine(role, text, key = null) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return null;

  const index = ++lineSeq;
  const el = document.createElement('div');
  el.className = `line ${role}`;
  el.dataset.role = role;
  el.dataset.len = String(clean.length);

  // AI 短句走"巨型竖列"，长句走"大号横排"；两者都允许被屏幕边缘裁掉
  if (clean.length <= 8) el.classList.add('hero');

  const t = document.createElement('span');
  t.className = 't';
  t.style.setProperty('--fs', `${sizeFor(role, clean.length, 0)}px`);

  if (role === 'user') {
    // 用户句：逐词脱模糊入场。中文按字切，含空格时按词切（空格保留成文本节点，
    // 否则 inline-block 会把词间距吃掉）。--i 是序号，--step 是每词间隔，
    // 长句自动压缩间隔，避免 40 个字排到两秒以后才出来。
    const tokens = /\s/.test(clean) ? clean.split(/(\s+)/) : [...clean];
    const step = Math.min(64, Math.round(900 / Math.max(tokens.length, 1)));
    t.style.setProperty('--step', `${step}ms`);
    let n = 0;
    for (const token of tokens) {
      if (!token) continue;
      if (/^\s+$/.test(token)) { t.append(document.createTextNode(token)); continue; }
      const w = document.createElement('span');
      w.className = 'w';
      w.style.setProperty('--i', String(n++));
      w.textContent = token;
      t.append(w);
    }
  } else {
    t.textContent = clean;
  }
  el.append(t);

  $('words').append(el);
  trimLines();      // 先删超额的旧句，再按最终顺序算年龄/字号
  ageLines();
  if (key) lines.set(key, el);
  return el;
}

// 窗口尺寸变了要重算字号，否则投影切换分辨率会失真
window.addEventListener('resize', () => ageLines());

function markCancelled(cancelledEpoch) {
  for (const [key, el] of lines) {
    if (key.startsWith(`${cancelledEpoch}:`)) el.classList.add('struck');
  }
}

function setPartial(text) {
  $('partial').textContent = text ? String(text) : '';
}

/* ============================ 连接 ============================ */

function send(event) {
  call?.send(event);
}

function showError(text) {
  const el = $('err');
  el.textContent = text;
  el.classList.remove('hidden');
}
function clearError() { $('err').classList.add('hidden'); }

async function start(useVoice) {
  if (call) return;
  const active = call = new CallConnection({
    onMessage, onError: showError, onClose: () => finish('通话已结束')
  });
  hideHint();
  clearError();
  // 先切到通话界面再等授权：否则浏览器弹麦克风权限时用户会觉得"点了没反应"
  showCallView();
  $('phase').textContent = '接通中';
  $('incomingSub').textContent = '正在接通…';
  $('callBtn').disabled = true;
  try {
    voice = useVoice;
    await active.resumeAudio(voice);
    if (call !== active) return;
    $('phase').textContent = '正在准备工单';
    const status = await prepareTask(task, {retry: true});
    if (call !== active) return;
    checkPreparation(status);
    await active.open();
    if (call !== active) return;
    connected = true;
    epoch = 0; lineSeq = 0; interrupted = false;
    lines.clear();
    $('words').replaceChildren();
    setPartial('');
    startedAt = performance.now();
    timer = setInterval(tick, 500);

    send({ type: 'start', task, voice, audio_protocol: 2, predictions: true });
    $('phase').textContent = '等待应答';
  } catch (error) {
    if (call !== active) return;
    await finish('未能接通');
    if (call) return;
    showError(error.name === 'NotAllowedError'
      ? '没有获得麦克风权限。允许后可重试。'
      : error.message);
    $('callBtn').disabled = false;
    $('incomingSub').textContent = '点击重试';
  }
}

function tick() {
  const seconds = Math.floor((performance.now() - startedAt) / 1000);
  $('timer').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function showCallView() {
  $('incoming').classList.add('hidden');
  $('call').classList.remove('hidden');
  $('dot').classList.add('live');
}

async function finish(label = '') {
  connected = false;
  clearInterval(timer);
  const active = call;
  call = null;
  const closing = active?.close();
  $('call').classList.add('hidden');
  $('incoming').classList.remove('hidden');
  $('callBtn').disabled = false;
  $('incomingSub').textContent = label || '点击接听';
  await closing;
}

/* ============================ 下行 ============================ */

function onMessage({ data }) {
  if (data instanceof ArrayBuffer) { call?.player?.packet(data); return; }
  let event;
  try { event = JSON.parse(data); } catch { return; }

  switch (event.type) {
    case 'ready':
      if (event.task) task = event.task;
      $('phase').textContent = PHASE.ready;
      clearError();
      break;

    case 'reply_start':
      epoch = event.epoch;
      call?.player?.begin(epoch);
      $('latency').textContent = '';
      break;

    case 'utterance':
      if (event.epoch !== epoch || call?.player?.blocked.has(event.epoch)) break;
      call?.player?.unit(event);
      addLine('ai', event.text, `${event.epoch}:${event.unit}`);
      setPartial('');
      break;

    case 'utterance_end':
      call?.player?.finish(event);
      break;

    case 'cancel':
      call?.player?.cancel(event.epoch);
      markCancelled(event.epoch);
      $('phase').textContent = PHASE.listening;
      break;

    case 'transcript':
      if (event.final) { setPartial(''); addLine('user', event.text); }
      else setPartial(event.text ? `听到：${event.text}` : '');
      break;

    case 'state':
      $('phase').textContent = PHASE[event.phase] || PHASE.listening;
      $('dot').classList.toggle('live', event.phase !== 'ready');
      break;

    case 'latency':
      if (event.voice_to_playback_ms != null) {
        $('latency').textContent = `接话 ${(event.voice_to_playback_ms / 1000).toFixed(2)}s`;
      }
      break;

    case 'reply_done':
    case 'response_source':
      break;

    case 'error':
      showError(event.message || '出错了');
      break;
    default:
      break;
  }
}

/* ============================ 交互 ============================ */

function hideHint() { $('hint')?.classList.add('fade'); }

function interrupt(source = 'manual') {
  if (!connected || !epoch) return;
  const progress = call?.player?.stop(true) || [];
  markCancelled(epoch);
  send({ type: 'interrupt', epoch, progress, source });
  setPartial('');
  hideHint();
  $('phase').textContent = PHASE.listening;
  interrupted = true;
}


$('stopBtn').addEventListener('click', async (ev) => {
  ev.stopPropagation();
  send({ type: 'stop' });
  await finish('已挂断');
});

// 点舞台任意处 = 打断（"按一下就能插话"）
$('call').addEventListener('click', (ev) => {
  if (ev.target.closest('#stopBtn')) return;
  interrupt('manual');
});

window.addEventListener('beforeunload', () => {
  call?.close();
});

/* ============================ 启动 ============================ */

if (DEMO) {
  // 纯视觉预览：不连后端、不开麦，直接喂一段脚本对话
  $('incomingSub').textContent = '演示模式 · 点击接听';
  if (params.get('auto') === '1') {
    // 截图 / 投影预览用：跳过点击直接演
    runDemo();
  } else {
    $('callBtn').addEventListener('click', () => runDemo(), { once: true });
  }
} else {
  try {
    const config = await (await fetch('/api/config')).json();
    task = config.default_task;
    if (config.configured) void prepareTask(task, {wait: false});
    $('callBtn').addEventListener('click', () => start(true));
    $('incomingSub').textContent = config.configured
      ? '点击接听'
      : '服务未配置 API 凭据（.env）';
    if (!config.configured) showError('服务尚未配置 API 凭据，请在 根目录的 .env 中填写后重启。');
    // ?auto=1：跳过点击直接接听，供无头截图 / 自动验收用
    if (params.get('auto') === '1') start(true);
  } catch {
    $('incomingSub').textContent = '服务未启动';
    showError('无法载入任务配置，请确认本地服务已启动。');
  }
}

async function runDemo() {
  showCallView();
  startedAt = performance.now();
  timer = setInterval(tick, 500);
  $('phase').textContent = '演示';

  const push = (who, text) => {
    if (who === 'user') { setPartial(''); addLine('user', text); return; }
    addLine('ai', text);
  };

  // ?at=N：直接铺到第 N 句（用于截图 / 逐段确认构图）
  const SOURCE = params.get('long') === '1' ? REAL : SCRIPT;
  const at = Number(params.get('at') || 0);
  if (at > 0) {
    for (const [who, text] of SOURCE.slice(0, at)) push(who, text);
  } else {
    // ?gap=毫秒：连播节奏
    const gap = Math.max(300, Number(params.get('gap') || 1500));
    let i = 0;
    const play = setInterval(() => {
      if (i >= SOURCE.length) { clearInterval(play); return; }
      const [who, text] = SOURCE[i++];
      push(who, text);
    }, gap);
  }

  $('stopBtn').onclick = async (ev) => { ev.stopPropagation(); clearInterval(timer); await finish('演示结束'); location.reload(); };
}
