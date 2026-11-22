import {readPhone, showGraph, downloadPhone} from './phone-file.js';

const $ = id => document.getElementById(id);
const PREFIX = 'g2-order/';
const blank = () => ({title: '', goal: '', facts: '', opening: '', checkpoints: []});
export const sameCard = (a, b) => !!a && !!b && ['title', 'goal', 'facts', 'opening'].every(k => a[k] === b[k])
  && JSON.stringify(a.checkpoints) === JSON.stringify(b.checkpoints);

function cardFromFile(file) {
  return {title: file.title, opening: file.basic_response.opening, facts: file.background,
    checkpoints: file.checkpoints, goal: file.graph.nodes.find(n => n.id === file.graph.default_leaf).goal};
}
const businessNode = n => !n.kind || n.kind === 'business';
function leaves(file) {
  const parents = new Set(file.graph.nodes.flatMap(n => n.requires));
  return file.graph.nodes.filter(n => !parents.has(n.id));
}
function checks(container, items, selected, disabled = new Set()) {
  container.replaceChildren(...items.map(([id, text]) => {
    const row = document.createElement('label'), box = document.createElement('input');
    box.type = 'checkbox'; box.value = id; box.checked = selected.includes(id); box.disabled = disabled.has(id);
    row.append(box, document.createTextNode(text)); return row;
  }));
}
function checked(id) { return [...$(id).querySelectorAll('input:checked')].map(el => el.value); }

