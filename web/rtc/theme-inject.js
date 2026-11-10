/**
 * theme-inject.js —— 运行时换肤：把 web/style.css 的设计语言移植到 /rtc/ 通话台
 *
 * 依据：web/index.html + web/style.css（暖纸色底 / 24px 圆角卡 / 赤陶 accent /
 *      等宽字体小标签 / 深色事件控制台 / 状态胶囊 + 呼吸点）
 * 约束：不改 DOM、不改 id/class、不进 release 指纹（config.js 不在哈希清单里）
 *
 * 原理：往 <head> 追加一张 <style>，浏览器立刻重算已有节点样式。
 *      所以"页面跑起来之后再注入"与"一开始注入"效果一致，只是后者没有 FOUC。
 *
 * 五条不破坏其他部分的硬约束（本文件逐一遵守）：
 *   1) #aiBadge 的 display 由 ui.js 写 inline，本文件不写任何 #aiBadge 规则。
 *   2) #dbBar 的 width 由 ui.js 写 inline，本文件只改 .meter i 的外观，不写 width。
 *   3) #status 的 color 由 ui.js 写 inline（值取自 var(--ok|--bad|--dim)），
 *      本文件不写 #status 的 color，靠重定义变量联动；.hint 的 color 也不加 !important。
 *   4) #contestCases 靠 hidden 属性显隐，本文件绝不写它的 display，
 *      并额外加 [hidden]{display:none!important} 兜底。
 *   5) #transcript 每次变更会被 ui.js 整段重建，所以气泡不加立面动画
 *      （否则每个 ASR 中间结果都会让全部气泡重播动画）。
 *
 * 挂载方式（本文件是 ES module，没有 IIFE —— 模块自带作用域 + 严格模式）：
 *   A. 在 web/rtc/config.js 末尾追加： import('./theme-inject.js');
 *   B. DevTools Console 里执行：      import('./theme-inject.js')
 *   C. 书签栏：                        javascript:import('./theme-inject.js?t=' + Date.now())
 *   D. 还原：window.__AICALL_THEME__.unmount()
 *   E. 临时关闭：URL 加 ?theme=off
 *
 * 为什么不需要 function 包裹：模块顶层是独立作用域、天然严格模式，不会污染 window，
 * 也不会因为重复加载而重复声明。唯一不能省的是"早退"——模块顶层写 return 是语法
 * 错误，所以这里改成 `const themeDisabled` 判断 + if 块，而不是 return 掉。
 * 反过来说：如果哪天要把本文件内容直接粘进 Console 顶层（不套 import），
 * 顶层 const 会在同一 execution context 里重复声明报 SyntaxError，那时才需要套一层函数。
 */

const VERSION = 'aicall-theme@2-light';
const STYLE_ID = 'aicall-theme-style';

/** ?theme=off 逃生门；URLSearchParams 不存在时按"没关"处理 */
const themeDisabled = (() => {
  try { return new URLSearchParams(location.search).get('theme') === 'off'; }
  catch { return false; }
})();

