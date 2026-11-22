import {bindCallAnalysis} from './call-analysis.js';

const $ = id => document.getElementById(id);
const main = $('historyMain');
const list = $('historyList');
const detail = $('historyDetail');
const initial = new URLSearchParams(location.search);
const outcomes = {complete: '已完成', active: '未完成', paused: '已暂停', declined: '已拒绝'};
const modes = {local: '纯本地', cloud: '云端'};
const dateTime = new Intl.DateTimeFormat('zh-CN', {dateStyle: 'medium', timeStyle: 'short'});
const clock = new Intl.DateTimeFormat('zh-CN', {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
const shortDate = new Intl.DateTimeFormat('zh-CN', {month: 'long', day: 'numeric'});
const fullDate = new Intl.DateTimeFormat('zh-CN', {dateStyle: 'medium'});
let selectedId, selectedTurn = 0, selectedScope, page = 1;
let searchRequest, detailRequest;
bindCallAnalysis(() => selectedScope);

function node(tag, text = '', className = '') {
  const item = document.createElement(tag);
  if (text) item.textContent = text;
  if (className) item.className = className;
  return item;
}

function formatTime(seconds, formatter = dateTime) {
  const date = new Date(seconds * 1000);
  const label = node('time', formatter.format(date));
  label.dateTime = date.toISOString();
  return label;
}

function icon(name) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  const use = document.createElementNS(ns, 'use');
  svg.setAttribute('aria-hidden', 'true');
  use.setAttribute('href', '/static/icons.svg#' + name);
  svg.append(use);
  return svg;
}

function badge(element, outcome) {
  element.dataset.outcome = outcome ?? 'unknown';
  element.textContent = outcomes[outcome] ?? '未记录';
}

async function fetchHistory(path, signal) {
  const response = await fetch('/api/call-history' + path, {signal});
  if (response.status === 404) throw new Error('这通电话的记录不可用。');
  if (!response.ok) throw new Error('读取历史通话失败，请刷新重试。');
  return response.json();
}

function updateAddress(id, turn) {
  const url = new URL(location.href);
  if (id) url.searchParams.set('call', id);
  else url.searchParams.delete('call');
  if (turn) url.searchParams.set('turn', turn);
  else url.searchParams.delete('turn');
  history.replaceState(null, '', url);
}

function markSelection() {
  for (const row of list.querySelectorAll('.record')) {
    if (row.dataset.id === selectedId) row.setAttribute('aria-current', 'true');
    else row.removeAttribute('aria-current');
  }
}

function drawCallList(calls) {
  const rows = document.createDocumentFragment();
  let day;
  for (const call of calls) {
    const date = new Date(call.ended_at * 1000);
    if (day !== date.toDateString()) {
      day = date.toDateString();
      const formatter = date.getFullYear() === new Date().getFullYear() ? shortDate : fullDate;
      rows.append(node('li', formatter.format(date), 'record-day'));
    }
    const li = node('li');
    const row = node('button', '', 'record');
    row.type = 'button';
    row.dataset.id = call.id;
    row.onclick = () => showCall(call.id, {entered: true});
    const status = node('span', '', 'record-dot');
    status.dataset.outcome = call.outcome ?? 'unknown';
    const kind = call.voice ? '语音' : '试聊';
    const outcome = outcomes[call.outcome] ?? '未记录';
    row.title = [call.title, kind, outcome].join(' · ');
    row.append(icon(call.voice ? 'phone' : 'reply'), node('span', kind, 'sr-only'), node('strong', call.title),
      formatTime(call.ended_at, clock), status, node('span', outcome, 'sr-only'));
    li.append(row);
    rows.append(li);
  }
  list.replaceChildren(rows);
  markSelection();
}

function callDuration(call) {
  if (call.started_at == null || call.ended_at < call.started_at) return '';
  const seconds = Math.round(call.ended_at - call.started_at);
  if (!seconds) return '';
  const minutes = Math.floor(seconds / 60);
  return (minutes ? minutes + ' 分 ' : '') + seconds % 60 + ' 秒';
}

function drawTranscript(call, turn) {
  $('historyCallTitle').textContent = call.title;
  $('historyCallTime').replaceChildren(formatTime(call.started_at ?? call.ended_at));
  $('historyCallInfo').open = false;
  badge($('historyCallOutcome'), call.outcome);
  const meta = [
    ['编号', call.id],
    ['方式', call.voice ? '语音' : '试聊'],
    ['模式', modes[call.mode] ?? '未记录'],
    ['时长', callDuration(call)],
    ['流程', call.version == null ? (call.mode ? '原工单' : '未记录') : '版本 ' + call.version],
  ];
  $('historyCallMeta').replaceChildren(...meta.filter(([, value]) => value).map(([name, value]) => {
    const pair = node('div');
    const definition = node('dd');
    definition.append(value);
    pair.append(node('dt', name), definition);
    return pair;
  }));
  $('historyNoTranscript').hidden = call.transcript.length > 0;
  const messages = document.createDocumentFragment();
  let highlighted;
  let previousSpeaker;
  for (const entry of call.transcript) {
    const message = node('li', '', 'message');
    message.dataset.role = entry.role;
    const speaker = entry.role === 'caller' ? (call.voice ? '来电者' : '试聊者') : 'AI 客服';
    if (speaker !== previousSpeaker) {
      message.append(node('div', speaker, 'message-meta'));
      previousSpeaker = speaker;
    }
    if (entry.role === 'cutoff') message.append(node('span', '打断 · 估算已播', 'cutoff-label'));
    if (entry.at != null) message.title = dateTime.format(new Date(entry.at * 1000));
    if (entry.turn === turn) {
      message.dataset.highlight = '';
      highlighted = message;
    }
    message.append(node('p', entry.text));
    messages.append(message);
  }
  $('historyTranscript').replaceChildren(messages);
  return highlighted;
}

function detailStatus(message) {
  $('historyCall').hidden = true;
  $('historyDetailStatus').hidden = false;
  $('historyDetailStatus').textContent = message;
}

async function showCall(id, {entered = false, turn = 0} = {}) {
  detailRequest?.abort();
  selectedScope = null;
  $('openCallAnalysis').disabled = true;
  selectedId = id;
  selectedTurn = turn;
  markSelection();
  if (entered) {
    main.dataset.mobileDetail = 'true';
    updateAddress(id, turn);
    detail.focus({preventScroll: true});
    if (matchMedia('(max-width: 650px)').matches) window.scrollTo({top: 0});
    else if (detail.getBoundingClientRect().top < 0) detail.scrollIntoView({block: 'start'});
  }
  if (!id) {
    detailStatus('选择通话');
    return;
  }
  detailStatus('读取中…');
  const request = new AbortController();
  detailRequest = request;
  try {
    const call = await fetchHistory('/' + encodeURIComponent(id), request.signal);
    if (request.signal.aborted) return;
    const highlighted = drawTranscript(call, turn);
    selectedScope = call.analysis_scope;
    $('openCallAnalysis').disabled = !selectedScope?.mode;
    $('historyDetailStatus').hidden = true;
    $('historyCall').hidden = false;
    highlighted?.scrollIntoView({block: 'center'});
  } catch (error) {
    if (!request.signal.aborted) detailStatus(error.message);
  }
}

async function search({resetSelection = false} = {}) {
  searchRequest?.abort();
  const request = new AbortController();
  searchRequest = request;
  const params = new URLSearchParams({page});
  const filters = {q: $('historyQuery').value.trim(), kind: $('historyKind').value, outcome: $('historyOutcome').value};
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value);
  }
  if (resetSelection) {
    delete main.dataset.mobileDetail;
    updateAddress();
    showCall();
  }
  $('historyCount').textContent = '读取中…';
  $('prevHistoryPage').disabled = $('nextHistoryPage').disabled = true;
  try {
    const data = await fetchHistory('?' + params, request.signal);
    if (request.signal.aborted) return;
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    page = data.page;
    $('historyCount').textContent = data.total ? data.total + ' 通'
      : Object.values(filters).some(Boolean) ? '无匹配记录' : '暂无通话';
    $('historyPager').hidden = pages <= 1;
    $('historyPageNumber').textContent = page + ' / ' + pages;
    $('prevHistoryPage').disabled = page <= 1;
    $('nextHistoryPage').disabled = page >= pages;
    drawCallList(data.calls);
    if (!selectedId && data.calls.length) showCall(data.calls[0].id);
  } catch (error) {
    if (request.signal.aborted) return;
    $('historyCount').textContent = error.message;
  }
}

