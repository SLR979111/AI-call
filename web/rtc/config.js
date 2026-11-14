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
  var frameMs = 20;
  var requestedBargeMs = Number(q.get('bargeMs'));
  var bargeMs = requestedBargeMs >= 300 && requestedBargeMs <= 1200
    ? requestedBargeMs
    : 600;
  var bargeFrames = Math.ceil(bargeMs / frameMs);
  var cfg = {
    /** 后端基地址：默认同源；不同源时用 ?app= 覆盖 */
    APP_BASE: appFromQuery || location.origin,
    /** 便于自检：到底是同源还是覆盖来的（调试面板可显示） */
    APP_BASE_SOURCE: appFromQuery ? 'query' : 'same-origin',
    SCENARIO_ID: q.get('scenario') || 'service_outbound',
    RELEASE_ID: q.get('release') || null,

    /** 前端逻辑版本：
     *  stream —— 新逻辑（Decision 流：push 上行 / onAudio 下行 hook）
     *  legacy —— 旧契约（ws_pcm 那套 session/turn 状态机）
     *  auto   —— 先探测 /ws/decision 是否可用，可用走 stream，否则自动回落 legacy
     *  ?mode=stream|legacy|auto 可强制覆盖 */
    MODE: q.get('mode') || 'auto',

    /** Decision 流：上行输入与下行音频共用一条 WS（这就是"两个接口"的传输层） */
    STREAM: {
      WS_PATH: q.get('streamPath') || '/ws/decision',
      PROBE_TIMEOUT_MS: 800,
      HEARTBEAT_MS: 10000,
      RECONNECT_BACKOFF: [300, 800, 2000, 4000]
    },

    /** 传输偏好：auto | livekit | webrtc（=livekit）| ws_pcm | rtc_pcm
     *  auto   —— 先要 LiveKit token；拿到就入房。MEDIA_NOT_CONFIGURED / 入房失败 /
     *            超时一律静默回落 ws_pcm（C1 + G4），不影响通话可用性。
     *  ws_pcm —— 不尝试 LiveKit（无 LiveKit Server 时的确定性路径）。
     *  rtc_pcm—— 竞赛路径的 WebRTC DataChannel，与 LiveKit 无关。 */
    TRANSPORT_PREF: q.get('transport') || 'auto',

    /** LiveKit 客户端 SDK：懒加载，ws_pcm 用户不会下载这 1.4MB。
     *  ?lkSdk= 可覆盖（测试 / 灰度）；默认用 web/rtc/vendor/ 里的随仓库副本。 */
    LIVEKIT_SDK_URL: q.get('lkSdk') || null,
    LIVEKIT_CONNECT_TIMEOUT_MS: 8000,
    /** 采集约束必须与 audio-engine 的 getUserMedia 一致，
     *  否则「VAD 判定那条轨」和「发给服务端那条轨」就不是同一份，AEC 也白给 */
    LIVEKIT_AUDIO: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },

    /** 下行播放形态：auto | speech | pcm
     *  auto：tts_started 后 400ms 内收到 audio_chunk 用 pcm，否则用 speechSynthesis
     *  speech：W1 Fake TTS 形态（服务端只发 tts_* 元数据，音频由浏览器合成）
     *  pcm：W2 真实 TTS 形态（服务端下行 24k PCM 块） */
    DOWNLINK: q.get('downlink') || 'auto',

    UPLINK_RATE: 16000,
    FRAME_MS: frameMs,
    /** auto | mic | file —— file 或无麦时自动走回放音源（现场保险 / E2E） */
    UPLINK_MODE: q.get('uplink') || 'auto',
    DEMO_WAV: q.get('wav') || './fixtures/demo_16k.wav',
    PCM_LOOKAHEAD_MS: 120,     // ws_pcm 播放预调度
    FIRST_FRAME_WAIT_MS: 400,  // auto 模式判定窗口
    PING_MS: 10000,
    RECONNECT_BACKOFF: [500, 1000, 2000, 4000, 8000],
    VAD: { START_FRAMES: 3, END_FRAMES: 40, BARGE_FRAMES: bargeFrames, BARGE_MS: bargeMs, FLOOR_MARGIN_DB: 9, REF_MARGIN_DB: 6 },
    ENABLE_MIC: q.get('mic') !== '0'
  };
  window.__AICALL_CONFIG__ = cfg;
})();

// 运行时换肤：注入 ./theme-inject.js（不改 DOM、不进 release 指纹；?theme=off 可关闭）
import('./theme-inject.js');
