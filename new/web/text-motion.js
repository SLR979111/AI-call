/* Retained paragraph and glyph springs. Layout supplies destinations; rendered
 * positions and velocities survive each update. No remounts or replayed tweens. */
class TextMotion {
  constructor(root, viewport) {
    this.root = root; this.viewport = viewport;
    this.queue = new Map(); this.tickers = new Set(); this.lines = new Map();
    this.glyphs = new Map(); this.activeGlyphs = new Set(); this.dirtyLines = new Set();
    this.copyText = new WeakMap(); this.contentOffset = 0;
    this.promotions = new Map();
    this.frameId = 0; this.previousTime = 0; this.paused = false; this.follow = true;
    this.snapNext = true;
    this.entryStep = 22;
    this.scroll = {value: 0, velocity: 0, target: 0};
    this.segmenter = new Intl.Segmenter('zh', {granularity: 'grapheme'});
    this.reduced = matchMedia('(prefers-reduced-motion: reduce)');
    this.visibility = () => {
      this.previousTime = 0;
      if (!document.hidden) this.wake();
    };
    document.addEventListener('visibilitychange', this.visibility);
    const bounds = viewport.getBoundingClientRect();
    this.viewportSize = [bounds.width, bounds.height];
    this.resize = new ResizeObserver(() => {
      const rect = viewport.getBoundingClientRect();
      if (rect.width === this.viewportSize[0] && rect.height === this.viewportSize[1]) return;
      this.viewportSize = [rect.width, rect.height];
      this.batch('resize', () => { this.snapNext = true; });
    });
    this.resize.observe(viewport);
  }
  split(text) { return Array.from(this.segmenter.segment(String(text)), item => item.segment); }
  batch(key, mutation) { this.queue.set(key, mutation); this.wake(); }
  addTicker(ticker) { this.tickers.add(ticker); this.wake(); return () => this.tickers.delete(ticker); }
  wake() { if (!this.frameId && !document.hidden) this.frameId = requestAnimationFrame(time => this.frame(time)); }
  frame(time) {
    this.frameId = 0;
    // A stalled/hidden tab must not consume an entire entrance in one frame.
    const delta = this.previousTime ? Math.min(40, Math.max(0, time - this.previousTime)) : 0;
    this.previousTime = time;
    if (!this.paused) for (const ticker of [...this.tickers]) ticker(delta);
    if (this.queue.size) this.flush();
    const moving = !this.paused && this.advance(delta / 1000);
    if (this.queue.size || (!this.paused && (this.tickers.size || moving))) this.wake();
    else this.previousTime = 0;
  }

  write(copy, text) {
    text = String(text);
    if (this.copyText.get(copy) === text) return;
    this.copyText.set(copy, text);
    copy.setAttribute('aria-label', text);
    // One begin() is one paragraph. Chunks and punctuation never split it;
    // the browser wraps the continuous text at the available width.
    const chars = this.split(text.replace(/[\r\n]+/g, ' '));
    let window = copy.firstChild;
    if (!window) {
      window = document.createElement('span'); window.className = 'sentence-window';
      const line = document.createElement('span'); line.className = 'sentence';
      window.append(line); copy.append(window);
    }
    const line = window.firstChild, nodes = [...line.children].filter(node => !node.classList.contains('candidate-marker'));
    let left = 0, right = 0;
    while (left < nodes.length && left < chars.length && nodes[left].textContent === chars[left]) left++;
    while (right < nodes.length - left && right < chars.length - left &&
      nodes[nodes.length - 1 - right].textContent === chars[chars.length - 1 - right]) right++;
    if (left === chars.length && left === nodes.length) return;
    const anchor = right ? nodes[nodes.length - right] : null;
    for (const node of nodes.slice(left, nodes.length - right)) {
      this.activeGlyphs.delete(this.glyphs.get(node));
      this.glyphs.delete(node);
      node.remove();
    }
    const fragment = document.createDocumentFragment();
    for (const char of chars.slice(left, chars.length - right)) {
      const glyph = document.createElement('span'); glyph.className = 'glyph'; glyph.setAttribute('aria-hidden', 'true');
      const ink = document.createElement('span'); ink.className = 'ink'; ink.textContent = char;
      ink.style.opacity = '0';
      glyph.append(ink); fragment.append(glyph);
    }
    // Retained prefix and suffix never leave the document or change parents.
    line.insertBefore(fragment, anchor);
    this.dirtyLines.add(line);
  }
  setText(copy, text) { this.batch(copy, () => this.write(copy, text)); }

