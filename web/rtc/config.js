/**
 * config.js —— 运行期配置（无需构建；可被 URL 参数覆盖）
 *
 * 冻结依据：契约 v0.2.1（C5）
 *   - `POST /sessions` 返回**完整 ws_url（含 token）**，前端不再自行拼 query
 *   - APP_BASE 默认 **同源**（`location.origin`）：
 *       · 由后端托管（uvicorn 8081 / Docker）→ 自动连真后端，URL 不用带任何参数
 *       · 由 mock 托管（8099）        → 自动连 mock，同样不用带参数
 *       · 页面与后端不同源（如 5173 静态服务 + 8081 后端）→ 显式传 `?app=http://HOST:PORT`
 *     这样"接入真后端"不需要改代码、也不需要改 URL，更不会因为端口/域名变化而错连。
 *
 * 覆盖示例：
 *   http://127.0.0.1:5173/rtc/?app=http://192.168.1.20:8081&downlink=speech
 */
(function () {
  var q = new URLSearchParams(location.search);
  var appFromQuery = q.get('app');
  var cfg = {
    /** 后端基地址：默认同源；不同源时用 ?app= 覆盖 */
    APP_BASE: appFromQuery || location.origin,
    /** 便于自检：到底是同源还是覆盖来的（调试面板可显示） */
    APP_BASE_SOURCE: appFromQuery ? 'query' : 'same-origin',
    SCENARIO_ID: q.get('scenario') || 'service_outbound',
    RELEASE_ID: q.get('release') || null,

    /** 传输偏好：webrtc | ws_pcm | auto。
     *  W1：/rtc 返回 MEDIA_NOT_CONFIGURED(503) → 静默回落 ws_pcm（C1） */
    TRANSPORT_PREF: q.get('transport') || 'auto',

    /** 下行播放形态：auto | speech | pcm
     *  auto：tts_started 后 400ms 内收到 audio_chunk 用 pcm，否则用 speechSynthesis
     *  speech：W1 Fake TTS 形态（服务端只发 tts_* 元数据，音频由浏览器合成）
     *  pcm：W2 真实 TTS 形态（服务端下行 24k PCM 块） */
    DOWNLINK: q.get('downlink') || 'auto',

    UPLINK_RATE: 16000,
    FRAME_MS: 20,
    /** auto | mic | file —— file 或无麦时自动走回放音源（现场保险 / E2E） */
    UPLINK_MODE: q.get('uplink') || 'auto',
    DEMO_WAV: q.get('wav') || './fixtures/demo_16k.wav',
    PCM_LOOKAHEAD_MS: 120,     // ws_pcm 播放预调度
    FIRST_FRAME_WAIT_MS: 400,  // auto 模式判定窗口
    PING_MS: 10000,
    RECONNECT_BACKOFF: [500, 1000, 2000, 4000, 8000],
    VAD: { START_FRAMES: 3, END_FRAMES: 20, BARGE_FRAMES: 8, FLOOR_MARGIN_DB: 9, REF_MARGIN_DB: 6 },
    ENABLE_MIC: q.get('mic') !== '0'
  };
  window.__AICALL_CONFIG__ = cfg;
})();
