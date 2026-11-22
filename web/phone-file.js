export async function readPhone(input, validate = false, mode = 'cloud') {
  const response = await fetch('/api/phone-file' + (validate ? '/validate' : '?mode=' + encodeURIComponent(mode)), {
    method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(validate ? '电话文件格式或任务图无效。' : '无法生成任务图，请检查工单。');
  return await response.json();
}

const graphViews = new WeakMap();

export function showGraph(container, file, progress, onEdit = null) {
  const old = graphViews.get(container);
  const key = JSON.stringify([progress?.leaf, progress?.current, progress?.completed, !!onEdit]);
  if (old?.file === file && old.key === key && container.contains(old.path)) return;
  old?.observer.disconnect();
  const {nodes} = file.graph;
  const byId = new Map(nodes.map(n => [n.id, n]));
  const parents = new Set(nodes.flatMap(n => n.requires));
  const leaves = nodes.filter(n => !parents.has(n.id));
  const label = document.createElement('label'), select = document.createElement('select');
  label.className = 'path-filter'; label.textContent = '查看路径';
  select.add(new Option('全部分支', ''));
  for (const leaf of leaves) select.add(new Option(leaf.goal, leaf.id));
  if (old?.file === file) select.value = old.select.value;
  label.append(select);
  const path = document.createElement('div'); path.className = 'phone-path';
  const svgNS = 'http://www.w3.org/2000/svg';
  const edges = document.createElementNS(svgNS, 'svg');
  edges.classList.add('phone-edges'); edges.setAttribute('aria-hidden', 'true');
  const boxes = new Map();
  function drawEdges() {
    const bounds = path.getBoundingClientRect();
    if (!bounds.width) return;
    const lines = [];
    for (const [id, box] of boxes) {
      const target = box.getBoundingClientRect();
      for (const parent of byId.get(id).requires) {
        const source = boxes.get(parent).getBoundingClientRect();
        const x = source.left + source.width / 2 - bounds.left, y = source.bottom - bounds.top;
        const endX = target.left + target.width / 2 - bounds.left, endY = target.top - bounds.top;
        const mid = (y + endY) / 2;
        const line = document.createElementNS(svgNS, 'path');
        line.setAttribute('d', 'M '+x+' '+y+' C '+x+' '+mid+' '+endX+' '+mid+' '+endX+' '+endY+
          ' m -3 -4 l 3 4 l 3 -4');
        lines.push(line);
      }
    }
    edges.replaceChildren(...lines);
  }
  const observer = new ResizeObserver(drawEdges);
  graphViews.set(container, {file, key, path, observer, select});
  function draw() {
    const included = new Set();
    function visit(id) {
      if (included.has(id)) return;
      included.add(id); byId.get(id).requires.forEach(visit);
    }
    if (select.value) visit(select.value); else nodes.forEach(n => visit(n.id));
    observer.disconnect(); boxes.clear();
    const rows = [], shown = new Set();
    let columns = 1;
    while (shown.size < included.size) {
      const ready = nodes.filter(n => included.has(n.id) && !shown.has(n.id) && n.requires.every(p => shown.has(p)));
      if (!ready.length) break;
      columns = Math.max(columns,ready.length);
      const rank = document.createElement("div"); rank.className = "phone-rank";
      rows.push(rank);
      for (const n of ready) {
        const li = document.createElement('div'), detail = document.createElement('details');
        const summary = document.createElement('summary');
        li.dataset.node = n.id;
        const status = progress?.completed?.includes(n.id) ? '✓ ' : n.id === progress?.current ? '当前 · ' : '';
        if (n.id === progress?.current) li.setAttribute('aria-current', 'step');
        const title = document.createElement('span');
        title.textContent = status + n.goal; summary.append(title); detail.append(summary);
        const criterion = document.createElement('p'); criterion.textContent = '完成依据 · ' + n.done_when;
        if (n.action) {
          const action = document.createElement('p'); action.textContent = n.action; detail.append(action);
        }
        detail.append(criterion);
        if (n.requires.length) {
          const deps = document.createElement('p'); deps.textContent = '依赖 · ' + n.requires.map(p => byId.get(p).goal).join('、');
          detail.append(deps);
        }
        li.append(detail);
        if (onEdit) {
          const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'node-edit';
          edit.textContent = '编辑'; edit.setAttribute('aria-label', '编辑节点 ' + n.goal);
          edit.onclick = () => onEdit(n.id); li.append(edit);
        }
        rank.append(li); shown.add(n.id); boxes.set(n.id, li);
      }
    }
    path.style.setProperty("--columns",columns);
    path.style.setProperty("--rank-gaps",(columns - 1) * 12 + "px");
    path.replaceChildren(edges, ...rows);
    observer.observe(path);
    for (const box of boxes.values()) observer.observe(box);
  }
  const canvas = document.createElement('div'); canvas.className = 'graph-canvas'; canvas.tabIndex = 0;
  canvas.setAttribute('aria-label', '工单流程图'); canvas.append(path);
  select.onchange = draw; draw(); container.replaceChildren(label, canvas);
  if (canvas.scrollWidth > canvas.clientWidth) {
    const root = path.querySelector('[data-node]');
    if (root) canvas.scrollLeft = Math.max(0, root.offsetLeft + root.offsetWidth / 2 - canvas.clientWidth / 2);
  }
}

export function downloadPhone(file) {
  const blob = new Blob([JSON.stringify(file, null, 2)], {type: 'application/json'});
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob);
  link.download = file.title + '.phone.json'; link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