  marker(copy, visible) {
    const line = copy.querySelector('.sentence');
    if (!line) return;
    const existing = line.querySelector('.candidate-marker');
    if (!visible && existing) { this.activeGlyphs.delete(this.glyphs.get(existing)); this.glyphs.delete(existing); existing.remove(); }
    if (visible && !existing) {
      const marker = document.createElement('span');
      marker.className = 'glyph candidate-marker'; marker.setAttribute('aria-hidden', 'true');
      const ink = document.createElement('span'); ink.className = 'ink'; ink.style.opacity = '0';
      marker.append(ink); line.prepend(marker); this.dirtyLines.add(line);
    }
  }

  enter(state, distance, index = 0, opacity = 0) {
    state.value = [state.target[0] + distance, state.target[1], opacity, 7];
    state.velocity.fill(0);
    state.delay = Math.min(index * this.entryStep, 650);
    state.entering = true;
    this.activeGlyphs.add(state);
  }

  promote(copy) {
    this.marker(copy, false);
    const line = copy.querySelector('.sentence');
    if (!line || this.reduced.matches) return;
    const state = this.lines.get(line);
    this.promotions.set(line, {elapsed: 0, sourceScale: state?.value[2] ??
      parseFloat(getComputedStyle(copy).fontSize) / parseFloat(getComputedStyle(line).fontSize),
      glyphs: [...line.children]});
    const distance = parseFloat(getComputedStyle(line).fontSize) * .72;
    for (const [index, glyph] of [...line.children].entries()) {
      const entry = this.glyphs.get(glyph);
      if (entry) { this.enter(entry, distance, index, (state?.value[3] ?? .44) * entry.value[2]); this.paintGlyph(entry); }
    }
    this.wake();
  }

  flush() {
    // A shorter layout must not clamp scrollTop before its spring can move.
    // The absolute reserve participates only in scroll extent, never flex layout.
    this.reserveScroll(Math.max(this.viewport.scrollTop, this.follow ? this.scroll.value : 0));
    const mutations = [...this.queue.values()]; this.queue.clear();
    for (const mutation of mutations) mutation();
    for (const [line] of this.lines) if (!this.root.contains(line)) { this.lines.delete(line); this.promotions.delete(line); }
    for (const [glyph, state] of this.glyphs) if (!this.root.contains(glyph)) {
      this.activeGlyphs.delete(state); this.glyphs.delete(glyph);
    }
    const layouts = [...this.root.querySelectorAll('.sentence')].map(line => {
      const slot = line.parentElement, copy = slot.parentElement;
      const style = getComputedStyle(copy);
      const base = parseFloat(getComputedStyle(line).fontSize);
      const scale = parseFloat(style.fontSize) / base;
      const promotion = this.promotions.get(line);
      if (promotion && promotion.targetScale == null) {
        promotion.targetScale = scale;
        for (const glyph of promotion.glyphs) {
          glyph.firstChild.style.fontSize = `${promotion.sourceScale / scale}em`;
        }
      }
      const width = slot.getBoundingClientRect().width;
      return {line, slot, copy, style, scale, width};
    });
    // Group reads and writes so a long conversation needs one layout pass per
    // phase rather than forcing a layout for every historical paragraph.
    for (const {line, width, scale} of layouts) line.style.width = `${width / scale}px`;
    const heights = layouts.map(({line, scale}) => line.offsetHeight * scale);
    layouts.forEach(({slot}, i) => { slot.style.height = `${heights[i]}px`; });
    const origin = this.root.getBoundingClientRect();
    const positions = layouts.map(({slot}) => slot.getBoundingClientRect());
    this.contentOffset = origin.top - this.viewport.getBoundingClientRect().top + this.viewport.scrollTop - this.viewport.clientTop;
    for (const [index, {line, copy, style, scale, width}] of layouts.entries()) {
      const rect = positions[index];
      const x = rect.left - origin.left, y = rect.top - origin.top;
      const previous = this.lines.get(line);
      const color = (style.color.match(/[\d.]+/g) || ['198', '200', '205']).slice(0, 3).map(Number);
      const target = [x, y, scale, Number(copy.dataset.alpha ?? 1), ...color];
      let state = previous;
      if (!state) {
        state = {value: [...target], velocity: target.map(() => 0), target};
        this.lines.set(line, state);
      }
      state.width = width;
      state.target = target;
      if (this.promotions.has(line)) {
        // Promotion size belongs to individual glyphs, not the whole paragraph.
        state.value[2] = target[2]; state.velocity[2] = 0;
        state.value[3] = target[3]; state.velocity[3] = 0;
      }
      if (this.snapNext || this.reduced.matches) { state.value = [...target]; state.velocity.fill(0); }
      this.paint(line, state);
    }
    this.measureGlyphs(this.snapNext || this.reduced.matches);
    this.scroll.target = Math.max(0, this.contentOffset + this.root.offsetHeight - this.viewport.clientHeight);
    if (this.snapNext || this.reduced.matches) {
      this.scroll.value = this.follow ? this.scroll.target : this.viewport.scrollTop;
      this.scroll.velocity = 0; this.reserveScroll(this.scroll.value); this.viewport.scrollTop = this.scroll.value;
    }
    this.snapNext = false;
  }

