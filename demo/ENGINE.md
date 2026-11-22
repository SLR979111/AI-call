# 文本动画排版引擎

`TextLayoutEngine` 是公开入口，不依赖演示脚本、客服业务、网络或音频服务。

正式页面与演示共用 `web/` 内的 `text-engine.css`、`text-motion.js`、`text-engine.js`，加载后：

```js
const engine = new TextLayoutEngine(contentElement, scrollElement);
const reply = engine.begin('reply-1', 'assistant');
engine.append(reply, '您好，');
engine.append(reply, '请继续说。');
engine.revise(reply, '您好，请慢慢说。');
engine.finish(reply);

engine.setCandidates([{id: 'ready-1', text: '我帮您核对一下。'}]);
const next = engine.promote('ready-1', 'reply-2');
engine.append(next, '请打开第一份材料。');
engine.cancel(next); // 后续迟到的 append/revise 被忽略

engine.setInput({active: true, level: 0.7}); // level 范围 0–1
engine.setInput({active: false}); // 常驻的弱波形
engine.pause(true);
engine.reset(); // 旧 handle 全部失效
engine.destroy();
```

正式通话使用 `{interactiveCandidates: false}`，候选作为静态预览，由服务端选中后通过 `promote` 升格。`web/conversation.js` 将真实识别、回复、候选和取消事件接到上述接口；没有在浏览器里安排假打字。预览入口是 `/demo/`，测试片段通过 `?scene=stream|revision|prediction|interrupt` 选择，默认完整演示；页面只显示底部图标按钮。

底部书本图标打开电话例子库；`/demo/?library=1` 可直接打开。六通虚构的 JD 客服脚本保存在 `demo/call-examples.js`，包括补交说明、已提交后的等待、主体与授权澄清、忙碌结束、连续道别和较长的多轮电话。选择后从头载入，地址保存当前 `scene`，支持暂停、重播和 1/2/4 倍速；打开库时暂停，关闭后恢复此前播放状态。脚本只做无声回放，不调用麦克风、模型或音频服务。回放结束保留历史，可回看或载入另一通。“进入互动通话”链接会打开主页面并选中对应的京东工单，随后可接听或文字试聊。对应检查为 `node scripts/check-call-examples.mjs`。

- **入口状态**：`begin / append / revise / finish / cancel`。同一帧多次输入合并成一次布局更新，文字立即更新，不在引擎中排队假打字。
- **排版**：一次 `begin` 对应一个连续段落。增量和标点不会拆出新段；到达右边界后在同一段内自然续到下一行，不横向滚动，显式换行显示为空格。每个角色的最新消息为大字，旧消息缩小。当前用户纯白、客服银色；历史保留暗青蓝和灰色。
- **动效**：ASR、客服、候选字和圆点共用 `enter` 入口，从右侧小幅偏移并带模糊滑入，每字间隔22毫秒，连续输入不重播旧字。候选圆点是实际动画节点，不是留在原位的伪元素。命中时逐字放大并沿用同样的位移、模糊与透明度演化。缩放只校正布局，不把入场透明度跳到终点；隐藏/卡顿后限制单帧步长，避免一帧播完整个动画。同一行的文字追加保留横向惯性，段落整体使用带阻尼的质量弹簧让位和回稳。测量前成批归零字形偏移，测量后恢复，避免将动画位移累积进布局目标。追加保留已有节点，没有残影副本。
- **生命周期**：取消和重置令旧 handle 失效；暂停、窗口缩放、页面隐藏与销毁由引擎管理。减少动态效果的系统偏好会被尊重。
- **输入指示**：波形节点常驻，三层渐变曲线由输入 level 驱动，约65毫秒抬升、280毫秒回落，并响应音量上升沿。闲置时减慢、缩短、减弱。引擎不打开麦克风。

`text-motion.js` 负责保留节点、段落与字形弹簧、逐帧批处理和模糊滑入；`text-engine.js` 负责消息状态、布局、候选与输入指示；`text-engine.css` 保存排版规则。`demo.js` 只发模拟事件并控制回放。

独立检查：`node scripts/check-text-engine.mjs`。惯性轨迹与录屏：`node scripts/check-inertial-text.mjs`。演示验收：`node scripts/check-typography-demo.mjs`。