const CSS = `
/* ===========================================================================
   1. 设计令牌 —— 与 web/style.css :root 同名同值；hex 在前做 oklch 不支持时的回落
   =========================================================================== */
:root{
  --bg:#f3ede2;             --bg:oklch(0.958 0.013 83);
  --surface:#fefcf9;        --surface:oklch(0.995 0.006 83);
  --surface-raised:#fbf7f2; --surface-raised:oklch(0.982 0.009 80);
  --surface-muted:#f3ecdf;  --surface-muted:oklch(0.956 0.014 79);

  --ink:#2b2320;            --ink:oklch(0.235 0.022 52);
  --ink-soft:#4a3f38;       --ink-soft:oklch(0.335 0.024 52);
  --muted:#7d7269;          --muted:oklch(0.525 0.018 60);
  --faint:#a89d93;          --faint:oklch(0.675 0.015 64);

  --line:#e2d9cc;           --line:oklch(0.885 0.018 76);
  --line-strong:#c8b9a8;    --line-strong:oklch(0.80 0.025 70);

  --accent:#c9663a;         --accent:oklch(0.62 0.17 42);
  --accent-strong:#a94a22;  --accent-strong:oklch(0.53 0.175 39);
  --accent-soft:#fbe3d3;    --accent-soft:oklch(0.94 0.045 55);
  --teal:#2f7a80;           --teal:oklch(0.53 0.08 190);
  --green:#3f8c56;          --green:oklch(0.58 0.13 150);
  --red:#c0392b;            --red:oklch(0.55 0.19 28);
  --red-soft:#fbe7e4;       --red-soft:oklch(0.95 0.035 30);

  --shadow-sm:0 1px 2px rgba(64,48,36,.05),0 8px 24px rgba(64,48,36,.04);
  --shadow-md:0 18px 50px rgba(64,48,36,.08);

  --font-sans:"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans SC","Source Han Sans SC",system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --font-mono:"SFMono-Regular","JetBrains Mono","Cascadia Code","Roboto Mono",Consolas,"Liberation Mono",monospace;

  --radius-lg:24px; --radius-md:16px; --radius-sm:12px;

  /* 兼容 /rtc/ 原主题与 ui.js 直接引用的 9 个变量（同样 hex 在前做回落） */
  --panel:#fefcf9; --panel:oklch(0.995 0.006 83);
  --fg:#2b2320;    --fg:oklch(0.235 0.022 52);
  --dim:#7d7269;   --dim:oklch(0.525 0.018 60);
  --ok:#3f8c56;    --ok:oklch(0.58 0.13 150);
  --warn:#c9663a;  --warn:oklch(0.62 0.17 42);
  --bad:#c0392b;   --bad:oklch(0.55 0.19 28);
  --acc:#c9663a;   --acc:oklch(0.62 0.17 42);
}

/* ===========================================================================
   2. 底子：页面背景 + 极淡网格纹理（复刻 web/style.css body + body::before）
   =========================================================================== */
html{
  min-height:100%;
  background:#f3ede2 !important;
  background:oklch(0.958 0.013 83) !important;
}

body{
  position:relative;
  margin:0 !important;
  min-height:100vh;
  color:oklch(0.235 0.022 52) !important;
  font-family:var(--font-sans) !important;
  font-size:14px !important;
  line-height:1.5 !important;
  -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;
  background:
    radial-gradient(ellipse 58% 42% at 6% -12%, rgba(239,187,146,.42), transparent 68%),
    radial-gradient(ellipse 48% 36% at 102% -4%, rgba(186,224,224,.42), transparent 70%),
    linear-gradient(180deg,#f7f2e8 0%,#f3ede2 55%,#efe7d9 100%) !important;
}

/* 网格纹理。main/header/footer 都已抬到 z-index>=1，这层才不会盖住文字 */
body::before{
  position:fixed;
  inset:0;
  z-index:0;
  pointer-events:none;
  content:"";
  background-image:
    linear-gradient(rgba(90,70,50,.045) 1px,transparent 1px),
    linear-gradient(90deg,rgba(90,70,50,.045) 1px,transparent 1px);
  background-size:32px 32px;
  -webkit-mask-image:linear-gradient(to bottom,#000 0%,transparent 78%);
  mask-image:linear-gradient(to bottom,#000 0%,transparent 78%);
}

::selection{
  color:oklch(0.235 0.022 52);
  background:oklch(0.94 0.045 55);
}

:focus-visible,
button:focus-visible,
a:focus-visible,
select:focus-visible,
input:focus-visible{
  outline:2px solid oklch(0.62 0.17 42) !important;
  outline-offset:3px;
}

/* hidden 兜底：任何后加的 ID 规则都压不掉它 */
[hidden]{display:none !important}

/* 细滚动条（复刻 .chat-log / .event-log 的窄竖条） */
*::-webkit-scrollbar{width:8px;height:8px}
*::-webkit-scrollbar-track{background:transparent}
*::-webkit-scrollbar-thumb{
  border-radius:999px;
  background:oklch(0.80 0.025 70);
  border:2px solid transparent;
  background-clip:content-box;
}
#log::-webkit-scrollbar-thumb{
  background:rgba(190,175,155,.32);
  background-clip:content-box;
}

/* ===========================================================================
   3. 顶栏：复刻 .topbar + .connection-pill + .live-tag 的观感
   =========================================================================== */
header{
  position:sticky !important;
  top:0;
  z-index:5;
  gap:10px !important;
  padding:14px clamp(14px,3vw,40px) !important;
  border-bottom:1px solid oklch(0.885 0.018 76 / .9) !important;
  background:rgba(253,250,245,.78) !important;
  -webkit-backdrop-filter:blur(14px);
  backdrop-filter:blur(14px);
  box-shadow:0 1px 0 rgba(255,255,255,.6) inset;
  flex-wrap:wrap;
}

/* #lamp → .connection-dot：小圆点 + 外环 + 状态呼吸 */
.lamp{
  width:9px !important;
  height:9px !important;
  border-radius:50% !important;
  background:oklch(0.675 0.015 64) !important;
  box-shadow:0 0 0 4px rgba(168,157,147,.14) !important;
  transition:background 180ms ease,box-shadow 180ms ease;
}
.lamp.on{
  background:oklch(0.58 0.13 150) !important;
  box-shadow:0 0 0 4px rgba(63,140,86,.16) !important;
  animation:aicallPulseGreen 2.2s ease-out infinite;
}
.lamp.busy{
  background:oklch(0.62 0.17 42) !important;
  box-shadow:0 0 0 4px rgba(201,102,58,.16) !important;
  animation:aicallPulseAccent 1.4s ease-out infinite;
}
.lamp.err{
  background:oklch(0.55 0.19 28) !important;
  box-shadow:0 0 0 4px rgba(192,57,43,.15) !important;
  animation:none;
}

/* #turnState → 等宽大写字（复刻 .live-tag / .state-value 语气） */
header b{
  font-family:var(--font-mono);
  font-size:11.5px !important;
  font-weight:700 !important;
  letter-spacing:.12em;
  color:oklch(0.335 0.024 52) !important;
}

/* .tag → 胶囊标签；长内容（session id / release id）保持可换行、不截断，
   信息完整优先于顶栏宽度；header 本身是 flex-wrap */
.tag{
  padding:5px 10px !important;
  border:1px solid oklch(0.885 0.018 76) !important;
  border-radius:999px !important;
  background:rgba(243,236,223,.72) !important;
  color:oklch(0.525 0.018 60) !important;
  font-family:var(--font-mono) !important;
  font-size:9px !important;
  font-weight:700 !important;
  letter-spacing:.06em !important;
  white-space:nowrap;
  transition:border-color 160ms ease;
}
.tag.acc{
  color:oklch(0.53 0.175 39) !important;
  border-color:rgba(201,102,58,.28) !important;
  background:rgba(251,227,211,.72) !important;
}
a.tag{
  text-decoration:none !important;
  cursor:pointer;
}
a.tag:hover{
  color:oklch(0.53 0.175 39) !important;
  border-color:rgba(201,102,58,.45) !important;
  background:oklch(0.94 0.045 55) !important;
}

/* ===========================================================================
   4. 栅格：复刻 .workspace（1.55fr / 0.85fr）
   =========================================================================== */
main{
  position:relative;
  z-index:1;
  display:grid !important;
  grid-template-columns:minmax(0,1.55fr) minmax(330px,.85fr) !important;
  gap:18px !important;
  align-items:start;
  width:100%;
  max-width:1440px !important;
  margin:0 auto !important;
  padding:clamp(16px,2.4vw,34px) clamp(14px,3vw,40px) 24px !important;
}

/* ===========================================================================
   5. 卡片：复刻 .panel（24px 圆角 + 半透明白 + 模糊 + 双层软阴影）
   =========================================================================== */
section{
  min-width:0;
  padding:clamp(18px,2vw,26px) !important;
  border:1px solid rgba(226,217,204,.9) !important;
  border-radius:var(--radius-lg) !important;
  background:rgba(254,252,249,.86) !important;
  -webkit-backdrop-filter:blur(16px);
  backdrop-filter:blur(16px);
  box-shadow:var(--shadow-sm) !important;
  min-height:0;
}

/* h2 → .section-kicker（英文小标）+ 19px 标题；纯装饰，不动 DOM */
h2{
  display:block !important;
  margin:0 0 14px !important;
  color:oklch(0.235 0.022 52) !important;
  font-size:19px !important;
  font-weight:730 !important;
  letter-spacing:-.02em !important;
  line-height:1.2 !important;
}
h2::before{
  display:block;
  margin-bottom:4px;
  color:oklch(0.53 0.175 39);
  font-family:var(--font-mono);
  font-size:9.5px;
  font-weight:700;
  letter-spacing:.16em;
  line-height:1.4;
}
main > section > h2::before{content:"DIALOGUE"}
main > aside > section:nth-of-type(1) > h2::before{content:"RUN STATE"}
main > aside > section:nth-of-type(2) > h2::before{content:"SLOTS"}
main > aside > section:nth-of-type(3) > h2::before{content:"COMPLIANCE"}
main > aside > section:nth-of-type(4) > h2::before{content:"RESULT"}
main > aside > section:nth-of-type(5) > h2::before{content:"DEBUG"}

/* ===========================================================================
   6. 字幕区：复刻 .chat-log + .message + .bubble
   =========================================================================== */
#transcript{
  display:flex;
  flex-direction:column;
  gap:16px !important;
  height:clamp(320px,52vh,620px) !important;
  padding:18px 8px 14px 2px !important;
  overflow:auto;
  overscroll-behavior:contain;
  border-top:1px dashed oklch(0.885 0.018 76);
  scrollbar-width:thin;
}

/* 空态提示（对应 web/index.html 的 .empty-state）。ui.js 在无字幕时把
   innerHTML 置空，:empty 正好命中；有字幕之后自动消失。纯 CSS，无 DOM 改动 */
#transcript:empty::before{
  content:"点击「开始通话」建立会话，然后说话或用下方输入框回车发送";
  margin:auto;
  color:oklch(0.675 0.015 64);
  font-size:12.5px;
  line-height:1.7;
  text-align:center;
}

.line{
  max-width:min(84%,620px) !important;
  padding:12px 15px !important;
  border-radius:18px !important;
  overflow-wrap:anywhere;
  /* 注意：ui.js 用带缩进的模板字符串拼 innerHTML，两个 div 之间留有换行+空格。
     这些空白文本节点在 white-space:pre-wrap 下会被保留成一整行空白，
     把气泡撑高一倍。所以 .line 用 normal，只让正文那个 div 保留 pre-wrap。 */
  white-space:normal;
  font-size:14.5px !important;
  line-height:1.72 !important;
}
.line > div:last-child{white-space:pre-wrap}

/* 用户：深色墨块 + 右下角小圆角（复刻 .message--user .bubble） */
.line.user{
  align-self:flex-end !important;
  color:oklch(0.98 0.008 80) !important;
  background:linear-gradient(145deg,oklch(0.29 0.027 50),oklch(0.235 0.022 52)) !important;
  border:0 !important;
  border-radius:18px 18px 5px 18px !important;
  box-shadow:0 1px 0 rgba(255,255,255,.08) inset,0 8px 20px rgba(64,48,36,.10) !important;
}

/* 数字员工：白卡 + 左侧 3px 赤陶竖条（复刻 .message--agent .bubble） */
.line.agent{
  align-self:flex-start !important;
  color:oklch(0.235 0.022 52) !important;
  background:oklch(0.982 0.009 80) !important;
  border:1px solid oklch(0.885 0.018 76) !important;
  border-left:3px solid oklch(0.62 0.17 42) !important;
  border-radius:5px 18px 18px !important;
  box-shadow:0 3px 14px rgba(64,48,36,.035) !important;
}

/* .who → .message-head */
.line .who{
  margin:0 2px 7px !important;
  color:oklch(0.675 0.015 64) !important;
  font-family:var(--font-mono) !important;
  font-size:9.5px !important;
  letter-spacing:.04em;
  opacity:1;
}
.line.agent .who{color:oklch(0.53 0.175 39) !important}
.line.user .who{color:rgba(253,250,245,.62) !important}
.line .meta{color:oklch(0.675 0.015 64) !important;font-size:9px}

.struck{
  opacity:.55 !important;
  text-decoration:line-through;
}
.cut{color:oklch(0.55 0.19 28) !important;font-weight:700}
.line.user .cut{color:oklch(0.86 0.11 30) !important}

/* ui.js 生成的"打断等待"提示：inline style，只能 !important 盖。
   它带 .line 但既不是 .user 也不是 .agent，正好可精确命中 */
.line:not(.user):not(.agent){
  align-self:center !important;
  max-width:92% !important;
  padding:7px 14px !important;
  border:1px dashed oklch(0.885 0.018 76) !important;
  border-radius:999px !important;
  color:oklch(0.525 0.018 60) !important;
  background:rgba(251,247,242,.8) !important;
  font-size:11px !important;
  line-height:1.55 !important;
  text-align:center;
}

/* ===========================================================================
   7. 六阶段进度：圆点 + 当前项赤陶光晕
   =========================================================================== */
.stages{margin:0;padding:0;list-style:none}
.stages li{
  display:flex !important;
  align-items:center;
  gap:10px;
  padding:7px 0 !important;
  color:oklch(0.525 0.018 60) !important;
  font-size:12.5px !important;
  line-height:1.5;
  transition:color 180ms ease;
}
.stages li .dot{
  width:18px !important;
  height:18px !important;
  flex:0 0 auto;
  display:grid;
  place-items:center;
  border:1px solid currentColor;
  border-radius:50% !important;
  /* ui.js 往 .dot 里塞了序号（i+1），而 STAGE_LABELS 本身已是「① 身份披露」，
     数字重复。把圆点里的字号归零换成干净的步骤环，信息不丢、观感不重复 */
  font-size:0 !important;
  font-weight:700;
  transition:background 180ms ease,border-color 180ms ease,box-shadow 180ms ease;
}
.stages li.done{color:oklch(0.42 0.1 150) !important}
/* 已完成的环里放对勾，当前阶段放实心点，替代原先重复的数字 */
.stages li.done .dot::after{
  content:"✓";
  font-family:var(--font-sans);
  font-size:10px;
  font-weight:700;
  line-height:1;
}
.stages li.now .dot::after{
  content:"";
  width:6px;
  height:6px;
  border-radius:50%;
  background:currentColor;
}
.stages li.done .dot{
  border-color:rgba(63,140,86,.45) !important;
  background:rgba(63,140,86,.10);
}
.stages li.now{
  color:oklch(0.53 0.175 39) !important;
  font-weight:700 !important;
}
.stages li.now .dot{
  border-color:oklch(0.62 0.17 42) !important;
  background:oklch(0.94 0.045 55);
  box-shadow:0 0 0 3px rgba(201,102,58,.14);
}

/* ===========================================================================
   8. 键值行 / 槽位：复刻 .slot-item 与 .turn-row 的等宽小键 + 虚线分隔
   =========================================================================== */
.kv{
  display:flex !important;
  justify-content:space-between;
  gap:12px;
  padding:8px 0 !important;
  border-bottom:1px dashed oklch(0.885 0.018 76) !important;
  color:oklch(0.525 0.018 60) !important;
  font-size:12px !important;
  align-items:baseline;
}
.kv:last-child{border-bottom:0 !important}
.kv span{
  color:oklch(0.525 0.018 60);
  font-family:var(--font-mono);
  font-size:9.5px;
  letter-spacing:.06em;
  min-width:0;
  overflow-wrap:anywhere;
}
.kv b{
  color:oklch(0.335 0.024 52) !important;
  font-family:var(--font-mono);
  font-size:11.5px;
  font-weight:700 !important;
  text-align:right;
  overflow-wrap:anywhere;
  font-variant-numeric:tabular-nums;
}

/* ===========================================================================
   9. 电平条
   =========================================================================== */
.meter{
  height:8px !important;
  margin-top:8px;
  border-radius:999px !important;
  overflow:hidden;
  background:oklch(0.956 0.014 79) !important;
  box-shadow:0 1px 2px rgba(64,48,36,.06) inset;
}
/* 只改外观：宽度由 ui.js 写 inline，这里绝不声明 width。
   顺带去掉原主题的 transition:width .06s —— 电平由 VAD 帧驱动、约每 20ms 更新一次，
   60ms 的过渡永远追不上输入，只会让电平条看起来滞后。 */
.meter i{
  display:block;
  height:100%;
  border-radius:999px;
  background:linear-gradient(90deg,oklch(0.58 0.13 150),oklch(0.62 0.17 42)) !important;
  transition:none;
}

/* ===========================================================================
   10. 事件日志：复刻 .event-log 的深色控制台（纸色页面上唯一的暗块）
   =========================================================================== */
details{margin-top:10px}
details summary{
  cursor:pointer;
  user-select:none;
  color:oklch(0.53 0.175 39) !important;
  font-family:var(--font-mono);
  font-size:10px;
  font-weight:700;
  letter-spacing:.06em;
  list-style:none;
}
details summary::-webkit-details-marker{display:none}
details summary:hover{text-decoration:underline;text-underline-offset:3px}
details[open] summary{margin-bottom:8px}

#log{
  height:clamp(140px,18vh,220px) !important;
  padding:10px !important;
  border:1px solid rgba(90,70,50,.18) !important;
  border-radius:15px !important;
  color:rgba(214,200,184,.86) !important;
  background:linear-gradient(180deg,oklch(0.255 0.02 52),oklch(0.205 0.016 52)) !important;
  box-shadow:0 1px 0 rgba(255,255,255,.06) inset,0 0 24px rgba(20,10,5,.22) inset !important;
  font-family:var(--font-mono) !important;
  font-size:10px !important;
  line-height:1.6 !important;
  white-space:pre-wrap;
  overflow:auto;
  scrollbar-width:thin;
}

/* ===========================================================================
   11. 底部操作区：复刻 .btn 系列
   =========================================================================== */
footer{
  position:sticky !important;
  bottom:0;
  z-index:5;
  gap:9px !important;
  padding:14px clamp(14px,3vw,40px) !important;
  border-top:1px solid oklch(0.885 0.018 76 / .9) !important;
  background:rgba(253,250,245,.82) !important;
  -webkit-backdrop-filter:blur(14px);
  backdrop-filter:blur(14px);
  box-shadow:0 -1px 0 rgba(255,255,255,.6) inset;
  flex-wrap:wrap;
  align-items:center;
}
footer label{
  color:oklch(0.525 0.018 60) !important;
  font-family:var(--font-mono);
  font-size:9.5px;
  font-weight:700;
  letter-spacing:.06em;
}

button{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  gap:8px;
  min-height:42px !important;
  padding:0 16px !important;
  border:1px solid oklch(0.885 0.018 76) !important;
  border-radius:var(--radius-sm) !important;
  color:oklch(0.335 0.024 52) !important;
  background:oklch(0.982 0.009 80) !important;
  font-family:var(--font-sans);
  font-size:13px !important;
  font-weight:680 !important;
  letter-spacing:.01em;
  cursor:pointer;
  box-shadow:0 1px 2px rgba(64,48,36,.04);
  /* 只过渡描边/阴影/位移：不过渡 background 与 color，
     否则主题在首帧注入时会看到"深色 → 浅色"的闪变 */
  transition:border-color 160ms ease,transform 120ms ease,box-shadow 160ms ease,opacity 160ms ease;
}
button:hover:not(:disabled){
  border-color:oklch(0.80 0.025 70) !important;
  background:oklch(0.956 0.014 79) !important;
  transform:translateY(-1px);
  box-shadow:0 6px 16px rgba(64,48,36,.08);
}
button:active:not(:disabled){
  transform:translateY(1px) scale(.99);
  box-shadow:0 1px 2px rgba(64,48,36,.04);
}
button:disabled{
  cursor:not-allowed;
  opacity:.46 !important;
  box-shadow:none !important;
  transform:none !important;
}

/* 开始通话 → .btn-primary */
button.primary{
  border-color:transparent !important;
  color:oklch(0.99 0.006 80) !important;
  background:linear-gradient(180deg,oklch(0.56 0.175 40),oklch(0.53 0.175 39)) !important;
  box-shadow:0 1px 0 rgba(255,255,255,.18) inset,0 8px 18px rgba(169,74,34,.20) !important;
}
button.primary:hover:not(:disabled){
  background:linear-gradient(180deg,oklch(0.59 0.18 41),oklch(0.49 0.17 38)) !important;
  box-shadow:0 1px 0 rgba(255,255,255,.2) inset,0 10px 24px rgba(169,74,34,.26) !important;
}

/* 挂断 → .btn-barge 的红色描边款 */
button.danger{
  color:oklch(0.55 0.19 28) !important;
  border-color:rgba(192,57,43,.22) !important;
  background:rgba(250,240,237,.7) !important;
}
button.danger:hover:not(:disabled){
  border-color:rgba(192,57,43,.4) !important;
  background:oklch(0.95 0.035 30) !important;
}

/* 发送 → .btn-send 的墨色款 */
#btnSend{
  border-color:transparent !important;
  color:oklch(0.98 0.008 80) !important;
  background:oklch(0.235 0.022 52) !important;
  box-shadow:0 6px 16px rgba(64,48,36,.14) !important;
}
#btnSend:hover:not(:disabled){
  background:oklch(0.19 0.02 52) !important;
}

/* ===========================================================================
   12. 表单控件：复刻 .message-input 与下拉框
   =========================================================================== */
select,#inputSource,#contestCase{
  min-height:38px;
  padding:0 12px !important;
  border:1px solid oklch(0.885 0.018 76) !important;
  border-radius:10px !important;
  color:oklch(0.235 0.022 52) !important;
  background:oklch(0.956 0.014 79) !important;
  font-family:var(--font-sans) !important;
  font-size:12.5px !important;
  transition:border-color 160ms ease,box-shadow 160ms ease;
}
select:hover:not(:disabled){
  border-color:oklch(0.80 0.025 70) !important;
}
select:disabled{opacity:.55}

/* 注意：只调外观，绝不写 display —— 它靠 hidden 属性显隐 */
#contestCases{
  align-items:center;
  gap:8px;
  color:oklch(0.525 0.018 60);
  font-family:var(--font-mono);
  font-size:9.5px;
  font-weight:700;
  letter-spacing:.06em;
}

#txtInput{
  flex:1 1 260px;
  min-width:200px;
  height:46px;
  padding:0 16px !important;
  border:1px solid oklch(0.885 0.018 76) !important;
  border-radius:14px !important;
  color:oklch(0.235 0.022 52) !important;
  background:oklch(0.956 0.014 79) !important;
  font-family:var(--font-sans) !important;
  font-size:14px !important;
  transition:border-color 160ms ease,box-shadow 160ms ease;
}
#txtInput::placeholder{color:oklch(0.675 0.015 64)}
#txtInput:hover:not(:disabled){border-color:oklch(0.80 0.025 70) !important}
#txtInput:focus{
  border-color:oklch(0.62 0.17 42) !important;
  outline:none !important;
  background:oklch(0.995 0.006 83) !important;
  box-shadow:0 0 0 3px rgba(201,102,58,.11) !important;
}
#txtInput:disabled{cursor:not-allowed;opacity:.6}

#audioFile{
  max-width:190px;
  color:oklch(0.675 0.015 64);
  font-family:var(--font-mono);
  font-size:9.5px;
}
#audioFile::file-selector-button{
  margin-right:8px;
  padding:5px 10px;
  border:1px solid oklch(0.885 0.018 76);
  border-radius:8px;
  color:oklch(0.335 0.024 52);
  background:oklch(0.982 0.009 80);
  font-family:var(--font-sans);
  font-size:10.5px;
  font-weight:680;
  cursor:pointer;
  transition:background 160ms ease,border-color 160ms ease;
}
#audioFile::file-selector-button:hover{
  border-color:oklch(0.62 0.17 42);
  background:oklch(0.94 0.045 55);
}

/* ===========================================================================
   13. 提示文字 / 药丸 / 杂项
   =========================================================================== */
/* 注意：color 不加 !important —— #status 的颜色由 ui.js 用 var(--ok|--bad|--dim) 写 inline */
.hint{
  font-size:11px !important;
  color:oklch(0.525 0.018 60);
  letter-spacing:.01em;
}
.pill{
  padding:2px 8px !important;
  border:1px solid oklch(0.885 0.018 76) !important;
  border-radius:999px !important;
  color:oklch(0.675 0.015 64) !important;
  background:oklch(0.956 0.014 79);
  font-family:var(--font-mono);
  font-size:9px !important;
  font-weight:700;
  letter-spacing:.04em;
}
.pill.warn{
  color:oklch(0.45 0.15 38) !important;
  border-color:rgba(201,102,58,.24) !important;
  background:rgba(251,227,211,.85);
}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}

/* ===========================================================================
   14. 动效
   =========================================================================== */
@keyframes aicallPulseGreen{
  0%{box-shadow:0 0 0 3px rgba(63,140,86,.18)}
  70%,100%{box-shadow:0 0 0 9px rgba(63,140,86,0)}
}
@keyframes aicallPulseAccent{
  0%{box-shadow:0 0 0 3px rgba(201,102,58,.18)}
  70%,100%{box-shadow:0 0 0 9px rgba(201,102,58,0)}
}

/* ===========================================================================
   15. 响应式：与 web/style.css 的 1120 / 960 / 760 / 560 断点对齐
   =========================================================================== */
@media (max-width:1120px){
  main{grid-template-columns:minmax(0,1.42fr) minmax(312px,.9fr) !important}
}
@media (max-width:960px){
  main{grid-template-columns:minmax(0,1fr) !important}
  #transcript{height:clamp(300px,48vh,500px) !important}
  main > aside{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px}
}
@media (max-width:760px){
  section{border-radius:20px !important}
  .line{max-width:90% !important}
  .tag{font-size:8.5px !important;padding:4px 8px !important}
}
@media (max-width:560px){
  main{padding:14px 12px 20px !important}
  header,footer{padding-left:12px !important;padding-right:12px !important}
  section{padding:17px 15px !important}
  h2{font-size:17px !important}
  #txtInput{font-size:16px !important;height:48px}
  button.primary,button.danger{flex:1 1 0}
  main > aside{grid-template-columns:minmax(0,1fr)}
}

/* ===========================================================================
   16. 动效降级
   =========================================================================== */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{
    animation-duration:.01ms !important;
    animation-iteration-count:1 !important;
    transition-duration:.01ms !important;
    scroll-behavior:auto !important;
  }
}
`;

if (!themeDisabled) {
  const existing = document.getElementById(STYLE_ID);
  const el = existing || document.createElement('style');
  el.id = STYLE_ID;
  // 已存在则覆盖内容：改了样式后重新 import 能立刻生效，不用刷新页面
  el.textContent = CSS;
  if (!existing) (document.head || document.documentElement).appendChild(el);

  window.__AICALL_THEME__ = {
    version: VERSION,
    injected: true,
    /** 还原成仓库原始（深色）样式：验收对比前调用 */
    unmount() {
      const node = document.getElementById(STYLE_ID);
      if (node) node.remove();
      this.injected = false;
    },
  };

  console.debug('[theme]', VERSION, 'injected');
}
