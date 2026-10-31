/**
 * uplink-file.mjs —— FileUplink（无麦回放音源）泵逻辑单测
 *
 * 只测主线程泵：帧切分（320 样本/20ms）、monoMs、energyDb、按时长收尾。
 * 不解码 wav（那是浏览器 decodeAudioData 的活），直接把 pcm 塞进去。
 *
 * 运行：node web/rtc/test/uplink-file.mjs   （约 1.2 秒）
 */
globalThis.window = globalThis.window || {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h)
};

const { FileUplink } = await import('../src/uplink-file.js');

const fails = [];
const check = (name, ok, extra) => {
  console.log(`${ok ? '✅' : '❌'} ${name}${ok || extra === undefined ? '' : ' → ' + JSON.stringify(extra)}`);
  if (!ok) fails.push(name);
};

const SR = 16000;
const FRAME = 320;
const oneSecond = new Int16Array(SR);
for (let i = 0; i < SR; i++) oneSecond[i] = Math.round(Math.sin((2 * Math.PI * 220 * i) / SR) * 0.4 * 32767);

const up = new FileUplink({ ctx: {}, ctxTime: () => 0 }, { UPLINK_RATE: SR, FRAME_MS: 20 });
up.pcm = oneSecond;
up.durationMs = 1000;
up.sourceLabel = 'synthetic';

const frames = [];
let ended = 0;
const t0 = performance.now();
await up.start({
  onFrame: (f) => frames.push(f),
  onEnd: () => ended++
});
await new Promise((r) => setTimeout(r, 1150));

check('1s 音源产出 50 帧（50×20ms）', frames.length === 50, frames.length);
check('每帧固定 320 样本', frames.every((f) => f.pcm.length === FRAME));
check('frameSeq 从 0 连续递增', frames.every((f, i) => f.frameSeq === i), frames.slice(0, 3).map((f) => f.frameSeq));
check('monoMs 单调不减', frames.every((f, i) => i === 0 || f.monoMs >= frames[i - 1].monoMs));
check('monoMs 跨度≈音源时长（±120ms 抖动容忍）',
  Math.abs((frames[frames.length - 1].monoMs - frames[0].monoMs) - 980) < 120,
  Math.round(frames[frames.length - 1].monoMs - frames[0].monoMs));
check('能量为正弦幅值量级（-20±10 dB）',
  frames.slice(2, 10).every((f) => f.energyDb > -30 && f.energyDb < -5),
  frames.slice(2, 10).map((f) => Math.round(f.energyDb)));
check('播完触发 onEnd 且只触发一次', ended === 1, ended);
check('结束后 isRunning=false', up.isRunning() === false);
check('实时性：泵完 1s 音源耗时 ≈1.0s（不 burst）', performance.now() - t0 > 900, Math.round(performance.now() - t0));

// 静音/静音功能
const up2 = new FileUplink({ ctx: {} }, { UPLINK_RATE: SR, FRAME_MS: 20 });
up2.pcm = oneSecond.slice(0, FRAME * 5);
up2.mute(true);
const silent = [];
await up2.start({ onFrame: (f) => silent.push(f) });
await new Promise((r) => setTimeout(r, 200));
check('mute 时输出全 0 样本', silent.length > 0 && silent.every((f) => f.pcm.every((v) => v === 0)), silent.length);
up2.stop();

console.log(fails.length ? `\n❌ ${fails.length} 项失败：\n - ` + fails.join('\n - ') : '\n✅ 全部通过');
process.exit(fails.length ? 1 : 0);
