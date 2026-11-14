// @ts-check
/**
 * boot.js —— 入口分发（index.html 只引这一个）
 *
 * 为什么需要它：新前端逻辑（Decision 流）和旧契约（ws_pcm 状态机）是两套装配，
 * 但共用同一个 DOM。用一次能力探测决定装哪一个，就不用维护两份页面。
 *
 *   ?mode=stream  强制新逻辑（后端还没合并时也能配合 mock 开发）
 *   ?mode=legacy  强制旧契约
 *   ?mode=auto    默认：探测 /ws/decision，通了走新逻辑，不通自动回落旧契约
 *
 * 探测本身不建会话：只开一条 WS，看到开房即认为可用，随即关掉。
 */

const cfg = /** @type {any} */ (window.__AICALL_CONFIG__ || {});
const mode = String(cfg.MODE || 'auto');
const path = (cfg.STREAM && cfg.STREAM.WS_PATH) || '/ws/decision';
const budget = (cfg.STREAM && cfg.STREAM.PROBE_TIMEOUT_MS) || 800;

if (mode === 'legacy') {
  import('./main.js');
} else if (mode === 'stream') {
  import('./stream-app.js');
} else {
  probeStream(streamUrl(cfg, path, budget)).then((ok) => {
    if (ok) {
      import('./stream-app.js');
    } else {
      // 回落旧契约：不报错、不打扰用户，只是少了新逻辑
      console.info('[boot] /ws/decision 不可用，回落 legacy（ws_pcm）');
      import('./main.js');
    }
  }).catch(() => { import('./main.js'); });
}

/** @param {any} cfg @param {string} path @param {number} budget */
function streamUrl(cfg, path, budget) {
  const base = String(cfg.APP_BASE || location.origin).replace(/^http/, 'ws');
  const q = new URLSearchParams({ probe: '1' });
  // 防御：探测预算也传给服务端，便于它直接拒绝
  q.set('t', String(budget));
  return `${base}${path}?${q.toString()}`;
}

/**
 * @param {string} url
 * @returns {Promise<boolean>}
 */
function probeStream(url) {
  return new Promise((resolve) => {
    let settled = false;
    let opened = false;
    let ws;
    const done = (ok) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { ws && ws.close(); } catch { /* noop */ }
      resolve(ok);
    };
    const timer = setTimeout(() => done(false), budget + 200);
    try {
      ws = new WebSocket(url);
    } catch {
      done(false);
      return;
    }
    ws.onopen = () => {
      opened = true;
      // 开房即认为可用；再稳 150ms 确认没被立刻踢掉
      setTimeout(() => done(opened), 150);
    };
    ws.onerror = () => done(false);
    ws.onclose = () => done(false);
  });
}
