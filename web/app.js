(() => {
  "use strict";

  const API_BASE = /^https?:$/.test(window.location.protocol)
    ? window.location.origin
    : "http://localhost:8080";
  const CLIENT_VERSION = "web@0.1";
  const CAPABILITIES = ["local_vad", "aec"];
  const MAX_EVENTS = 50;
  const MAX_RECONNECTS = 2;
  const REQUEST_TIMEOUT_MS = 10000;

  const els = {
    connectionPill: document.getElementById("connectionPill"),
    connectionText: document.getElementById("connectionText"),
    liveTag: document.getElementById("liveTag"),
    startBtn: document.getElementById("startBtn"),
    startBtnText: document.getElementById("startBtnText"),
    bargeBtn: document.getElementById("bargeBtn"),
    muteBadge: document.getElementById("muteBadge"),
    sessionMeta: document.getElementById("sessionMeta"),
    sessionShort: document.getElementById("sessionShort"),
    chatLog: document.getElementById("chatLog"),
    emptyState: document.getElementById("emptyState"),
    composer: document.getElementById("composer"),
    messageInput: document.getElementById("messageInput"),
    sendBtn: document.getElementById("sendBtn"),
    turnState: document.getElementById("turnState"),
    flowState: document.getElementById("flowState"),
    turnId: document.getElementById("turnId"),
    slotCount: document.getElementById("slotCount"),
    slotsList: document.getElementById("slotsList"),
    eventCount: document.getElementById("eventCount"),
    eventLog: document.getElementById("eventLog"),
    clearLogBtn: document.getElementById("clearLogBtn"),
    toastRegion: document.getElementById("toastRegion"),
  };

  const speechSupported = Boolean(
    window.speechSynthesis &&
    typeof window.SpeechSynthesisUtterance === "function"
  );

  const app = {
    session: null,
    socket: null,
    socketSeq: 0,
    wsOpen: false,
    creating: false,
    reconnectAttempts: 0,
    reconnectTimer: null,
    currentTurnId: null,
    muted: false,
    audioContext: null,
    voices: [],
    events: [],
    pendingUserMessages: [],
    transcriptByTurn: new Map(),
    agentBubbles: new Map(),
    firstFrameSent: new Set(),
    speechNoticeShown: false,
    emptyStateTemplate: null,
  };

  /* Utilities ------------------------------------------------------------- */

  function formatClock(date = new Date()) {
    const hh = String(date.getHours()).padStart(2, "0");
    const mm = String(date.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  }

  function formatLogTime(date = new Date()) {
    const hh = String(date.getHours()).padStart(2, "0");
    const mm = String(date.getMinutes()).padStart(2, "0");
    const ss = String(date.getSeconds()).padStart(2, "0");
    const ms = String(date.getMilliseconds()).padStart(3, "0");
    return `${hh}:${mm}:${ss}.${ms}`;
  }

  function round(value, digits = 1) {
    if (!Number.isFinite(value)) return 0;
    const factor = 10 ** digits;
    return Math.round(value * factor) / factor;
  }

  function compactJson(value, maxLength = 260) {
    let text;
    try {
      text = JSON.stringify(value);
    } catch {
      text = String(value);
    }
    if (text === undefined) text = "";
    return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
  }

  function fullJson(value) {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }

  function normalizeText(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function redactUrl(rawUrl) {
    try {
      const url = new URL(rawUrl);
      if (url.searchParams.has("token")) {
        url.searchParams.set("token", "***");
      }
      return url.toString();
    } catch {
      return String(rawUrl).replace(/([?&]token=)[^&]+/, "$1***");
    }
  }

  function describeError(error) {
    if (!error) return "未知错误";
    if (error.name === "AbortError") return "请求超时（10 秒）";
    if (error instanceof TypeError) {
      return `无法连接服务器，请确认 FastAPI 已在 ${API_BASE} 启动`;
    }
    return error.message || String(error);
  }

  function makeAgentKey(turnId, replyId) {
    return `${turnId || "no-turn"}|${replyId || "no-reply"}`;
  }

  function makeReplyKey(turnId, replyId) {
    return makeAgentKey(turnId, replyId);
  }

  /* Connection chrome ----------------------------------------------------- */

  function setConnection(state, label) {
    document.body.dataset.connection = state;
    els.connectionPill.dataset.state = state;
    els.connectionText.textContent = label;
    const liveLabels = {
      idle: "IDLE",
      connecting: "SYNC",
      connected: "LIVE",
      reconnecting: "SYNC",
      error: "OFFLINE",
    };
    els.liveTag.textContent = liveLabels[state] || "IDLE";
  }

  function updateControls() {
    const canChat = app.wsOpen;
    els.startBtn.disabled = app.creating;
    els.startBtn.dataset.loading = app.creating ? "true" : "false";
    els.startBtn.setAttribute("aria-busy", String(app.creating));
    els.startBtnText.textContent = app.creating ? "创建中…" : "发起会话";
    els.messageInput.disabled = !canChat;
    els.bargeBtn.disabled = !canChat;
    els.sendBtn.disabled = !canChat || !els.messageInput.value.trim();
  }

  function setMuted(muted) {
    app.muted = muted;
    els.muteBadge.hidden = !muted;
  }

  function setCurrentTurnId(turnId, authoritative = false) {
    if (!turnId && !authoritative) return;
    if (app.currentTurnId && !authoritative) return;
    app.currentTurnId = turnId || null;
    els.turnId.textContent = turnId || "—";
    els.turnId.title = turnId || "";
  }

  function updateSessionMeta(session) {
    if (!session) {
      els.sessionMeta.hidden = true;
      els.sessionShort.textContent = "—";
      els.sessionMeta.removeAttribute("title");
      return;
    }
    els.sessionMeta.hidden = false;
    els.sessionShort.textContent = session.session_id;
    els.sessionShort.title = session.session_id;
    const version = session.version_set
      ? JSON.stringify(session.version_set, null, 2)
      : "未提供";
    els.sessionMeta.title = `session_id: ${session.session_id}\nversion_set:\n${version}`;
  }

  /* Toasts / chat --------------------------------------------------------- */

  function dismissToast(toast) {
    if (!toast.isConnected) return;
    toast.classList.add("is-leaving");
    window.setTimeout(() => toast.remove(), 200);
  }

  function showToast(message, tone = "info", timeout = 5000) {
    if (!message) return;
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.dataset.tone = tone;

    const dot = document.createElement("span");
    dot.className = "toast-dot";
    dot.setAttribute("aria-hidden", "true");
    const text = document.createElement("span");
    text.textContent = message;
    toast.append(dot, text);

    toast.addEventListener("click", () => dismissToast(toast));
    els.toastRegion.append(toast);

    while (els.toastRegion.children.length > 3) {
      els.toastRegion.firstElementChild.remove();
    }

    window.setTimeout(() => dismissToast(toast), timeout);
  }

  function removeEmptyState() {
    const empty = els.chatLog.querySelector(".empty-state");
    if (empty) empty.remove();
  }

  function scrollChat(force = false) {
    const log = els.chatLog;
    const distanceToBottom = log.scrollHeight - log.scrollTop - log.clientHeight;
    if (force || distanceToBottom < 120) {
      window.requestAnimationFrame(() => {
        log.scrollTop = log.scrollHeight;
      });
    }
  }

  function createMessageHead(author, stateText) {
    const head = document.createElement("div");
    head.className = "message-head";

    const authorEl = document.createElement("span");
    authorEl.className = "message-author";
    authorEl.textContent = author;

    const timeEl = document.createElement("time");
    timeEl.className = "message-time";
    timeEl.textContent = formatClock();

    const stateEl = document.createElement("span");
    stateEl.className = "message-state";
    stateEl.textContent = stateText || "";

    head.append(authorEl, timeEl);
    if (stateText) head.append(stateEl);
    return { head, stateEl };
  }

  function createUserMessage(text, { pending = false } = {}) {
    const article = document.createElement("article");
    article.className = "message message--user";
    if (pending) article.classList.add("is-pending");

    const { head, stateEl } = createMessageHead("你", pending ? "发送中" : "已转写");
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const paragraph = document.createElement("p");
    paragraph.textContent = text;
    bubble.append(paragraph);
    article.append(head, bubble);

    removeEmptyState();
    els.chatLog.append(article);
    scrollChat(true);
    return { article, bubble, paragraph, stateEl };
  }

  function createAgentMessage(turnId, replyId) {
    const article = document.createElement("article");
    article.className = "message message--agent";
    article.dataset.turnId = turnId || "";
    article.dataset.replyId = replyId || "";

    const { head, stateEl } = createMessageHead(
      "智语坐席",
      speechSupported ? "语音播报" : "文本回退"
    );
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    article.append(head, bubble);

    removeEmptyState();
    els.chatLog.append(article);
    scrollChat(false);

    return {
      article,
      bubble,
      stateEl,
      sentences: new Map(),
      sentenceIndex: 0,
    };
  }

  function addSystemMessage(text, tone = "info") {
    if (!text) return;
    removeEmptyState();
    const article = document.createElement("article");
    article.className = "message message--system";
    article.dataset.tone = tone;

    const dot = document.createElement("span");
    dot.className = "system-dot";
    dot.setAttribute("aria-hidden", "true");
    const copy = document.createElement("span");
    copy.textContent = text;

    article.append(dot, copy);
    els.chatLog.append(article);
    scrollChat(false);
  }

  function takePendingUserMessage(text) {
    if (!app.pendingUserMessages.length) return null;
    const target = normalizeText(text);
    for (let index = app.pendingUserMessages.length - 1; index >= 0; index -= 1) {
      if (normalizeText(app.pendingUserMessages[index].text) === target) {
        return app.pendingUserMessages.splice(index, 1)[0];
      }
    }
    return app.pendingUserMessages.shift();
  }

  function resetConversation() {
    cancelSpeech();
    app.pendingUserMessages = [];
    app.transcriptByTurn.clear();
    app.agentBubbles.clear();
    app.firstFrameSent.clear();
    app.speechNoticeShown = false;
    if (app.emptyStateTemplate) {
      els.chatLog.replaceChildren(app.emptyStateTemplate.cloneNode(true));
    } else {
      els.chatLog.replaceChildren();
    }
  }

  /* State / event panels -------------------------------------------------- */

  function formatSlotValue(value) {
    if (value === null) return "null";
    if (value === undefined) return "undefined";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    return fullJson(value);
  }

  function renderSlots(slots) {
    const entries =
      slots && typeof slots === "object" && !Array.isArray(slots)
        ? Object.entries(slots)
        : [];
    els.slotCount.textContent = `${entries.length} 项`;
    els.slotsList.replaceChildren();

    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "slot-empty";
      empty.textContent = "暂无槽位";
      els.slotsList.append(empty);
      return;
    }

    const fragment = document.createDocumentFragment();
    for (const [key, value] of entries) {
      const item = document.createElement("div");
      item.className = "slot-item";

      const keyEl = document.createElement("span");
      keyEl.className = "slot-key";
      keyEl.textContent = key;

      const valueEl = document.createElement("span");
      valueEl.className = "slot-value";
      const fullValue = formatSlotValue(value);
      valueEl.textContent =
        fullValue.length > 110 ? `${fullValue.slice(0, 109)}…` : fullValue;
      valueEl.title = fullValue;

      item.append(keyEl, valueEl);
      fragment.append(item);
    }
    els.slotsList.append(fragment);
  }

  function resetStatePanel() {
    app.currentTurnId = null;
    els.turnState.textContent = "—";
    els.flowState.textContent = "—";
    els.turnId.textContent = "—";
    els.turnId.title = "";
    renderSlots({});
  }

  function renderEvents() {
    els.eventCount.textContent = `${app.events.length} / ${MAX_EVENTS}`;
    els.eventLog.replaceChildren();

    if (!app.events.length) {
      const empty = document.createElement("p");
      empty.className = "event-empty";
      empty.textContent = "等待连接事件…";
      els.eventLog.append(empty);
      return;
    }

    const fragment = document.createDocumentFragment();
    for (const event of app.events) {
      const item = document.createElement("div");
      item.className = "event-item";
      item.dataset.direction = event.direction;
      item.title = event.fullJson;

      const meta = document.createElement("div");
      meta.className = "event-meta";

      const direction = document.createElement("span");
      direction.className = "event-dir";
      direction.textContent = event.direction === "in" ? "↓ 入站" : "↑ 出站";

      const type = document.createElement("span");
      type.className = "event-type";
      type.textContent = event.type;

      const time = document.createElement("time");
      time.className = "event-time";
      time.textContent = event.time;

      const json = document.createElement("code");
      json.className = "event-json";
      json.textContent = event.preview;

      meta.append(direction, type, time);
      item.append(meta, json);
      fragment.append(item);
    }
    els.eventLog.append(fragment);
  }

  function logEvent(direction, type, payload) {
    const now = new Date();
    app.events.unshift({
      direction,
      type: String(type || "message"),
      time: formatLogTime(now),
      preview: compactJson(payload, 260),
      fullJson: fullJson(payload),
    });
    if (app.events.length > MAX_EVENTS) app.events.length = MAX_EVENTS;
    renderEvents();
  }

  function clearEvents() {
    app.events = [];
    renderEvents();
  }

  /* Audio / speech -------------------------------------------------------- */

  function ensureAudioContext() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return null;
    if (!app.audioContext) {
      try {
        app.audioContext = new AudioContextClass();
      } catch {
        return null;
      }
    }
    if (app.audioContext.state === "suspended") {
      app.audioContext.resume().catch(() => {});
    }
    return app.audioContext;
  }

  function refreshVoices() {
    if (!speechSupported) return;
    try {
      app.voices = window.speechSynthesis.getVoices() || [];
    } catch {
      app.voices = [];
    }
  }

  function pickChineseVoice() {
    if (!app.voices.length) return null;
    return (
      app.voices.find((voice) => /^zh[-_]CN$/i.test(voice.lang)) ||
      app.voices.find((voice) => /^zh([-_]|$)/i.test(voice.lang)) ||
      app.voices.find((voice) => /chinese|mandarin|中文|普通话/i.test(voice.name)) ||
      null
    );
  }

  function cancelSpeech() {
    if (!speechSupported) return;
    try {
      window.speechSynthesis.cancel();
    } catch {
      // Speech synthesis is best-effort and must never block the demo.
    }
  }

  function sendPlaybackFirstFrame(turnId, replyId, replyKey) {
    const key = replyKey || makeReplyKey(turnId, replyId);
    if (app.firstFrameSent.has(key)) return;
    app.firstFrameSent.add(key);

    const audioContext = ensureAudioContext();
    const payload = {
      type: "playback_first_frame",
      turn_id: turnId || app.currentTurnId || null,
      reply_id: replyId || null,
      client_mono_ms: round(window.performance.now(), 1),
      audio_ctx_time: audioContext ? round(audioContext.currentTime, 4) : 0,
    };

    if (!sendMessage(payload)) {
      app.firstFrameSent.delete(key);
    }
  }

  function speakAgentText(text, turnId, replyId, replyKey) {
    if (!speechSupported) {
      if (!app.speechNoticeShown) {
        app.speechNoticeShown = true;
        addSystemMessage(
          "当前浏览器不支持语音合成，已显示文本并立即上报播放首帧。",
          "warning"
        );
      }
      sendPlaybackFirstFrame(turnId, replyId, replyKey);
      return;
    }

    refreshVoices();
    const utterance = new window.SpeechSynthesisUtterance(text);
    utterance.lang = "zh-CN";
    utterance.rate = 1.02;
    utterance.pitch = 1;
    utterance.volume = 1;
    const voice = pickChineseVoice();
    if (voice) utterance.voice = voice;

    utterance.onstart = () => {
      sendPlaybackFirstFrame(turnId, replyId, replyKey);
    };

    utterance.onerror = (event) => {
      const reason = String(event.error || "unknown");
      if (reason === "interrupted" || reason === "canceled") return;
      if (!app.firstFrameSent.has(replyKey)) {
        if (!app.speechNoticeShown) {
          app.speechNoticeShown = true;
          addSystemMessage(
            `语音播报未能启动（${reason}），已回退文本并上报播放首帧。`,
            "warning"
          );
        }
        sendPlaybackFirstFrame(turnId, replyId, replyKey);
      }
    };

    try {
      window.speechSynthesis.speak(utterance);
    } catch {
      if (!app.speechNoticeShown) {
        app.speechNoticeShown = true;
        addSystemMessage("语音播报启动失败，已回退文本并上报播放首帧。", "warning");
      }
      sendPlaybackFirstFrame(turnId, replyId, replyKey);
    }
  }

  /* WebSocket ------------------------------------------------------------- */

  function buildWsUrl(session) {
    const raw = session.ws_url || `/ws/session/${encodeURIComponent(session.session_id)}`;
    const url = new URL(raw, API_BASE);
    if (url.protocol === "http:") url.protocol = "ws:";
    if (url.protocol === "https:") url.protocol = "wss:";
    if (url.protocol !== "ws:" && url.protocol !== "wss:") {
      throw new Error(`不支持的 WebSocket 协议：${url.protocol}`);
    }
    if (session.token) url.searchParams.set("token", String(session.token));
    return url.toString();
  }

  function closeSocket(reason = "client close") {
    const socket = app.socket;
    app.socket = null;
    app.wsOpen = false;
    if (!socket) return;
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    try {
      socket.close(1000, reason);
    } catch {
      // Ignore close races; the socket is already detached from app state.
    }
  }

  function clearReconnectTimer() {
    if (app.reconnectTimer !== null) {
      window.clearTimeout(app.reconnectTimer);
      app.reconnectTimer = null;
    }
  }

  function sendMessage(payload) {
    const socket = app.socket;
    if (!socket || socket.readyState !== window.WebSocket.OPEN) {
      showToast("WebSocket 未连接，消息未发送。", "error");
      return false;
    }
    try {
      socket.send(JSON.stringify(payload));
      logEvent("out", payload.type || "message", payload);
      return true;
    } catch (error) {
      logEvent("out", "ws.send_error", { message: describeError(error) });
      showToast(`消息发送失败：${describeError(error)}`, "error");
      return false;
    }
  }

  function connectSocket(isReconnect = false) {
    if (!app.session) return;

    closeSocket("reconnect");
    const sequence = ++app.socketSeq;
    let socket;
    let wsUrl;

    try {
      wsUrl = buildWsUrl(app.session);
      socket = new WebSocket(wsUrl);
    } catch (error) {
      logEvent("in", "ws.construct_error", { message: describeError(error) });
      addSystemMessage(`无法建立 WebSocket：${describeError(error)}`, "error");
      scheduleReconnect();
      return;
    }

    app.socket = socket;
    app.wsOpen = false;
    setConnection(
      isReconnect ? "reconnecting" : "connecting",
      isReconnect ? `重连 ${app.reconnectAttempts}/${MAX_RECONNECTS}` : "连接中"
    );
    updateControls();

    socket.onopen = () => {
      if (sequence !== app.socketSeq) return;
      app.wsOpen = true;
      setConnection("connected", isReconnect ? "已重连" : "已连接");
      updateControls();
      logEvent("in", "ws.open", {
        url: redactUrl(wsUrl),
        reconnect: isReconnect,
      });
      if (isReconnect) {
        addSystemMessage("WebSocket 重连成功，正在重新握手。", "success");
      }
      sendMessage({
        type: "hello",
        session_id: app.session.session_id,
        client_version: CLIENT_VERSION,
        capabilities: CAPABILITIES,
      });
    };

    socket.onmessage = (event) => {
      if (sequence !== app.socketSeq) return;
      handleRawMessage(event.data);
    };

    socket.onerror = () => {
      if (sequence !== app.socketSeq) return;
      logEvent("in", "ws.error", { readyState: socket.readyState });
      showToast("WebSocket 连接异常，正在检查连接状态。", "error");
    };

    socket.onclose = (event) => {
      if (sequence !== app.socketSeq) return;
      app.socket = null;
      app.wsOpen = false;
      cancelSpeech();
      updateControls();
      logEvent("in", "ws.close", {
        code: event.code,
        reason: event.reason || "",
        wasClean: event.wasClean,
      });

      if (!app.session) return;

      if (app.reconnectAttempts < MAX_RECONNECTS) {
        setConnection("reconnecting", "连接中断");
        addSystemMessage(
          `连接已断开（${event.code}），将自动重连。`,
          "warning"
        );
        scheduleReconnect();
      } else {
        failConnection("连接已断开，自动重连 2 次仍失败。请重新发起会话。");
      }
    };
  }

  function scheduleReconnect() {
    if (!app.session) return;
    if (app.reconnectAttempts >= MAX_RECONNECTS) {
      failConnection("连接已断开，自动重连 2 次仍失败。请重新发起会话。");
      return;
    }

    clearReconnectTimer();
    app.reconnectAttempts += 1;
    const attempt = app.reconnectAttempts;
    const delay = 800 * attempt;
    setConnection("reconnecting", `重连 ${attempt}/${MAX_RECONNECTS}`);

    app.reconnectTimer = window.setTimeout(() => {
      app.reconnectTimer = null;
      connectSocket(true);
    }, delay);
  }

  function failConnection(message) {
    clearReconnectTimer();
    app.wsOpen = false;
    updateControls();
    setConnection("error", "连接失败");
    addSystemMessage(message, "error");
    showToast(message, "error", 7000);
  }

  /* Server message handling ---------------------------------------------- */

  function handleRawMessage(raw) {
    let message;
    try {
      message = JSON.parse(raw);
    } catch {
      logEvent("in", "invalid_json", { raw: String(raw).slice(0, 300) });
      showToast("收到无法解析的服务端消息。", "error");
      return;
    }

    if (!message || typeof message !== "object") {
      logEvent("in", "invalid_message", { raw: String(raw).slice(0, 300) });
      return;
    }

    const type = typeof message.type === "string" ? message.type : "unknown";
    logEvent("in", type, message);

    switch (type) {
      case "session_ready":
        handleSessionReady(message);
        break;
      case "state":
        handleState(message);
        break;
      case "transcript":
        handleTranscript(message);
        break;
      case "agent_text":
        handleAgentText(message);
        break;
      case "turn_end":
        handleTurnEnd(message);
        break;
      case "cancel":
        handleCancel(message);
        break;
      case "error":
        handleServerError(message);
        break;
      case "pong":
        // Pong is intentionally only recorded in the event log.
        break;
      default:
        break;
    }
  }

  function handleSessionReady(message) {
    const wasReconnecting = app.reconnectAttempts > 0;
    app.reconnectAttempts = 0;
    setConnection("connected", "会话就绪");

    if (message.version_set && app.session) {
      app.session.version_set = message.version_set;
      updateSessionMeta(app.session);
    }

    addSystemMessage(
      wasReconnecting ? "重连完成，会话已恢复，可以继续输入。" : "会话已就绪，可以开始输入。",
      "success"
    );
    if (wasReconnecting) showToast("重连完成，会话已恢复。", "success", 3500);
  }

  function handleState(message) {
    if (typeof message.turn_state === "string") {
      els.turnState.textContent = message.turn_state || "—";
    }
    if (typeof message.flow_state === "string") {
      els.flowState.textContent = message.flow_state || "—";
    }
    if (Object.prototype.hasOwnProperty.call(message, "slots")) {
      renderSlots(message.slots);
    }
    if (Object.prototype.hasOwnProperty.call(message, "turn_id")) {
      setCurrentTurnId(message.turn_id, true);
    }
  }

  function handleTranscript(message) {
    const text = String(message.text || "").trim();
    if (!text) return;

    const turnId = message.turn_id || app.currentTurnId || null;
    if (message.turn_id) setCurrentTurnId(message.turn_id);

    let record = turnId ? app.transcriptByTurn.get(turnId) : null;
    if (!record) {
      const pending = takePendingUserMessage(text);
      const created = pending
        ? {
            article: pending.element,
            bubble: pending.element.querySelector(".bubble"),
            paragraph: pending.element.querySelector(".bubble p"),
            stateEl: pending.element.querySelector(".message-state"),
          }
        : createUserMessage(text, { pending: false });

      record = created;
      record.article.classList.remove("is-pending");
      record.article.dataset.turnId = turnId || "";
      if (record.paragraph) record.paragraph.textContent = text;
      if (record.stateEl) record.stateEl.textContent = message.final ? "已转写" : "识别中";
      if (turnId) app.transcriptByTurn.set(turnId, record);
    } else {
      if (record.paragraph) record.paragraph.textContent = text;
      if (record.stateEl) record.stateEl.textContent = message.final ? "已转写" : "识别中";
      record.article.classList.toggle("is-pending", message.final === false);
    }

    record.article.classList.toggle("is-pending", message.final === false);
    scrollChat(false);
  }

  function handleAgentText(message) {
    const text = String(message.text || "").trim();
    if (!text) return;

    const turnId = message.turn_id || app.currentTurnId || null;
    const replyId = message.reply_id || null;
    if (message.turn_id) setCurrentTurnId(message.turn_id);

    const key = makeAgentKey(turnId, replyId);
    let record = app.agentBubbles.get(key);
    if (!record) {
      record = createAgentMessage(turnId, replyId);
      app.agentBubbles.set(key, record);
    }

    const sentenceId =
      message.sentence_id === undefined || message.sentence_id === null
        ? `idx-${record.sentenceIndex}`
        : String(message.sentence_id);
    record.sentenceIndex += 1;

    let paragraph = record.sentences.get(sentenceId);
    let isNewText = false;
    if (!paragraph) {
      paragraph = document.createElement("p");
      paragraph.textContent = text;
      record.bubble.append(paragraph);
      record.sentences.set(sentenceId, paragraph);
      isNewText = true;
    } else if (paragraph.textContent !== text) {
      paragraph.textContent = text;
      isNewText = true;
    }

    if (isNewText) {
      scrollChat(false);
      speakAgentText(text, turnId, replyId, makeReplyKey(turnId, replyId));
    }
  }

  function handleTurnEnd(message) {
    const result = message.result;
    let summary = "本轮结束";
    if (result && typeof result === "object") {
      const outcome = result.outcome || result.status || result.label;
      if (outcome) {
        summary = `本轮结束 · ${outcome}`;
      } else if (Object.keys(result).length) {
        summary = `本轮结束 · ${compactJson(result, 72)}`;
      }
    } else if (typeof result === "string" && result) {
      summary = `本轮结束 · ${result}`;
    }
    addSystemMessage(summary, "success");
  }

  function handleCancel(message) {
    const reason = message.reason || "UNKNOWN";
    cancelSpeech();
    if (reason === "BARGE_IN") setMuted(true);
    addSystemMessage(
      reason === "BARGE_IN" ? "已打断 · BARGE_IN" : `本轮已取消 · ${reason}`,
      "warning"
    );
    scrollChat(false);
  }

  function handleServerError(message) {
    const error = message.error;
    let text = "服务端返回错误";
    if (typeof error === "string" && error) {
      text = error;
    } else if (error && typeof error === "object") {
      const code = error.code ? `${error.code}: ` : "";
      text = `${code}${error.user_message || error.message || "未知错误"}`;
    }
    showToast(text, "error", 7000);
    addSystemMessage(`服务端错误 · ${text}`, "error");
    scrollChat(false);
  }

  /* User actions ---------------------------------------------------------- */

  async function postSession() {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(new URL("/sessions", API_BASE), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "text" }),
        cache: "no-store",
        signal: controller.signal,
      });

      const rawBody = await response.text();
      let data = null;
      if (rawBody) {
        try {
          data = JSON.parse(rawBody);
        } catch {
          data = null;
        }
      }

      if (!response.ok) {
        let detail = "";
        if (data) {
          if (typeof data.detail === "string") detail = data.detail;
          else if (data.error) {
            detail =
              typeof data.error === "string"
                ? data.error
                : data.error.message || data.error.code || "";
          } else if (typeof data.detail === "object") {
            detail = fullJson(data.detail);
          }
        }
        const error = new Error(
          detail || rawBody.slice(0, 240) || `HTTP ${response.status}`
        );
        error.status = response.status;
        throw error;
      }

      if (!data || typeof data.session_id !== "string" || !data.session_id) {
        throw new Error("创建会话响应缺少 session_id");
      }
      return data;
    } finally {
      window.clearTimeout(timer);
    }
  }

  async function startSession() {
    if (app.creating) return;

    app.creating = true;
    clearReconnectTimer();
    closeSocket("new session");
    cancelSpeech();
    setMuted(false);
    resetConversation();
    resetStatePanel();
    clearEvents();
    updateSessionMeta(null);
    app.session = null;
    app.reconnectAttempts = 0;
    setConnection("connecting", "连接中");
    updateControls();
    ensureAudioContext();

    logEvent("out", "POST /sessions", { mode: "text" });
    addSystemMessage("正在创建文本模式会话…");

    try {
      const session = await postSession();
      app.session = session;
      updateSessionMeta(session);
      logEvent("in", "session_created", {
        session_id: session.session_id,
        ws_url: session.ws_url,
        version_set: session.version_set,
      });
      addSystemMessage(`会话已创建 · ${session.session_id}`, "success");
      connectSocket(false);
    } catch (error) {
      const message = describeError(error);
      logEvent("in", "session_error", {
        status: error.status || null,
        message,
      });
      setConnection("error", "连接失败");
      addSystemMessage(`创建会话失败：${message}`, "error");
      showToast(`创建会话失败：${message}`, "error", 7000);
    } finally {
      app.creating = false;
      updateControls();
    }
  }

  function handleComposerSubmit(event) {
    event.preventDefault();
    const text = els.messageInput.value.trim();
    if (!text) return;
    if (!app.wsOpen) {
      showToast("WebSocket 未连接，暂时无法发送。", "error");
      return;
    }

    ensureAudioContext();
    const sent = sendMessage({ type: "text", text });
    if (!sent) return;

    const userMessage = createUserMessage(text, { pending: true });
    app.pendingUserMessages.push({ element: userMessage.article, text });

    els.messageInput.value = "";
    setMuted(false);
    updateControls();
    els.messageInput.focus();
  }

  function handleBargeIn() {
    if (!app.wsOpen) {
      showToast("WebSocket 未连接，无法发送打断。", "error");
      return;
    }

    ensureAudioContext();
    cancelSpeech();

    const clientTs = Date.now();
    const clientMono = round(window.performance.now(), 1);
    const speechStartedSent = sendMessage({
      type: "speech_started",
      client_ts_ms: clientTs,
      client_mono_ms: clientMono,
      source: "local_vad",
    });
    const localMutedSent = sendMessage({
      type: "local_muted",
      turn_id: app.currentTurnId || null,
      client_mono_ms: round(window.performance.now(), 1),
      reason: "barge_in",
    });

    if (!speechStartedSent || !localMutedSent) return;

    setMuted(true);
    addSystemMessage("已本地静音，打断事件已发送。", "warning");
    showToast("已发送打断，本地播放已静音。", "success", 3200);
    scrollChat(false);
  }

  /* Init ------------------------------------------------------------------ */

  function bindEvents() {
    els.startBtn.addEventListener("click", startSession);
    els.bargeBtn.addEventListener("click", handleBargeIn);
    els.composer.addEventListener("submit", handleComposerSubmit);
    els.messageInput.addEventListener("input", updateControls);
    els.clearLogBtn.addEventListener("click", clearEvents);

    window.addEventListener("beforeunload", () => {
      clearReconnectTimer();
      closeSocket("page unload");
      cancelSpeech();
    });

    window.addEventListener("offline", () => {
      showToast("网络已离线，连接可能中断。", "error");
    });

    window.addEventListener("online", () => {
      showToast("网络已恢复。", "success", 3000);
    });
  }

  function init() {
    app.emptyStateTemplate = els.emptyState.cloneNode(true);
    if (speechSupported) {
      refreshVoices();
      if (typeof window.speechSynthesis.addEventListener === "function") {
        window.speechSynthesis.addEventListener("voiceschanged", refreshVoices);
      } else {
        window.speechSynthesis.onvoiceschanged = refreshVoices;
      }
    }

    bindEvents();
    setConnection("idle", "未连接");
    resetStatePanel();
    renderEvents();
    updateControls();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