  measureGlyphs(snap = false) {
    // Inline parents include their relatively positioned children in offsetLeft.
    // Clear ink offsets in one batch before measuring, then restore them before
    // paint. Otherwise each new measurement feeds the animation back into itself.
    const measurements = [];
    const pending = [...(snap ? this.lines.keys() : this.dirtyLines)].filter(line => this.lines.has(line));
    for (const line of pending) for (const glyph of line.children) {
      glyph.firstChild.style.left = '0px'; glyph.firstChild.style.top = '0px';
    }
    for (const line of pending) {
      if (!this.lines.has(line)) continue;
      const paragraph = this.lines.get(line);
      const reflow = Math.abs(paragraph.value[2] - paragraph.target[2]) > .0001;
      const distance = parseFloat(getComputedStyle(line).fontSize) * .72;
      let entryIndex = 0;
      for (const glyph of line.children) measurements.push({glyph, distance,
        entryIndex: this.glyphs.has(glyph) ? 0 : entryIndex++,
        reflow: reflow || this.promotions.has(line),
        x: glyph.offsetLeft, y: glyph.offsetTop});
    }
    this.dirtyLines.clear();
    for (const {glyph, distance, entryIndex, x, y, reflow} of measurements) {
      let state = this.glyphs.get(glyph);
      const target = [x, y, 1, 0];
      if (!state) {
        state = {ink: glyph.firstChild, value: [...target],
          velocity: [0, 0, 0, 0], target};
        this.enter(state, distance, entryIndex);
        this.glyphs.set(glyph, state);
      } else if (reflow || y !== state.target[1]) {
        // Reflow is a paragraph operation, never an individual character flight
        // between rows. Paragraph springs supply the vertical/scale motion.
        if (state.entering) state.value[0] += x - state.target[0];
        else { state.value[0] = x; state.velocity[0] = 0; }
      }
      state.value[1] = y; state.velocity[1] = 0;
      state.target = target;
      this.activeGlyphs.add(state);
      // Resizing may snap geometry, never a new/existing entrance. Only the
      // explicit reduced-motion preference disables opacity/blur transitions.
      if (this.reduced.matches) {
        state.value = [...target]; state.velocity.fill(0); state.delay = 0; state.entering = false;
      } else if (snap && !state.entering) {
        state.value[0] = x; state.velocity[0] = 0;
      }
      this.paintGlyph(state);
    }
  }

