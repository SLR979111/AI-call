export async function readPhone(input, validate = false, mode = 'cloud') {
  const response = await fetch('/api/phone-file' + (validate ? '/validate' : '?mode=' + encodeURIComponent(mode)), {
    method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(validate ? '电话文件格式或任务图无效。' : '无法生成任务图，请检查工单。');
  return await response.json();
}

const graphViews = new WeakMap();

export function showGraph(container, file, progress) {
  const old = graphViews.get(container);
  const key = JSON.stringify([progress?.leaf, progress?.current, progress?.completed]);
  if (old?.file === file && old.key === key && container.contains(old.path)) return;
  old?.observer.disconnect();
  const {nodes, default_leaf} = file.graph;
  const byId = new Map(nodes.map(n => [n.id, n]));
  const parents = new Set(nodes.flatMap(n => n.requires));
  const leaves = nodes.filter(n => !parents.has(n.id));
  const label = document.createElement('label'), select = document.createElement('select');
  label.textContent = '通话目标';
  for (const leaf of leaves) select.add(new Option(leaf.goal, leaf.id));
  select.value = byId.has(progress?.leaf) ? progress.leaf : default_leaf;
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
  graphViews.set(container, {file, key, path, observer});
  function draw() {
    const included = new Set();
    function visit(id) {
      if (included.has(id)) return;
      included.add(id); byId.get(id).requires.forEach(visit);
    }
    visit(select.value);
    observer.disconnect(); boxes.clear();
    const rows = [], shown = new Set();
    while (shown.size < included.size) {
      const ready = nodes.filter(n => included.has(n.id) && !shown.has(n.id) && n.requires.every(p => shown.has(p)));
      if (!ready.length) break;
      const rank = document.createElement("div"); rank.className = "phone-rank";
      rows.push(rank);
      for (const n of ready) {
        const li = document.createElement('div'), detail = document.createElement('details');
        const summary = document.createElement('summary');
        const status = progress?.completed?.includes(n.id) ? '✓ ' : n.id === progress?.current ? '当前 · ' : '';
        if (n.id === progress?.current) li.setAttribute('aria-current', 'step');
        summary.textContent = status + n.goal; detail.append(summary);
        const criterion = document.createElement('p'); criterion.textContent = '完成依据 · ' + n.done_when;
        detail.append(criterion);
        if (n.requires.length) {
          const deps = document.createElement('p'); deps.textContent = '依赖 · ' + n.requires.map(p => byId.get(p).goal).join('、');
          detail.append(deps);
        }
        li.append(detail); rank.append(li); shown.add(n.id); boxes.set(n.id, li);
      }
    }
    path.replaceChildren(edges, ...rows);
    observer.observe(path);
    for (const box of boxes.values()) observer.observe(box);
  }
  select.onchange = draw; draw(); container.replaceChildren(label, path);
}

export function downloadPhone(file) {
  const blob = new Blob([JSON.stringify(file, null, 2)], {type: 'application/json'});
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob);
  link.download = file.task.title + '.phone.json'; link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
