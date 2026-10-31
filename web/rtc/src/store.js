// @ts-check
/**
 * store.js —— 唯一可变状态容器（zustand 的零依赖替代）
 * 规则：只有 session/ 与 audio/ 允许写；ui/ 只读 + 订阅。
 * 不变量：UI 不得推导业务状态，一切以服务端 state 消息为准。
 */

/** @typedef {{replyId:string, turnId:string, sentences:Array<any>, interrupted:boolean, estimated:boolean, completed:boolean}} Reply */

const state = {
  // 会话
  phase: 'idle',              // idle | connecting | connected | ended | error
  sessionId: '',
  releaseId: '',
  versionSet: null,
  transport: '—',             // ws_pcm | livekit | —
  transportNote: '',          // 例如「媒体未配置」

  // 状态机（服务端权威）
  turnId: '',
  turnState: 'IDLE',
  flowState: '—',
  slots: /** @type {Record<string,unknown>} */ ({}),
  flowHighWater: -1,          // 进度条只前进不后退

  // 字幕
  lines: /** @type {Array<{id:string, who:'user'|'agent', text:string, final:boolean, struck?:boolean, estimated?:boolean}>} */ ([]),
  partialUser: '',

  // 播报
  replies: /** @type {Record<string, Reply>} */ ({}),
  currentReplyId: '',

  // 合规 / 工具 / 结果
  compliance: /** @type {Array<any>} */ ([]),
  tools: /** @type {Record<string, any>} */ ({}),
  result: null,
  /** 打断后"等待用户说话"的状态提示（纯 UI 文案，不是业务话术；业务话术一律由服务端下发） */
  hint: '',

  // 指标与调试
  db: -99,
  b0: null,
  b3: null,
  stopLatency: null,
  ttfb: null,
  downlink: '—',
  rtt: null,
  logs: /** @type {string[]} */ ([])
};

const subs = new Set();
/** @param {(s:typeof state)=>void} fn @returns {()=>void} */
export function subscribe(fn) { subs.add(fn); return () => subs.delete(fn); }
export function get() { return state; }
/** @param {Partial<typeof state>} patch */
export function set(patch) {
  Object.assign(state, patch);
  for (const fn of subs) { try { fn(state); } catch (e) { console.error(e); } }
}
export function log(msg) {
  const t = new Date().toISOString().slice(11, 23);
  state.logs.push(`[${t}] ${msg}`);
  if (state.logs.length > 400) state.logs.splice(0, state.logs.length - 400);
  set({ logs: state.logs });
}

/* ---------- 字幕与播报的小操作（放这里，避免 ui 层各自造结构） ---------- */

export function pushUserLine(text, final) {
  const last = state.lines[state.lines.length - 1];
  if (last && last.who === 'user' && !last.final) {
    last.text = text; last.final = !!final;
  } else {
    state.lines.push({ id: `u_${state.lines.length}`, who: 'user', text, final: !!final });
  }
  set({ lines: state.lines.slice() });
}

/**
 * agent_text 是**按句**下发（{sentence_id, text, final}），不是 token 增量：
 * 同一 reply 的多句要拼成一条字幕行，重复 sentence_id 只更新不追加。
 */
export function pushAgentSentence(replyId, sentenceId, text) {
  const idx = state.lines.findIndex((l) => l.who === 'agent' && l.replyId === replyId);
  if (idx < 0) {
    state.lines.push({
      id: `a_${state.lines.length}`, who: 'agent', text: text || '', final: true,
      struck: false, replyId, lastSentenceId: sentenceId
    });
    set({ lines: state.lines.slice() });
    return;
  }
  const line = state.lines[idx];
  const last = line.lastSentenceId == null ? -1 : line.lastSentenceId;
  if (sentenceId == null) {
    line.text = text || line.text;                 // 无 sentence_id：按整段覆盖
  } else if (sentenceId > last) {
    line.text = (line.text || '') + (text || '');   // 新句追加
    line.lastSentenceId = sentenceId;
  }                                               // 同句/乱序：忽略（幂等）
  set({ lines: state.lines.slice() });
}

export function pushAgentLine(text, replyId) {
  state.lines.push({
    id: `a_${state.lines.length}`, who: 'agent', text, final: true,
    struck: false, replyId
  });
  set({ lines: state.lines.slice() });
}

export function beginReply(replyId, turnId, estimated) {
  state.replies[replyId] = {
    replyId, turnId, sentences: [], interrupted: false, estimated: !!estimated, completed: false
  };
  set({ replies: { ...state.replies }, currentReplyId: replyId });
}

export function upsertSentence(replyId, sentence) {
  const r = state.replies[replyId];
  if (!r) return;
  const i = r.sentences.findIndex((s) => s.sentence_id === sentence.sentence_id);
  if (i >= 0) r.sentences[i] = { ...r.sentences[i], ...sentence };
  else r.sentences.push(sentence);
  r.sentences.sort((a, b) => a.sentence_id - b.sentence_id);
  set({ replies: { ...state.replies } });
}

export function completeReply(replyId, interrupted) {
  const r = state.replies[replyId];
  if (!r) return;
  r.completed = true;
  r.interrupted = !!interrupted;
  if (interrupted) {
    // 本次播报的 agent 行加删除线：用户能直观看到「被打断了」
    for (const ln of state.lines) {
      if (ln.who === 'agent' && ln.replyId === replyId) ln.struck = true;
    }
  }
  set({ replies: { ...state.replies }, lines: state.lines.slice() });
}

export function strikeReplyLines(replyId) {
  for (const ln of state.lines) if (ln.who === 'agent' && ln.replyId === replyId) ln.struck = true;
  set({ lines: state.lines.slice() });
}

export function resetForNewCall() {
  set({
    phase: 'connecting', sessionId: '', releaseId: '', transport: '—', transportNote: '',
    turnId: '', turnState: 'CONNECTING', flowState: '—', slots: {}, flowHighWater: -1,
    lines: [], partialUser: '', replies: {}, currentReplyId: '',
    compliance: [], tools: {}, result: null, hint: '',
    db: -99, b0: null, b3: null, stopLatency: null, ttfb: null, downlink: '—', rtt: null
  });
}