  // Exact damped mass/spring solution. The small overshoot and retained velocity
  // make surrounding text yield with inertia, including during rapid updates.
  spring(value, velocity, target, seconds, frequency, epsilon, damping = .86) {
    if (Math.abs(value - target) < epsilon && Math.abs(velocity) < epsilon * 8) return [target, 0];
    const displacement = value - target, decayRate = frequency * damping;
    const damped = frequency * Math.sqrt(1 - damping * damping);
    const decay = Math.exp(-decayRate * seconds), sine = Math.sin(damped * seconds), cosine = Math.cos(damped * seconds);
    return [target + decay * (displacement * cosine + (velocity + decayRate * displacement) / damped * sine),
      decay * (velocity * cosine - (decayRate * velocity + frequency * frequency * displacement) / damped * sine)];
  }
  advance(seconds) {
    let moving = false;
    for (const [line, promotion] of this.promotions) {
      if (promotion.targetScale == null) continue;
      promotion.elapsed += seconds * 1000;
      let finished = true;
      const start = promotion.sourceScale / promotion.targetScale;
      for (const [index, glyph] of promotion.glyphs.entries()) {
        const t = this.reduced.matches ? 1 : Math.max(0, Math.min(1, (promotion.elapsed - Math.min(index * this.entryStep, 650)) / 360));
        const ease = t * t * (3 - 2 * t);
        glyph.firstChild.style.fontSize = `${start + (1 - start) * ease}em`;
        finished &&= t === 1;
      }
      this.dirtyLines.add(line);
      // Font growth changes wrapping and the occupied height. Measure it in the
      // next layout batch so neighbouring paragraphs keep their own springs.
      this.batch('promotion-layout', () => {});
      if (finished) {
        for (const glyph of promotion.glyphs) {
          glyph.firstChild.style.fontSize = '';
          const state = this.glyphs.get(glyph);
          if (state) this.paintGlyph(state);
        }
        this.promotions.delete(line);
      } else moving = true;
    }
    const frequencies = [22, 13, 15, 16, 15, 15, 15];
    const tolerances = [.025, .025, .0001, .001, .05, .05, .05];
    for (const [line, state] of this.lines) {
      let changed = false;
      for (let index = 0; index < state.value.length; index++) {
        const before = state.value[index];
        [state.value[index], state.velocity[index]] = this.spring(before, state.velocity[index], state.target[index],
          seconds, frequencies[index], tolerances[index]);
        changed ||= state.value[index] !== before;
        moving ||= state.value[index] !== state.target[index];
      }
      if (changed) this.paint(line, state);
    }
    // Scaling keeps paragraph rows coherent. Only same-row text insertion
    // receives horizontal inertia; glyphs never migrate individually across rows.
    if (this.dirtyLines.size) this.measureGlyphs();
    for (const state of this.activeGlyphs) {
      let elapsed = seconds;
      let active = false;
      if (state.delay > 0) {
        const held = Math.min(elapsed, state.delay / 1000);
        state.delay -= held * 1000; elapsed -= held; active = true;
      }
      let changed = false;
      for (let index = 0; index < state.value.length; index++) {
        const before = state.value[index];
        [state.value[index], state.velocity[index]] = this.spring(before, state.velocity[index], state.target[index],
          elapsed, index < 2 ? 19 : 17, index < 2 ? .025 : .002, index < 2 ? .84 : .98);
        changed ||= state.value[index] !== before;
        active ||= state.value[index] !== state.target[index];
      }
      if (changed) this.paintGlyph(state);
      if (state.value[2] > .995 && state.value[3] < .03) state.entering = false;
      if (active) moving = true;
      else this.activeGlyphs.delete(state);
    }
    if (this.follow && !this.paused) {
      [this.scroll.value, this.scroll.velocity] = this.spring(this.scroll.value, this.scroll.velocity, this.scroll.target, seconds, 15, .1);
      this.reserveScroll(this.scroll.value);
      this.viewport.scrollTop = this.scroll.value;
      moving ||= this.scroll.value !== this.scroll.target;
    } else {
      this.scroll.value = this.viewport.scrollTop; this.scroll.velocity = 0;
      this.reserveScroll(this.scroll.value);
    }
    return moving;
  }
  reserveScroll(position) {
    this.root.style.setProperty('--scroll-floor', `${Math.max(0, this.viewport.clientHeight + position - this.contentOffset)}px`);
  }
  paint(line, state) {
    const [x, y, scale, opacity, red, green, blue] = state.value;
    // Keep the visible paragraph width fixed while its font scales. This also
    // keeps right-aligned text inside the viewport during focus transitions.
    const width = `${state.width / scale}px`;
    if (line.style.width !== width) { line.style.width = width; this.dirtyLines.add(line); }
    line.style.transform = `translate3d(${x}px,${y}px,0) scale(${scale})`;
    line.style.opacity = opacity;
    line.style.color = `rgb(${red},${green},${blue})`;
  }
  paintGlyph(state) {
    const [x, y, opacity, blur] = state.value;
    // Relative inline positioning preserves native CJK punctuation/wrapping.
    // Layout measurements temporarily clear these offsets to avoid feedback.
    state.ink.style.left = `${x - state.target[0]}px`;
    state.ink.style.top = `${y - state.target[1]}px`;
    state.ink.style.opacity = Math.max(0, Math.min(1, opacity));
    state.ink.style.filter = blur > .02 ? `blur(${Math.max(0, blur)}px)` : 'none';
  }
  pause(value) {
    this.paused = value; this.previousTime = 0;
    if (!value) this.wake();
  }
  reset() {
    cancelAnimationFrame(this.frameId); this.frameId = 0; this.previousTime = 0;
    this.glyphs.clear(); this.activeGlyphs.clear(); this.dirtyLines.clear(); this.promotions.clear(); this.lines.clear(); this.queue.clear(); this.tickers.clear();
    this.copyText = new WeakMap(); this.contentOffset = 0; this.root.style.removeProperty('--scroll-floor');
    this.root.replaceChildren(); this.follow = true; this.snapNext = true;
    this.scroll = {value: 0, velocity: 0, target: 0}; this.viewport.scrollTop = 0;
  }
  destroy() { this.reset(); this.resize.disconnect(); document.removeEventListener('visibilitychange', this.visibility); }
}
window.TextMotion = TextMotion;