export function workOrders({mode, locked, activate, progress}) {
  const entries = new Map();
  let activeId, selected, card, file, stage = 'view', revision = 0, pending = false, nodeId;
  const dialog = $('taskDialog');
  const inReview = () => stage === 'review';
  function message(value = '') {
    $('orderMessage').textContent = value; $('orderMessage').hidden = !value;
  }
  function options() {
    const select = $('taskPack'); select.replaceChildren();
    for (const [id, entry] of entries) select.add(new Option(entry.card.title, id));
    if (!entries.has(selected)) select.add(new Option(card?.title || '新工单', selected));
    select.value = selected;
  }
  function controls() {
    const busy = locked() || pending;
    $('taskPack').disabled = locked() || stage !== 'view';
    $('addOrder').disabled = busy || stage !== 'view'; $('importPhone').disabled = busy || stage !== 'view';
    $('restoreTask').disabled = busy || stage !== 'view';
    $('graphView').hidden = stage === 'form';
    $('taskForm').hidden = stage !== 'form';
    $('generateOrder').disabled = pending;
    for (const el of $('taskForm').querySelectorAll('input, textarea')) el.disabled = pending;
    $('graphView').inert = pending && !!file;
    $('reviewActions').hidden = stage === 'view';
    $('saveTask').hidden = !inReview(); $('saveTask').disabled = busy || !!nodeId;
    $('graphActions').hidden = !file || !!nodeId;
    $('editGraph').hidden = inReview() || locked(); $('editSource').hidden = locked();
    $('addNode').hidden = !inReview(); $('defaultGoalField').hidden = !inReview() || !!nodeId;
    $('downloadPhone').disabled = !file;
    $('nodeForm').hidden = !nodeId;
    for (const id of ['basicOpening','basicGuide','backgroundText']) $(id).readOnly = !inReview();
    $('orderHeading').textContent = stage === 'form' ? '填写工单' : inReview() ? '确认电话文件' : '工单流程';
  }
  function graphTab(materials = false) {
    $('materialsView').hidden = !materials; $('phoneGraph').hidden = materials;
    $('graphTab').setAttribute('aria-pressed', String(!materials));
    $('materialsTab').setAttribute('aria-pressed', String(materials));
  }
  function draw() {
    controls();
    if (!file) { $('phoneGraph').replaceChildren(); return; }
    showGraph($('phoneGraph'), file, stage === 'view' && selected === activeId ? progress() : null, inReview() ? editNode : null);
    $('basicOpening').value = file.basic_response.opening; $('basicGuide').value = file.basic_response.guide;
    $('backgroundText').value = file.background;
    const leafSelect = $('defaultGoal'); leafSelect.replaceChildren(...leaves(file).map(n => new Option(n.goal,n.id)));
    leafSelect.value = file.graph.default_leaf;
  }
  function setActive(id, entry) {
    activeId = id; $('selectedTitle').textContent = entry.card.title;
    $('editTask').title = entry.card.title + ' · 选择工单';
    activate(entry.card, entry.file, id);
  }
  async function view(id, use = false) {
    const version = ++revision;
    const entry = entries.get(id);
    if (!entry) return;
    selected = id; card = entry.card; file = entry.file; stage = 'view'; nodeId = null;
    pending = !file || !!entry.needsValidation; graphTab(); options();
    if (entry.needsValidation) file = null;
    draw(); message(pending ? '正在生成工单流程…' : '');
    try {
      if (!file) {
        const generated = entry.needsValidation ? await readPhone(entry.file, true) : await readPhone(card, false, mode());
        if (version !== revision || !dialog.open) return;
        file = generated; entry.file = generated; entry.needsValidation = false;
      }
      if (use && !locked()) setActive(id, entry);
      message(); draw();
    } catch (e) {
      if (version !== revision) return;
      message(e.message);
      const retry = document.createElement('button'); retry.className = 'action'; retry.textContent = '重试';
      retry.onclick = () => view(id, use); $('orderMessage').append(retry);
      if (!locked()) {
        const revise = document.createElement('button'); revise.className = 'action'; revise.textContent = '编辑工单';
        revise.onclick = () => form(entry.card,id); $('orderMessage').append(revise);
      }
    } finally { if (revision === version) { pending = false; controls(); } }
  }
  function form(source, id) {
    ++revision; pending = false; selected = id; card = source; file = null; stage = 'form'; nodeId = null;
    $('editTitle').value = source.title; $('editGoal').value = source.goal;
    $('editFacts').value = source.facts; $('editOpening').value = source.opening;
    $('editPoints').value = source.checkpoints.map(p => p.description).join('\n');
    $('editPoints').setCustomValidity(''); message(); options(); controls(); $('editTitle').focus();
  }
  function review(data) {
    stage = 'review'; file = structuredClone(data); nodeId = null; graphTab(); options();
    message((data.source === 'work_order' ? '已按工单要点生成草稿。' : '已生成草稿。') + '核对流程、基础应答和背景资料后确认。'); draw();
  }
  function editNode(id) {
    if (!inReview() || locked()) return;
    const n = file.graph.nodes.find(n => n.id === id);
    if (n?.kind === 'greeting') { graphTab(true); $('basicOpening').focus(); return; }
    nodeId = id || 'node_' + crypto.randomUUID().replaceAll('-','');
    $('nodeGoal').value = n?.goal || ''; $('nodeAction').value = n?.action || ''; $('nodeDone').value = n?.done_when || '';
    const blocked = new Set([nodeId]); // Descendants would create a cycle.
    for (let count = 0; count < file.graph.nodes.length; count++) {
      const before = blocked.size;
      for (const v of file.graph.nodes) if (v.requires.some(p => blocked.has(p))) blocked.add(v.id);
      if (before === blocked.size) break;
    }
    checks($('nodeDeps'), file.graph.nodes.filter(v => businessNode(v) && v.id !== nodeId).map(v => [v.id,v.goal]), n?.requires || [], blocked);
    checks($('nodePoints'), file.checkpoints.map(p => [p.id,p.description]), n?.checkpoints || []);
    $('removeNode').hidden = !n; $('removeNode').disabled = file.graph.nodes.filter(businessNode).length <= 2;
    controls(); $('nodeGoal').focus();
  }
  function updateNodes(nodes) {
    const greeting = nodes.find(n => n.kind === 'greeting');
    nodes = nodes.map(n => businessNode(n) && !n.requires.length ? {...n, requires:[greeting.id]} : n);
    file = {...file, graph: {...file.graph,nodes}};
    const goals = leaves(file);
    if (!goals.some(n => n.id === file.graph.default_leaf)) file.graph.default_leaf = goals[0].id;
    nodeId = null; draw(); message('流程已更新，请确认草稿。');
  }
  $('nodeForm').onsubmit = e => {
    e.preventDefault();
    const n = {id:nodeId, goal:$('nodeGoal').value.trim(), action:$('nodeAction').value.trim(),
      done_when:$('nodeDone').value.trim(), requires:checked('nodeDeps'), checkpoints:checked('nodePoints')};
    if (!n.goal || !n.done_when || n.requires.length > 16) { message('请填写小目标、完成依据，前置节点最多 16 个。'); return; }
    const nodes = file.graph.nodes.some(v => v.id === nodeId) ? file.graph.nodes.map(v => v.id === nodeId ? n : v) : [...file.graph.nodes,n];
    updateNodes(nodes);
  };
  $('cancelNode').onclick = () => { nodeId = null; draw(); };
  $('removeNode').onclick = () => updateNodes(file.graph.nodes.filter(n => n.id !== nodeId).map(n => ({...n,requires:n.requires.filter(p => p !== nodeId)})));
  $('addNode').onclick = () => file.graph.nodes.filter(businessNode).length < 28 ? editNode(null) : message('每张图最多 28 个业务节点。');
  $('defaultGoal').onchange = e => { file.graph.default_leaf = e.target.value; };
  $('graphTab').onclick = () => { graphTab(); draw(); }; $('materialsTab').onclick = () => graphTab(true);
  for (const [id, key] of [['basicOpening','opening'],['basicGuide','guide'],['backgroundText','background']]) {
    $(id).oninput = () => {
      if (!inReview()) return;
      const value = $(id).value;
      if (key === 'background') file.background = value;
      else file.basic_response[key] = value;
      if (key === 'opening') {
        const nodes = file.graph.nodes.map(n => n.kind === 'greeting' ? {...n, action:value} : n);
        file = {...file, graph:{...file.graph, nodes}};
      }
    };
  }
  $('editGraph').onclick = () => { if (file && !locked()) review(file); };
  $('taskPack').onchange = e => view(e.target.value, true);
  $('restoreTask').onclick = () => view(entries.keys().next().value, true);
  $('addOrder').onclick = () => form(blank(), 'local-' + crypto.randomUUID());
  $('editSource').onclick = () => form(card, selected);
  $('taskForm').onsubmit = async e => {
    e.preventDefault(); if (pending || locked()) return;
    const descriptions = $('editPoints').value.split('\n').map(s=>s.trim()).filter(Boolean);
    if (!descriptions.length || descriptions.length > 8 || descriptions.some(d => d.length > 300)) {
      $('editPoints').setCustomValidity('请输入 1 至 8 个要点，每项最多 300 字。'); $('editPoints').reportValidity(); return;
    }
    const source = {title:$('editTitle').value.trim(),goal:$('editGoal').value.trim(),facts:$('editFacts').value.trim(),
      opening:$('editOpening').value.trim(),checkpoints:descriptions.map((description,i) =>
        ({id: card.checkpoints[i]?.id || 'p_' + i,description}))};
    const version = ++revision; pending = true; controls(); message('正在生成应答、背景资料和流程图…');
    try {
      const generated = await readPhone(source, false, mode());
      if (revision !== version) return;
      if (entries.get(selected)?.preset && !sameCard(source,card)) selected = 'local-' + crypto.randomUUID();
      card = source; review(generated);
    } catch (e) { if (version === revision) message(e.message); }
    finally { if (version === revision) { pending = false; controls(); } }
  };
  $('editPoints').oninput = () => $('editPoints').setCustomValidity('');
  $('saveTask').onclick = async () => {
    if (!inReview() || locked() || pending || nodeId) return;
    if (!file.basic_response.opening.trim() || !file.basic_response.guide.trim()) {graphTab(true); message('请填写开场白和基础应答。'); return;}
    const covered = new Set(file.graph.nodes.flatMap(n => n.checkpoints));
    const missing = file.checkpoints.filter(p => !covered.has(p.id));
    if (missing.length) { message('请将下列要点关联到节点：' + missing.map(p=>p.description).join('、')); return; }
    const version = ++revision; pending = true; controls();
    try {
      const verified = await readPhone(file, true);
      if (revision !== version || locked()) return;
      const entry = {card, file:verified, preset:entries.get(selected)?.preset || false};
      localStorage.setItem(PREFIX+selected, JSON.stringify(entry));
      entries.set(selected,entry); setActive(selected,entry); dialog.close();
    } catch (e) { if (revision === version) message(e.message); }
    finally { if (revision === version) { pending = false; controls(); } }
  };
  $('downloadPhone').onclick = () => { if (file) downloadPhone(file); };
  $('importPhone').onclick = () => $('phoneInput').click();
  $('phoneInput').onchange = async e => {
    const source = e.target.files[0]; e.target.value = '';
    if (!source || locked()) return;
    const version = ++revision;
    try {
      if (source.size > 400000) throw new Error('电话文件过大。');
      const imported = await readPhone(JSON.parse(await source.text()),true);
      if (revision !== version) return;
      selected = 'local-' + crypto.randomUUID(); card = cardFromFile(imported); review(imported);
    } catch { if (version === revision) message('无法载入此电话文件，请检查格式。'); }
  };
  $('discardDraft').onclick = () => view(activeId);
  $('closeDialog').onclick = () => dialog.close();
  dialog.addEventListener('close', () => { revision++; pending = false; });
  $('editTask').onclick = () => {
    if (!activeId) return;
    dialog.showModal(); view(activeId);
  };
  return {
    async init(packs, current, storedFile, savedCustom, preferredId) {
      for (const [id, card] of packs) entries.set(id,{card,preset:true});
      for (const key of Object.keys(localStorage).filter(k => k.startsWith(PREFIX))) {
        try {
          const item = JSON.parse(localStorage.getItem(key)), id = key.slice(PREFIX.length);
          if (!item.card?.checkpoints?.length || (item.file && item.file.format !== 'ai-call.phone.dag.v2')) continue;
          if (!entries.has(id) || sameCard(item.card,entries.get(id).card)) entries.set(id,{...item,needsValidation:!!item.file});
        } catch {}
      }
      function find(card) { return [...entries].find(([,entry]) => sameCard(entry.card,card))?.[0]; }
      if (savedCustom?.checkpoints?.length && !find(savedCustom)) {
        entries.set('local-legacy',{card:savedCustom});
      }
      activeId = sameCard(entries.get(preferredId)?.card,current) ? preferredId : find(current);
      if (!activeId) { activeId = 'local-' + crypto.randomUUID(); entries.set(activeId,{card:current}); }
      const entry = entries.get(activeId);
      if (storedFile) { entry.file = storedFile; entry.needsValidation = false; }
      else if (entry.needsValidation) {
        try {
          entry.file = await readPhone(entry.file,true);
          entry.needsValidation = false;
        } catch {
          // Keep the stored entry available for repair in the picker.
          const fallback = [...entries].find(([,v]) => v.preset && !v.needsValidation);
          if (!fallback) throw new Error('无法加载已保存的电话文件。');
          setActive(...fallback); $('editTask').disabled = false; return;
        }
      }
      for (const [id, item] of entries) {
        if (!item.preset && !localStorage.getItem(PREFIX+id)) localStorage.setItem(PREFIX+id,JSON.stringify(item));
      }
      setActive(activeId,entry); $('editTask').disabled = false;
    },
    ready(data) { if (data && activeId) entries.get(activeId).file = data; },
    refresh() { if (dialog.open && stage === 'view' && file) draw(); },
  };
}
