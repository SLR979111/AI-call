/* Public text/layout engine. Inputs are text events, never business decisions.
 * All DOM, typography, candidate transitions, scrolling and indicators live
 * here. TextMotion is the private rendering/animation layer. */
class TextLayoutEngine extends EventTarget {
  constructor(root, viewport, {interactiveCandidates = true} = {}) {
    super();
    this.root = root; this.viewport = viewport;
    root.classList.add('conversation'); viewport.classList.add('text-engine-viewport');
    this.motion = new TextMotion(root, viewport);
    this.records = new Map(); this.current = new Map(); this.rows = new Map();
    this.candidateData = []; this.candidateNodes = new Map();
    this.interactiveCandidates = interactiveCandidates;
    this.listeners = new AbortController();
    const options = {signal: this.listeners.signal, passive: true};
    viewport.addEventListener('wheel', event => { if (event.deltaY < 0) this.setFollow(false); }, options);
    let touchY = 0;
    viewport.addEventListener('touchstart', event => { touchY = event.touches[0]?.clientY ?? 0; }, options);
    viewport.addEventListener('touchmove', event => {
      if ((event.touches[0]?.clientY ?? touchY) - touchY > 4) this.setFollow(false);
    }, options);
    viewport.addEventListener('keydown', event => {
      if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) this.setFollow(false);
    }, {signal: this.listeners.signal});
    viewport.addEventListener('scroll', () => {
      // Scrollbar dragging and assistive scrolling do not emit wheel events.
      const rect = viewport.getBoundingClientRect();
      const resizing = this.motion.queue.has('resize') || rect.width !== this.motion.viewportSize[0] || rect.height !== this.motion.viewportSize[1];
      if (this.motion.follow && !resizing && viewport.scrollTop < Math.max(0, this.motion.scroll.value) - 2) this.setFollow(false);
      if (!this.motion.follow && viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop < 1) this.setFollow(true);
    }, options);
    this.reset();
  }

  // Handles are scoped to one stream. A cancelled/reset handle is inert, even
  // if a later stream reuses the same visible id.
  begin(id, role, {text = '', history = false} = {}) {
    if (!['user', 'assistant'].includes(role)) throw new TypeError('Unknown speaker');
    if (this.records.has(id)) throw new Error(`Duplicate text id: ${id}`);
    const handle = Object.freeze({id});
    this.records.set(id, {id, role, text: String(text), handle, closed: false});
    if (!history) this.current.set(role, id);
    this.invalidate(); return handle;
  }
  record(handle) { const value = this.records.get(handle?.id); return value?.handle === handle && !value.closed ? value : null; }
  append(handle, delta) { const record = this.record(handle); if (!record) return false; record.text += delta; this.invalidate(); return true; }
  revise(handle, text) { const record = this.record(handle); if (!record) return false; record.text = String(text); this.invalidate(); return true; }
  finish(handle) { const record = this.record(handle); if (record) record.closed = true; }
  cancel(handle) { this.finish(handle); }
  setCandidateProgress(value) { this.candidateProgress = value; this.invalidate(); }
  setCandidates(items, progress) {
    this.candidateProgress = progress;
    if (new Set(items.map(item => item.id)).size !== items.length) throw new Error('Duplicate candidate id');
    this.candidateData = items.map(({id, text}) => ({id, text: String(text)})); this.invalidate();
  }
  promote(candidateId, messageId) {
    const candidate = this.candidateData.find(item => item.id === candidateId);
    if (!candidate) return null;
    const handle = this.begin(messageId, 'assistant', {text: candidate.text});
    const copy = this.candidateNodes.get(candidateId)?.querySelector('.copy');
    this.records.get(messageId).adopt = copy;
    this.candidateData = []; this.invalidate(); return handle;
  }
  setInput({active = false, level = 1} = {}) {
    this.input = {active: !!active, level: Math.max(0, Math.min(1, Number(level) || 0))};
    this.motion.wake();
  }
  pause(value) { this.motion.pause(!!value); }
  onFrame(listener) { return this.motion.addTicker(listener); }
  graphemes(text) { return this.motion.split(text); }
  invalidate() { this.motion.batch('view', () => this.render()); }
  setFollow(value) {
    if (this.motion.follow === value) return;
    this.motion.follow = value;
    this.motion.scroll.value = this.viewport.scrollTop; this.motion.scroll.velocity = 0;
    if (value) this.motion.wake();
    this.dispatchEvent(new CustomEvent('followchange', {detail: {following: value}}));
  }
  followLatest() {
    this.setFollow(true);
    this.invalidate();
  }
  snapshot() {
    return {messages: [...this.records.values()].map(({id, role, text, closed}) =>
      ({id, role, text, closed, current: this.current.get(role) === id})),
    candidates: this.candidateData.map(item => ({...item})), input: {...this.input}, paused: this.motion.paused};
  }

  makeCopy(text = '') {
    const copy = document.createElement('p'); copy.className = 'copy';
    this.motion.write(copy, text); return copy;
  }
  render() {
    for (const record of this.records.values()) {
      let row = this.rows.get(record.id);
      if (!row) {
        row = document.createElement('section'); row.dataset.id = record.id; row.dataset.role = record.role;
        row.setAttribute('aria-label', record.role === 'user' ? '用户' : '客服');
        // Run candidate measurement in the render frame, outside the socket
        // handler that registers the unit with the audio player.
        if (record.adopt) this.motion.promote(record.adopt);
        row.append(record.adopt || this.makeCopy());
        this.rows.set(record.id, row);
        this.root.insertBefore(row, this.candidates || this.micDock);
      }
      const current = this.current.get(record.role) === record.id;
      row.className = `message ${record.role}${current ? ' focus' : ''}`;
      const copy = row.querySelector('.copy'); copy.dataset.alpha = current ? 1 : .48;
      this.motion.write(copy, record.text);
    }
    if (this.candidateData.length && !this.candidates) {
      this.candidates = document.createElement(this.interactiveCandidates ? 'div' : 'details');
      this.candidates.className = 'candidates';
      this.candidateList = this.candidates;
      if (!this.interactiveCandidates) {
        const summary = document.createElement('summary');
        this.candidateList = document.createElement('div');
        this.candidateList.className = 'candidate-list';
        this.candidates.append(summary, this.candidateList);
        this.candidates.addEventListener('toggle', () => this.invalidate());
      }
      this.root.insertBefore(this.candidates, this.micDock);
    }
    const visible = this.interactiveCandidates || this.candidates?.open;
    if (!this.interactiveCandidates && this.candidates) {
      const summary = this.candidates.firstElementChild;
      const progress = this.candidateProgress;
      const fraction = progress?.target ? progress.ready / progress.target : 0;
      summary.className = 'candidate-pie';
      summary.style.setProperty('--fill', Math.min(progress?.complete ? 1 : .96, fraction) + 'turn');
      summary.dataset.complete = String(!!progress?.complete);
      const label = '备选回复 · ' + this.candidateData.length + ' 条' +
        (progress?.complete ? '，已就绪' : progress?.settled ? '，部分语音未就绪' : '，准备中');
      summary.setAttribute('aria-label', label); summary.title = label;
    }
    const rows = visible ? this.candidateData : [];
    const wanted = new Set(rows.map(item => item.id));
    for (const [id, node] of this.candidateNodes) if (!wanted.has(id)) { node.remove(); this.candidateNodes.delete(id); }
    for (const candidate of rows) {
      let button = this.candidateNodes.get(candidate.id);
      if (!button) {
        button = document.createElement(this.interactiveCandidates ? 'button' : 'div');
        button.className = 'candidate'; button.dataset.candidate = candidate.id;
        button.append(this.makeCopy());
        if (this.interactiveCandidates) button.addEventListener('click', () => this.dispatchEvent(new CustomEvent('candidatepick', {detail: {id: candidate.id}})));
        this.candidateNodes.set(candidate.id, button);
      }
      button.setAttribute('aria-label', `备选回复：${candidate.text}`);
      const copy = button.querySelector('.copy'); copy.dataset.alpha = .44; this.motion.write(copy, candidate.text);
      this.motion.marker(copy, true);
      const index = this.candidateData.indexOf(candidate);
      if (this.candidateList.children[index] !== button) this.candidateList.insertBefore(button, this.candidateList.children[index] || null);
    }
    if (!this.candidateData.length) { this.candidates?.remove(); this.candidates = null; }
  }

  draw(delta) {
    const state = this.input.active ? 'active' : 'idle';
    if (this.micDock.dataset.state !== state) this.micDock.dataset.state = state;
    this.drawElapsed += delta;
    const target = this.input.active ? this.input.level : 0;
    const previous = this.micLevel;
    this.micLevel += (target - this.micLevel) * (1 - Math.exp(-delta / (target > this.micLevel ? 65 : 280)));
    this.micPulse = Math.max(this.micPulse * Math.exp(-delta / 190), Math.max(0, this.micLevel - previous) * 5);
    this.waveTime += delta / 1000 * (.65 + this.micLevel * 4.2);
    if (!this.input.active && this.micLevel < .01 && this.drawElapsed < 50) return;
    this.drawElapsed = 0;
    const wave = this.wave, level = this.micLevel;
    const width = wave.canvas.width, height = wave.canvas.height;
    const phase = this.motion.reduced.matches ? 0 : this.waveTime;
    wave.clearRect(0, 0, width, height);
    for (let line = 2; line >= 0; line--) {
      const alpha = [.29, .16, .09][line] + level * [.47, .29, .16][line];
      const gradient = wave.createLinearGradient(0, 0, width, 0);
      gradient.addColorStop(0, 'rgba(193,232,244,0)');
      gradient.addColorStop(.35, `rgba(193,232,244,${alpha})`);
      gradient.addColorStop(.65, `rgba(230,237,244,${alpha})`);
      gradient.addColorStop(1, 'rgba(193,232,244,0)');
      wave.beginPath(); wave.strokeStyle = gradient;
      wave.lineWidth = line ? 1.1 : 1.7;
      for (let x = 0; x <= width; x += 2) {
        const envelope = Math.sin(Math.PI * x / width) ** 1.25;
        const harmonic = .7 * Math.sin(x * .065 - phase + line * .65)
          + .22 * Math.sin(x * .113 + phase * 1.31 + line)
          + .08 * Math.sin(x * .031 - phase * .59);
        const amp = 1.6 + level * (13 + 2 * Math.sin(phase * .4 + line)) + this.micPulse * 3;
        const y = height / 2 + harmonic * envelope * amp * (1 - line * .14);
        const visibleX = width / 2 + (x - width / 2) * (.8 + level * .2);
        x ? wave.lineTo(visibleX, y) : wave.moveTo(visibleX, y);
      }
      wave.stroke();
    }
  }
  reset() {
    this.motion.reset(); this.records.clear(); this.current.clear(); this.rows.clear(); this.candidateNodes.clear();
    this.candidates = null; this.candidateData = [];
    this.input = {active: false, level: 0}; this.micLevel = 0; this.micPulse = 0; this.waveTime = 0; this.drawElapsed = 0;
    this.micDock = document.createElement('div'); this.micDock.className = 'microphone'; this.micDock.dataset.state = 'idle';
    this.micDock.setAttribute('aria-label', '麦克风输入指示');
    const canvas = document.createElement('canvas'); canvas.className = 'wave'; canvas.width = 224; canvas.height = 64;
    canvas.setAttribute('aria-hidden', 'true'); this.micDock.append(canvas); this.root.append(this.micDock);
    this.wave = canvas.getContext('2d'); this.motion.pause(false);
    this.motion.addTicker(delta => this.draw(delta));
  }
  destroy() { this.listeners.abort(); this.records.clear(); this.motion.destroy(); }
}
window.TextLayoutEngine = TextLayoutEngine;
