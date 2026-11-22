export async function readPhone(input, validate = false, mode = 'cloud') {
  const response = await fetch('/api/phone-file' + (validate ? '/validate' : '?mode=' + encodeURIComponent(mode)), {
    method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(validate ? '电话文件格式或任务图无效。' : '无法生成任务图，请检查工单。');
  return await response.json();
}

const graphViews = new WeakMap();
const svgNS = 'http://www.w3.org/2000/svg';
const icons = '/static/icons.svg#';
const statusLabels = {current: '当前', completed: '已完成', skipped: '已打断'};
const shortGoals = [
  [/^(?:确认(?:目前)?|说清)?手边有哪些材料.*进度/u, '核对材料与进度'],
  [/^重传一张.*商标.*截图/u, '重传商标截图'],
  [/^已(?:经)?提交后(?:只需)?等待反馈/u, '等待反馈'],
  [/^要求停止时.*收尾/u, '按要求结束通话'],
  [/^(?:只有注册证.*)?(?:还需|还要|需)单独准备(?:当前)?状态截图/u, '准备状态截图'],
  [/^已(?:经)?上传.*草稿.*提交/u, '提交申请草稿'],
  [/^把剩余疑问问清/u, '解答剩余疑问'],
  [/^(?:本次)?补正要求并结束通话/u, '确认要求并结束'],
];

function icon(name) {
  const svg = document.createElementNS(svgNS, 'svg');
  const use = document.createElementNS(svgNS, 'use');
  svg.setAttribute('aria-hidden', 'true');
  use.setAttribute('href', icons + name);
  svg.append(use);
  return svg;
}

function nodeLabel(node) {
  if (node.kind === 'greeting') return '开场问候';
  const goal = node.goal.trim();
  const label = goal.replace(/^(?:解决对方单独关心的问题[：:]|(?:让|请|帮)?(?:对方|用户|商家)(?:清楚地|能)?(?:确认已理解|明白|理解|弄清|确认|知道)?)/u, '')
    .replace(/^[\s，：:]+|[。！？]$/gu, '')
    .replace(/^(?:本次|这次)(?:只需|需要)/u, '') || goal;
  return shortGoals.find(([pattern]) => pattern.test(label))?.[1] || label;
}

export function showGraph(container, select, file, progress, onEdit = null) {
  const old = graphViews.get(container);
  if (old?.file === file && old.onEdit === onEdit && container.contains(old.path)) {
    old.updateProgress(progress);
    return;
  }
  old?.observer.disconnect();
  const {nodes} = file.graph;
  const byId = new Map(nodes.map(n => [n.id, n]));
  const parents = new Set(nodes.flatMap(n => n.requires));
  const leaves = nodes.filter(n => !parents.has(n.id));
  const pathId = old?.onEdit && onEdit ? select.value : '';
  select.replaceChildren(new Option('全部分支', ''), ...leaves.map(n => new Option(n.goal, n.id)));
  if (leaves.some(n => n.id === pathId)) select.value = pathId;

  const canvas = document.createElement('div');
  canvas.className = 'graph-canvas';
  canvas.tabIndex = 0;
  canvas.setAttribute('role', 'region');
  canvas.setAttribute('aria-label', '工单流程图');
  const path = document.createElement('div'); path.className = 'phone-path';
  const edges = document.createElementNS(svgNS, 'svg');
  edges.classList.add('phone-edges'); edges.setAttribute('aria-hidden', 'true');
  canvas.append(path);

  const detail = document.createElement('section');
  detail.id = 'phoneNodeDetail';
  detail.className = 'node-detail';
  detail.hidden = true;
  const heading = document.createElement('h2');
  heading.id = 'phoneNodeHeading';
  heading.tabIndex = -1;
  detail.setAttribute('aria-labelledby', heading.id);
  const header = document.createElement('header');
  const tools = document.createElement('div');
  tools.className = 'order-tools';
  const edit = document.createElement('button');
  edit.type = 'button'; edit.className = 'icon-control node-edit';
  edit.append(icon('edit'));
  const close = document.createElement('button');
  close.type = 'button'; close.className = 'icon-control';
  close.title = '关闭节点详情'; close.setAttribute('aria-label', close.title);
  close.append(icon('close'));
  tools.append(edit, close);
  header.append(heading, tools);
  const fields = document.createElement('dl');
  detail.append(header, fields);

  const boxes = new Map();
  let selectedNode = null;
  let latestProgress = progress;

  function hideDetail(restoreFocus = false) {
    const button = boxes.get(selectedNode);
    button?.setAttribute('aria-expanded', 'false');
    detail.hidden = true;
    selectedNode = null;
    if (restoreFocus) button?.focus();
  }
  close.onclick = () => hideDetail(true);
  edit.onclick = () => onEdit?.(selectedNode);

  function showDetail(node) {
    if (selectedNode === node.id) { hideDetail(); return; }
    hideDetail();
    selectedNode = node.id;
    boxes.get(node.id).setAttribute('aria-expanded', 'true');
    heading.textContent = node.goal;
    edit.hidden = !onEdit || node.kind === 'root';
    edit.title = node.kind === 'greeting' ? '编辑开场白' : '编辑节点';
    edit.setAttribute('aria-label', '编辑节点 ' + node.goal);
    const sections = [
      ['应答动作', node.action],
      ['完成依据', node.done_when],
      ['前置节点', node.requires.map(id => byId.get(id).goal).join('、')],
    ];
    fields.replaceChildren();
    for (const [label, value] of sections) {
      if (!value) continue;
      const dt = document.createElement('dt'), dd = document.createElement('dd');
      dt.textContent = label; dd.textContent = value;
      fields.append(dt, dd);
    }
    detail.hidden = false;
    heading.focus({preventScroll: true});
    detail.scrollIntoView({block: 'nearest'});
  }

  function drawEdges() {
    const bounds = path.getBoundingClientRect();
    if (!bounds.width) return;
    const lines = [];
    for (const [id, box] of boxes) {
      const target = box.getBoundingClientRect();
      for (const parent of byId.get(id).requires) {
        const sourceBox = boxes.get(parent);
        const source = sourceBox.getBoundingClientRect();
        const x = source.left + source.width / 2 - bounds.left, y = source.bottom - bounds.top;
        const endX = target.left + target.width / 2 - bounds.left, endY = target.top - bounds.top;
        const mid = (y + endY) / 2;
        const line = document.createElementNS(svgNS, 'path');
        line.setAttribute('d', `M ${x} ${y} C ${x} ${mid} ${endX} ${mid} ${endX} ${endY - 1} m -3 -4 l 3 4 l 3 -4`);
        if (sourceBox.dataset.status === 'completed' && ['completed', 'current'].includes(box.dataset.status)) {
          line.classList.add('traversed');
        }
        lines.push(line);
      }
    }
    edges.replaceChildren(...lines);
  }
  const observer = new ResizeObserver(drawEdges);

  function updateProgress(state) {
    latestProgress = state;
    const completed = new Set(state?.completed), skipped = new Set(state?.skipped);
    for (const [id, button] of boxes) {
      const status = id === state?.current ? 'current' : completed.has(id) ? 'completed' : skipped.has(id) ? 'skipped' : '';
      if (status === button.dataset.status) continue;
      const node = byId.get(id);
      const name = statusLabels[status] ? `${statusLabels[status]} · ${node.goal}` : node.goal;
      button.dataset.status = status;
      button.title = name;
      button.setAttribute('aria-label', name);
      if (status === 'current') button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
      const glyph = status === 'completed' ? 'check' : status === 'skipped' ? 'skip' : button.dataset.icon;
      button.querySelector('use').setAttribute('href', icons + glyph);
    }
    drawEdges();
  }

  function draw() {
    const included = new Set();
    function visit(id) {
      if (included.has(id)) return;
      included.add(id); byId.get(id).requires.forEach(visit);
    }
    if (select.value) visit(select.value); else nodes.forEach(n => visit(n.id));
    observer.disconnect();
    hideDetail();
    boxes.clear();
    const rows = [], shown = new Set();
    let columns = 1;
    while (shown.size < included.size) {
      const ready = nodes.filter(n => included.has(n.id) && !shown.has(n.id) && n.requires.every(p => shown.has(p)));
      if (!ready.length) break;
      columns = Math.max(columns,ready.length);
      const rank = document.createElement("div"); rank.className = "phone-rank";
      rows.push(rank);
      for (const n of ready) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'phone-node';
        button.dataset.node = n.id;
        button.dataset.kind = n.kind || 'business';
        button.dataset.icon = n.kind === 'root' ? 'phone' : n.kind === 'greeting' ? 'reply' : parents.has(n.id) ? 'step' : 'flag';
        button.setAttribute('aria-expanded', 'false');
        button.setAttribute('aria-controls', detail.id);
        const badge = document.createElement('span'); badge.className = 'phone-node-icon';
        badge.append(icon(button.dataset.icon));
        const label = document.createElement('span'); label.className = 'phone-node-label';
        label.textContent = nodeLabel(n);
        button.append(badge, label);
        button.onclick = () => showDetail(n);
        rank.append(button); shown.add(n.id); boxes.set(n.id, button);
      }
    }
    path.style.setProperty("--columns",columns);
    path.style.setProperty("--gaps", columns - 1);
    path.replaceChildren(edges, ...rows);
    observer.observe(path);
    updateProgress(latestProgress);
    if (canvas.scrollWidth > canvas.clientWidth) {
      const anchor = boxes.get(latestProgress?.current) || boxes.values().next().value;
      const bounds = anchor.getBoundingClientRect();
      canvas.scrollLeft += bounds.left + bounds.width / 2 - canvas.getBoundingClientRect().left - canvas.clientWidth / 2;
    }
  }
  graphViews.set(container, {file, onEdit, path, observer, updateProgress});
  select.onchange = draw;
  container.replaceChildren(canvas, detail);
  draw();
}

export function clearGraph(container, select) {
  graphViews.get(container)?.observer.disconnect();
  graphViews.delete(container);
  container.replaceChildren();
  select.onchange = null;
  select.replaceChildren();
}

export function downloadPhone(file) {
  const blob = new Blob([JSON.stringify(file, null, 2)], {type: 'application/json'});
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob);
  link.download = file.title + '.phone.json'; link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
