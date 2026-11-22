const panelMarkup = `<dialog id="analysisDialog" aria-labelledby="analysisTitle">
    <div class="analysis-shell">
      <header class="analysis-heading">
        <div><h1 id="analysisTitle">经验库</h1><p id="analysisScope" class="analysis-meta"></p></div>
        <button id="closeAnalysis" type="button" aria-label="关闭" title="关闭"><svg aria-hidden="true"><use href="/static/icons.svg#close"/></svg></button>
      </header>
      <div class="analysis-tabs" role="tablist" aria-label="经验库">
        <button id="analysisLibraryTab" class="analysis-tab" type="button" role="tab" aria-selected="true" aria-controls="analysisLibraryPanel">已入库</button>
        <button id="analysisReviewTab" class="analysis-tab" type="button" role="tab" aria-selected="false" aria-controls="analysisReviewPanel" tabindex="-1">复盘</button>
      </div>
      <div class="analysis-body">
        <p id="analysisError" class="analysis-error" role="alert" hidden></p>
        <section id="analysisLibraryPanel" role="tabpanel" aria-labelledby="analysisLibraryTab" tabindex="0">
          <div class="analysis-toolbar">
            <p id="analysisLibraryLabel" class="analysis-meta" role="status">读取中…</p>
            <button id="toggleAnalysisVersions" class="analysis-quiet" type="button" aria-expanded="false" aria-controls="analysisVersions" hidden>版本</button>
            <button id="analysisGoToReview" class="analysis-quiet" type="button" hidden>去复盘</button>
          </div>
          <div id="analysisVersions" class="analysis-actions" hidden>
            <select id="analysisVersion" aria-label="选择经验库版本"></select>
            <button id="activateAnalysisVersion" type="button">切换版本</button>
          </div>
          <ul id="analysisLibraryRules" class="analysis-rule-list"></ul>
        </section>
        <section id="analysisReviewPanel" role="tabpanel" aria-labelledby="analysisReviewTab" tabindex="0" hidden>
          <div class="analysis-toolbar">
            <span id="analysisMotion" class="analysis-motion" aria-hidden="true" hidden><i></i><i></i><i></i><i></i></span>
            <p id="analysisStatus" class="analysis-status" role="status"></p>
            <button id="toggleAnalysisSettings" class="analysis-quiet" type="button" aria-expanded="false" aria-controls="analysisTrigger" hidden>设置</button>
            <button id="runCallAnalysis" class="analysis-primary" type="button" hidden>开始复盘</button>
          </div>
          <form id="analysisTrigger" class="analysis-section" hidden>
            <div class="analysis-fields">
              <label>每批通数<input id="analysisBatchSize" type="number" min="2" max="12" required></label>
              <label>至少未完成通数<input id="analysisMinIncomplete" type="number" min="0" max="12" required></label>
            </div>
            <label class="analysis-toggle">计入文字试聊<input id="analysisIncludeText" class="toggle-switch" type="checkbox" role="switch"></label>
            <label class="analysis-toggle">满足条件后自动分析<input id="analysisAutomatic" class="toggle-switch" type="checkbox" role="switch"></label>
            <div class="analysis-actions"><button type="submit" class="analysis-primary">保存条件</button></div>
          </form>
          <p id="analysisNoReports" class="analysis-meta analysis-empty-report" hidden>暂无复盘报告</p>
          <section id="analysisReports" class="analysis-section" hidden>
            <div class="analysis-report-heading">
              <h2>复盘报告</h2><span id="analysisDraftStatus" class="analysis-meta" role="status"></span>
              <select id="analysisReport" aria-label="选择分析报告" hidden></select>
            </div>
            <div id="analysisReportContent"></div>
            <details id="analysisAcceptedHistory" hidden>
              <summary>当时入库的经验<svg aria-hidden="true"><use href="/static/icons.svg#chevron"/></svg></summary>
              <ul id="analysisAcceptedRules" class="analysis-rule-list"></ul>
            </details>
            <form id="analysisExperienceDraft" novalidate hidden>
              <div id="analysisRules" class="analysis-rule-list"></div>
            </form>
          </section>
        </section>
      </div>
      <footer id="analysisReviewFooter" class="analysis-review-footer" hidden>
        <span id="analysisReviewCount" class="analysis-meta" role="status"></span>
        <button id="applyAnalysis" type="submit" form="analysisExperienceDraft" class="analysis-primary">确认入库</button>
      </footer>
    </div>
  </dialog>`;

