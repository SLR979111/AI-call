/* Real socket events -> the shared text engine. No simulated typing or audio. */
export class LiveConversation {
  constructor(root, viewport) {
    this.engine = new TextLayoutEngine(root, viewport, {interactiveCandidates: false});
    this.reset();
  }
  reset() {
    this.engine.reset(); this.epoch = 0; this.serial = 0; this.partial = null;
    this.replies = new Map(); this.units = new Set(); this.blocked = new Set(); this.finished = new Set(); this.candidates = [];
  }
  cancel(epoch) {
    this.blocked.add(epoch);
    const handle = this.replies.get(epoch);
    if (handle) this.engine.cancel(handle);
    if (epoch === this.epoch) { this.engine.setCandidates([]); this.candidates = []; }
  }
  handle(event) {
    switch (event.type) {
      case 'reply_start':
        if (event.epoch <= this.epoch || this.blocked.has(event.epoch)) return false;
        this.epoch = event.epoch; return true;
      case 'utterance': {
        const key = `${event.epoch}:${event.unit}`;
        if (event.epoch !== this.epoch || this.blocked.has(event.epoch) || this.finished.has(event.epoch) || this.units.has(key)) return false;
        this.units.add(key);
        if (event.audio_only) {
          this.engine.discard(this.partial); this.partial = null;
          this.engine.setCandidates([]); this.candidates = [];
          return true;
        }
        let handle = this.replies.get(event.epoch);
        if (!handle) {
          const candidate = this.candidates.find(item => item.id === event.candidate_id && item.text === event.text);
          handle = candidate && this.engine.promote(candidate.id, `reply-${event.epoch}`);
          if (!handle) handle = this.engine.begin(`reply-${event.epoch}`, 'assistant', {text: event.text});
          this.replies.set(event.epoch, handle);
          this.engine.setCandidates([]); this.candidates = [];
        } else this.engine.append(handle, event.text);
        return true;
      }
      case 'reply_done':
        if (event.epoch !== this.epoch || this.blocked.has(event.epoch) || this.finished.has(event.epoch)) return false;
        this.finished.add(event.epoch); this.engine.finish(this.replies.get(event.epoch)); return true;
      case 'cancel': this.cancel(event.epoch); return true;
      case 'transcript':
        if (!event.text) return false;
        if (!this.partial) this.partial = this.engine.begin(`input-${++this.serial}`, 'user');
        this.engine.revise(this.partial, event.text);
        if (event.final) { this.engine.finish(this.partial); this.partial = null; }
        return true;
      case 'candidate_progress':
        if (event.epoch !== this.epoch || this.blocked.has(event.epoch) || !this.candidates.length) return false;
        this.engine.setCandidateProgress(event.progress); return true;
      case 'candidates':
        if (event.epoch !== this.epoch || this.blocked.has(event.epoch)) return false;
        this.candidates = event.items;
        this.engine.setCandidates(event.items, event.progress); return true;
    }
    return false;
  }
  setInput(input) { this.engine.setInput(input); }
  stop() {
    this.cancel(this.epoch);
    if (this.partial) this.engine.finish(this.partial);
    this.partial = null; this.engine.setInput({active: false});
  }
  destroy() { this.engine.destroy(); }
}
