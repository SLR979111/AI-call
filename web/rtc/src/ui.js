// @ts-check
/**
 * ui.js —— 纯渲染层：只读 store，不 import audio/ 与 session/
 * 规则：业务状态一律来自 store（其来源是服务端 state 消息）；UI 不推导、不兜底业务。
 */
import { FLOW_TO_STAGE, STAGE_LABELS, TURN_STATES } from './protocol.js';
import * as store from './store.js';

const $ = (id) => /** @type {HTMLElement} */ (document.getElementById(id));

let lastLinesSig = '';

/** @param {{onStart:Function,onHangup:Function,onMute:Function,onBarge:Function,onText:Function,onDemo:Function}} h */
export function initUi(h) {
  const stages = $('stages');
  stages.innerHTML = STAGE_LABELS.map((label, i) =>
    `<li data-i="${i}"><span class="dot">${i + 1}</span><span>${label}</span></li>`).join('');

  $('btnStart').addEventListener('click', () => h.onStart());
  $('btnHangup').addEventListener('click', () => h.onHangup());
  $('btnMute').addEventListener('click', () => h.onMute());
  $('btnBarge').addEventListener('click', () => h.onBarge());
  $('btnDemo').addEventListener('click', () => h.onDemo());

  const input = /** @type {HTMLInputElement} */ ($('txtInput'));
  const submit = () => {
    const text = input.value.trim();
    if (!text) return;
    h.onText(text);
    input.value = '';
  };
  $('btnSend').addEventListener('click', submit);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });

  store.subscribe(render);
  render(store.get());

  return {
    setConnected(ok) {
      $('btnStart').disabled = ok;
      $('btnHangup').disabled = !ok;
      $('btnMute').disabled = !ok;
      $('btnBarge').disabled = !ok;
      /** @type {HTMLButtonElement} */ ($('btnDemo')).disabled = !ok;
      input.disabled = !ok;
      /** @type {HTMLButtonElement} */ ($('btnSend')).disabled = !ok;
    },
    status(text, kind = '') {
      const el = $('status');
      el.textContent = text;
      el.style.color = kind === 'err' ? 'var(--bad)' : kind === 'ok' ? 'var(--ok)' : 'var(--dim)';
    }
  };
}

/** @param {any} s */
function render(s) {
  // 顶栏
  const lamp = $('lamp');
  lamp.className = 'lamp' + (s.phase === 'connected'
    ? (s.turnState === 'SPEAKING' ? ' busy' : ' on')
    : s.phase === 'error' ? ' err' : '');
  $('turnState').textContent = TURN_STATES.includes(s.turnState) ? s.turnState : s.turnState || 'IDLE';
  $('flowState').textContent = `flow: ${s.flowState || '—'}`;
  $('transport').textContent = `transport: ${s.transport}${s.transportNote ? ' · ' + s.transportNote : ''}`;
  $('rtt').textContent = `rtt: ${s.rtt == null ? '—' : Math.round(s.rtt) + 'ms'}`;
  $('ids').textContent = `session: ${s.sessionId || '—'}`;
  $('version').textContent = `release: ${s.releaseId || '—'}`;
  $('aiBadge').style.display = s.phase === 'connected' ? '' : 'none';

  // 字幕（仅在变化时重建，避免打断输入法与滚动跳动）
  const sig = s.lines.map((l) => `${l.id}:${l.final ? 1 : 0}:${l.struck ? 1 : 0}:${l.text.length}`).join('|');
  if (sig !== lastLinesSig) {
    lastLinesSig = sig;
    const box = $('transcript');
    box.innerHTML = s.lines.map((l) => `
      <div class="line ${l.who}">
        <div class="who">${l.who === 'user' ? '用户' : '数字员工'}${l.final ? '' : ' · 识别中'}${
          l.struck ? ' · <span class="cut">被打断</span>' : ''}</div>
        <div class="${l.struck ? 'struck' : ''}">${escapeHtml(l.text)}</div>
      </div>`).join('');
    box.scrollTop = box.scrollHeight;
  }

  // 估算标记（C6：tts_alignment=false 时游标是估算，必须显式标注）
  const anyEst = Object.values(s.replies || {}).some((r) => r.estimated);
  $('estimateBadge').textContent = anyEst ? '（游标：估算，tts_alignment=false）' : '';

  // 六阶段（只前进，不回退）
  const idx = s.flowHighWater;
  Array.from($('stages').children).forEach((li, i) => {
    li.className = i < idx ? 'done' : i === idx ? 'now' : '';
  });

  // 槽位
  const slotKeys = Object.keys(s.slots || {});
  $('slots').innerHTML = slotKeys.length
    ? slotKeys.map((k) => `<div class="kv"><span>${escapeHtml(k)}</span><b>${escapeHtml(String(s.slots[k]))}</b></div>`).join('')
    : '<span class="hint">—</span>';

  // 合规
  const comp = s.compliance || [];
  $('compliance').innerHTML = comp.length
    ? comp.slice(-4).map((c) => `
        <div class="kv"><span>${escapeHtml(c.decision || '—')} ${(c.rule_ids || []).join(',')}</span>
        <b class="${c.decision === 'BLOCK' ? 'cut' : ''}">${escapeHtml(String(c.after || c.before || '').slice(0, 26))}</b></div>`).join('')
    : '<span class="hint">无命中</span>';

  // 工具
  const tools = Object.values(s.tools || {});
  $('tools').innerHTML = tools.length
    ? tools.slice(-3).map((t) => `
        <div class="kv"><span>${escapeHtml(t.tool || t.name || 'tool')}</span>
        <b>${escapeHtml(t.state || '')}${t.latency_ms != null ? ' · ' + Math.round(t.latency_ms) + 'ms' : ''}</b></div>`).join('')
    : '';

  // 结果（session.onTurnResult 已把真实 payload 归一化为 {outcome, ticket, newTime}）
  $('result').innerHTML = s.result
    ? `<div class="kv"><span>outcome</span><b>${escapeHtml(String(s.result.outcome || '—'))}</b></div>
       <div class="kv"><span>ticket</span><b>${escapeHtml(String(s.result.ticket || '—'))}</b></div>
       ${s.result.newTime ? `<div class="kv"><span>new_time</span><b>${escapeHtml(String(s.result.newTime))}</b></div>` : ''}`
    : '<span class="hint">未回写</span>';

  // 调试
  $('db').textContent = s.db > -90 ? s.db.toFixed(1) + ' dB' : '—';
  $('dbBar').style.width = Math.min(100, Math.max(0, (s.db + 60) * 2)) + '%';
  $('b0').textContent = s.b0 == null ? '—' : `${Math.round(s.b0)}ms`;
  $('b3').textContent = s.b3 == null ? '—' : `${Math.round(s.b3)}ms`;
  $('stopLat').textContent = s.stopLatency == null ? '—' : `${Math.round(s.stopLatency)}ms`;
  $('ttfb').textContent = s.ttfb == null ? '—' : `${Math.round(s.ttfb)}ms`;
  $('downlink').textContent = s.downlink || '—';
  $('log').textContent = (s.logs || []).slice(-60).join('\n');
}

/** @param {string} s */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