const $ = id => document.getElementById(id);
const outcome = {complete: '已完成', active: '结束时未完成', paused: '已暂停', declined: '已拒绝'};
const versionName = version => version == null ? '原工单' : '版本 ' + version;
const ruleKinds = {flow: '流程', reply: '应答'};
const dateFormat = new Intl.DateTimeFormat('zh-CN', {
  month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const dateLabel = value => dateFormat.format(new Date(value * 1000));

function element(tag, text = '') {
  const item = document.createElement(tag);
  item.textContent = text;
  return item;
}

function historyLink(id, text, turn) {
  const link = element('a', text);
  const params = new URLSearchParams({call: id});
  if (turn) params.set('turn', turn);
  link.href = '/history?' + params;
  link.target = '_blank'; link.rel = 'noopener';
  return link;
}

function ruleCopy(rule) {
  const copy = element('span');
  copy.className = 'analysis-rule-copy';
  const headline = element('span');
  headline.className = 'analysis-rule-headline';
  const kind = element('span');
  kind.className = 'analysis-rule-kind';
  const condition = element('span');
  condition.className = 'analysis-rule-condition';
  const guidance = element('span');
  guidance.className = 'analysis-rule-guidance';
  headline.append(kind, condition);
  copy.append(headline, guidance);
  const update = value => {
    kind.textContent = ruleKinds[value.kind];
    kind.dataset.kind = value.kind;
    condition.textContent = value.when || '适用情境';
    guidance.textContent = value.guidance;
  };
  update(rule);
  return {copy, update};
}

function readOnlyRule(rule) {
  const item = element('li');
  item.className = 'analysis-rule';
  item.append(ruleCopy(rule).copy);
  return item;
}

function chevron() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  const use = document.createElementNS(ns, 'use');
  svg.setAttribute('aria-hidden', 'true');
  use.setAttribute('href', '/static/icons.svg#chevron');
  svg.append(use);
  return svg;
}

export function bindCallAnalysis(getScope) {
  document.body.insertAdjacentHTML('beforeend', panelMarkup);
  const dialog = $('analysisDialog');
  const panelBody = dialog.querySelector('.analysis-body');
  const libraryTab = $('analysisLibraryTab');
  const reviewTab = $('analysisReviewTab');
  const tabs = [libraryTab, reviewTab];
  const settingsButton = $('toggleAnalysisSettings');
  const versionsButton = $('toggleAnalysisVersions');
  const trigger = $('analysisTrigger');
  const run = $('runCallAnalysis');
  const reportSelect = $('analysisReport');
  const versionSelect = $('analysisVersion');
  const editor = $('analysisExperienceDraft');
  const rules = $('analysisRules');
  const acceptedRules = $('analysisAcceptedRules');
  const acceptedHistory = $('analysisAcceptedHistory');
  const reviewFooter = $('analysisReviewFooter');
  let activeTab = libraryTab;
  let scope, data, polling, loading, mutation, reportKey;
  let editingTrigger = false;
  let operationError = '';
  let revision = 0;
  let libraryVersion;

  function selectTab(tab, {focus = false} = {}) {
    activeTab = tab;
    for (const item of tabs) {
      const selected = item === tab;
      item.tabIndex = selected ? 0 : -1;
      item.setAttribute('aria-selected', String(selected));
      $(item.getAttribute('aria-controls')).hidden = !selected;
    }
    panelBody.scrollTop = 0;
    renderActions();
    if (focus) tab.focus();
  }

  function setDisclosure(button, open) {
    button.setAttribute('aria-expanded', String(open));
    $(button.getAttribute('aria-controls')).hidden = !open;
  }

  function analysisStatus(text, running = false) {
    $('analysisMotion').hidden = !running;
    run.hidden = !data || running;
    if ($('analysisStatus').textContent !== text) $('analysisStatus').textContent = text;
  }

  function receive(next, latest = false) {
    const report = next.reports[0];
    const arrived = report && data && report.id !== data.reports[0]?.id;
    data = next;
    render({latest});
    if (arrived && activeTab === reviewTab && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
      $('analysisReports').animate([{opacity: .5, transform: 'translateY(4px)'}, {opacity: 1, transform: 'none'}],
        {duration: 270, easing: 'ease-out'});
    }
  }

  async function request(path, fields = {}) {
    const response = await fetch('/api/call-analysis/' + path, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...scope, ...fields}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '请检查输入或重试。');
    return result;
  }

  function addRule(value) {
    const row = element('details');
    row.className = 'analysis-rule';
    row.setAttribute('name', 'analysis-rule-editor');
    const summary = element('summary');
    const {copy, update} = ruleCopy(value);
    summary.append(copy, chevron());
    row.append(summary);

    const fields = element('div');
    fields.className = 'analysis-rule-fields';
    const kindLabel = element('label', '类型');
    const kind = element('select');
    kind.name = 'kind';
    kind.replaceChildren(...Object.entries(ruleKinds).map(([id, label]) => new Option(label, id)));
    kind.value = value.kind;
    kindLabel.append(kind);
    fields.append(kindLabel);
    for (const [name, title, maxLength, rows] of [['when', '适用情境', 80, 1], ['guidance', '约束', 140, 2]]) {
      const label = element('label', title);
      const input = element('textarea');
      input.name = name; input.required = true; input.maxLength = maxLength;
      input.rows = rows; input.value = value[name];
      input.oninput = () => input.setCustomValidity('');
      label.append(input);
      fields.append(label);
    }
    fields.oninput = () => update(ruleValues(row));
    const actions = element('div');
    actions.className = 'analysis-rule-actions';
    const sources = element('span', value.calls?.length ? '样本 ' + value.calls.join('、') : '');
    const remove = element('button', '移除');
    remove.type = 'button';
    remove.onclick = () => {
      const nearby = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      renderActions();
      (nearby?.querySelector('summary') || (reportSelect.hidden ? reviewTab : reportSelect)).focus();
    };
    actions.append(sources, remove);
    fields.append(actions);
    row.append(fields);
    rules.append(row);
  }

  function ruleValues(row) {
    return Object.fromEntries(['kind', 'when', 'guidance'].map(name =>
      [name, row.querySelector('[name="' + name + '"]').value.trim()]));
  }

  function renderReport(report) {
    const key = report && [report.id, Boolean(report.accepted)].join(':');
    if (reportKey === key) return;
    reportKey = key;
    const content = $('analysisReportContent');
    content.replaceChildren();
    const suggestions = report?.accepted ? [] : (report?.experiences || []);
    editor.hidden = !suggestions.length;
    rules.replaceChildren();
    const accepted = report?.accepted || [];
    acceptedHistory.open = false;
    acceptedRules.replaceChildren(...accepted.map(readOnlyRule));
    if (!report) return;
    const lead = element('p', report.summary);
    lead.className = 'analysis-report-summary';
    content.append(lead);
    const samples = element('details');
    samples.className = 'analysis-samples';
    const sourceSummary = element('summary', '样本依据 · ' + new Set(report.samples.map(sample => sample.work_order)).size + ' 张工单');
    sourceSummary.append(chevron());
    samples.append(sourceSummary);
    const items = element('ul');
    for (const sample of report.samples) {
      const item = element('li');
      const labels = [outcome[sample.outcome] || '无结果记录', versionName(sample.version)];
      if (sample.truncated) labels.push('转写节选');
      item.append(historyLink(sample.id, '样本 ' + sample.number + ' · ' + sample.title), ' · ' + labels.join(' · '));
      if (sample.findings.length) {
        const findings = element('ul');
        for (const finding of sample.findings) {
          const reason = element('li');
          reason.append(historyLink(sample.id, '记录第 ' + finding.turn + ' 条', finding.turn), ' · ' + finding.observation);
          findings.append(reason);
        }
        item.append(findings);
      }
      items.append(item);
    }
    samples.append(items); content.append(samples);
    suggestions.forEach(addRule);
  }

  function renderActions() {
    if (!data) return;
    const report = data.reports.find(item => item.id === Number(reportSelect.value));
    const approved = Boolean(report?.accepted);
    const busy = mutation?.scope === scope;
    for (const field of trigger.elements) field.disabled = busy;
    run.disabled = busy || data.running || !data.ready || editingTrigger;
    $('activateAnalysisVersion').disabled = busy || data.running || versionSelect.value === String(data.active_version ?? '');
    const submitting = busy && mutation.path === 'apply';
    for (const field of editor.elements) field.disabled = submitting;
    const count = rules.children.length;
    reviewFooter.hidden = activeTab !== reviewTab || approved || editor.hidden;
    $('applyAnalysis').disabled = busy || !count;
    const countLabel = count + ' 条待入库';
    if ($('analysisReviewCount').textContent !== countLabel) $('analysisReviewCount').textContent = countLabel;
    $('analysisDraftStatus').textContent = approved ? '已入库' : '';
    acceptedHistory.hidden = !report?.accepted?.some(rule => !data.library.some(current =>
      rule.kind === current.kind && rule.when === current.when && rule.guidance === current.guidance));
    if (acceptedHistory.hidden) acceptedHistory.open = false;
  }

  function render({latest = false} = {}) {
    const {trigger: current, pending_voice, pending_text, pending_incomplete, pending_orders, active_version} = data;
    settingsButton.hidden = false;
    if (!editingTrigger) {
      $('analysisBatchSize').value = current.batch_size;
      $('analysisMinIncomplete').value = current.min_incomplete;
      $('analysisIncludeText').checked = current.include_text;
      $('analysisAutomatic').checked = current.automatic;
    }
    const eligible = pending_voice + (current.include_text ? pending_text : 0);
    const running = data.running || (mutation?.scope === scope && mutation.path === 'run');
    const progress = '待复盘 ' + eligible + ' / ' + current.batch_size + ' 通'
      + (current.min_incomplete ? ' · 未完成 ' + pending_incomplete + ' / ' + current.min_incomplete : '')
      + (eligible >= current.batch_size && pending_orders < 2 ? ' · 需两张工单' : '');
    analysisStatus(running ? '分析中…' : progress, running);
    const error = operationError || data.last_error || '';
    $('analysisError').textContent = error;
    $('analysisError').hidden = !error;

    const versionValue = versionSelect.value;
    const hasVersions = data.versions.length > 0;
    versionsButton.hidden = !hasVersions;
    if (!hasVersions) setDisclosure(versionsButton, false);
    if (libraryVersion !== active_version) {
      libraryVersion = active_version;
      $('analysisLibraryLabel').textContent = data.library.length ? data.library.length + ' 条经验' : '暂无已入库经验';
      $('analysisLibraryRules').replaceChildren(...data.library.map(readOnlyRule));
      $('analysisGoToReview').hidden = Boolean(data.library.length);
    }
    if (versionSelect.options.length !== data.versions.length + 1) {
      versionSelect.replaceChildren(new Option('空库', ''), ...data.versions.map(version =>
        new Option(versionName(version.id) + ' · ' + dateLabel(version.created_at), version.id)));
      versionSelect.value = String(active_version ?? '');
    } else if ([...versionSelect.options].some(option => option.value === versionValue)) {
      versionSelect.value = versionValue;
    }
    const reportValue = reportSelect.value;
    $('analysisReports').hidden = !data.reports.length;
    $('analysisNoReports').hidden = Boolean(data.reports.length) || running;
    reportSelect.hidden = data.reports.length < 2;
    const pending = data.reports.some(report => !report.accepted && report.experiences?.length);
    reviewTab.toggleAttribute('data-pending', pending);
    if (pending) reviewTab.setAttribute('aria-description', '有待入库经验');
    else reviewTab.removeAttribute('aria-description');
    if (reportSelect.options[0]?.value !== String(data.reports[0]?.id)) {
      reportSelect.replaceChildren(...data.reports.map(item =>
        new Option(item.id + ' · ' + dateLabel(item.created_at), item.id)));
      if (!latest && data.reports.some(item => String(item.id) === reportValue)) reportSelect.value = reportValue;
    } else if (latest) {
      reportSelect.selectedIndex = 0;
    }
    renderReport(data.reports.find(item => item.id === Number(reportSelect.value)));
    renderActions();
  }

  async function refresh() {
    if (loading || (mutation?.scope === scope && mutation.path !== 'run') || !dialog.open) return;
    const current = scope;
    const before = revision;
    loading = current;
    try {
      const result = await request('status');
      if (scope !== current || !dialog.open || before !== revision) return;
      receive(result);
    } catch (error) {
      if (scope === current && dialog.open && !data) {
        $('analysisLibraryLabel').textContent = '';
        $('analysisStatus').textContent = '';
        $('analysisError').textContent = error.message;
        $('analysisError').hidden = false;
      }
    } finally {
      if (loading === current) loading = null;
    }
  }

  async function change(path, fields) {
    if (mutation) return;
    const current = scope;
    mutation = {scope: current, path};
    revision++;
    operationError = '';
    if (path === 'run') {
      $('analysisError').hidden = true;
      analysisStatus('分析中…', true);
    }
    renderActions();
    try {
      const result = await request(path, fields);
      if (scope !== current) return;
      if (path === 'trigger') {
        editingTrigger = false;
        setDisclosure(settingsButton, false);
        if (dialog.open && activeTab === reviewTab) settingsButton.focus();
      }
      if (path === 'apply' || path === 'activate') versionSelect.value = String(result.active_version ?? '');
      receive(result, path === 'run');
      if (path === 'apply' && dialog.open) selectTab(libraryTab, {focus: true});
    } catch (error) {
      if (scope === current) {
        operationError = error.message;
        render();
        if (path === 'apply' && dialog.open) $('analysisError').scrollIntoView({block: 'nearest'});
      }
    } finally {
      mutation = null;
      revision++;
      if (data) render();
      refresh();
    }
  }

  $('openCallAnalysis').onclick = () => {
    scope = getScope();
    if (!scope?.mode) return;
    data = null; loading = null; editingTrigger = false; reportKey = null; operationError = '';
    libraryVersion = undefined;
    $('analysisMinIncomplete').setCustomValidity('');
    $('analysisScope').textContent = '跨工单 · ' + (scope.mode === 'local' ? '纯本地' : '云端');
    analysisStatus('读取中…');
    setDisclosure(settingsButton, false);
    setDisclosure(versionsButton, false);
    reviewTab.removeAttribute('data-pending');
    reviewTab.removeAttribute('aria-description');
    $('analysisLibraryLabel').textContent = '读取中…';
    $('analysisLibraryRules').replaceChildren();
    $('analysisError').hidden = settingsButton.hidden = versionsButton.hidden = run.hidden =
      $('analysisGoToReview').hidden = $('analysisNoReports').hidden = $('analysisReports').hidden = reviewFooter.hidden = true;
    reportSelect.replaceChildren(); versionSelect.replaceChildren();
    selectTab(libraryTab);
    dialog.showModal();
    refresh();
    polling = setInterval(refresh, 3500);
  };
  $('closeAnalysis').onclick = () => dialog.close();
  dialog.onclose = () => clearInterval(polling);
  for (const tab of tabs) {
    tab.onclick = () => selectTab(tab);
    tab.onkeydown = event => {
      let next;
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') next = tab === libraryTab ? reviewTab : libraryTab;
      else if (event.key === 'Home') next = libraryTab;
      else if (event.key === 'End') next = reviewTab;
      if (next) {
        event.preventDefault();
        selectTab(next, {focus: true});
      }
    };
  }
  $('analysisGoToReview').onclick = () => selectTab(reviewTab, {focus: true});
  settingsButton.onclick = () => setDisclosure(settingsButton, trigger.hidden);
  versionsButton.onclick = () => setDisclosure(versionsButton, $('analysisVersions').hidden);
  trigger.oninput = () => {
    editingTrigger = true;
    $('analysisMinIncomplete').setCustomValidity('');
    renderActions();
  };
  trigger.onsubmit = event => {
    event.preventDefault();
    const size = $('analysisBatchSize').valueAsNumber;
    const incomplete = $('analysisMinIncomplete').valueAsNumber;
    if (incomplete > size) {
      $('analysisMinIncomplete').setCustomValidity('未完成通数不能超过每批通话数。');
      $('analysisMinIncomplete').reportValidity();
      return;
    }
    change('trigger', {trigger: {batch_size: size, min_incomplete: incomplete,
      automatic: $('analysisAutomatic').checked, include_text: $('analysisIncludeText').checked}});
  };
  $('runCallAnalysis').onclick = () => change('run');
  reportSelect.onchange = () => render();
  versionSelect.onchange = renderActions;
  $('activateAnalysisVersion').onclick = () => change('activate', {
    version: versionSelect.value ? Number(versionSelect.value) : null, expected_version: data.active_version,
  });
  editor.onsubmit = event => {
    event.preventDefault();
    for (const field of editor.querySelectorAll('textarea')) {
      field.setCustomValidity(field.value.trim() ? '' : '请填写此项。');
      if (!field.validity.valid) {
        field.closest('details').open = true;
        field.reportValidity();
        return;
      }
    }
    const submitted = [...rules.children].map(ruleValues);
    if (submitted.length) change('apply', {report_id: Number(reportSelect.value), rules: submitted});
  };
}