function changeFilter() {
  page = 1;
  search({resetSelection: true});
}

$('historyFilters').onsubmit = event => { event.preventDefault(); changeFilter(); };
$('historyKind').onchange = $('historyOutcome').onchange = () => {
  $('historyFilterMenu').dataset.active = String(Boolean($('historyKind').value || $('historyOutcome').value));
  changeFilter();
};
document.addEventListener('click', event => {
  const menu = $('historyFilterMenu');
  if (menu.open && !menu.contains(event.target)) menu.open = false;
});
$('historyQuery').addEventListener('search', () => { if (!$('historyQuery').value) changeFilter(); });
$('prevHistoryPage').onclick = () => { page--; search({resetSelection: true}); };
$('nextHistoryPage').onclick = () => { page++; search({resetSelection: true}); };
$('refreshHistory').onclick = () => {
  const entered = main.dataset.mobileDetail === 'true';
  if (selectedId) showCall(selectedId, {entered, turn: selectedTurn});
  search();
};
$('backToHistoryList').onclick = () => {
  delete main.dataset.mobileDetail;
  updateAddress();
  list.querySelector('[aria-current="true"]')?.focus();
};
if (initial.get('call')) {
  showCall(initial.get('call'), {entered: true, turn: Number(initial.get('turn')) || 0});
}
search();
