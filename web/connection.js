import {Player, Microphone} from './audio.js';

// One instance owns the transport and audio devices for one dial attempt.
export class CallConnection {
  constructor({onMessage, onClose, onError, onDone, onLevel, onStage}) {
    this.onMessage = onMessage;
    this.onClose = onClose;
    this.onError = onError;
    this.onDone = onDone;
    this.onLevel = onLevel;
    this.onStage = onStage;
    this.closed = false;
  }

  async resumeAudio(voice) {
    if (voice) {
      this.context = new AudioContext();
      await this.context.resume();
    }
  }

  async open() {
    if (this.closed) throw new DOMException('通话已结束', 'AbortError');
    const url = new URL('/ws', location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = this.socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    const cancelled = new Promise((_, reject) => { this.rejectOpen = reject; });
    const opened = new Promise(resolve => { socket.onopen = resolve; });
    const fail = () => this.rejectOpen?.(new Error('无法连接到本地服务。'));
    socket.onclose = socket.onerror = fail;
    const timeout = setTimeout(() => this.rejectOpen?.(new Error('连接超时，请确认服务已启动。')), 6000);
    try {
      await Promise.race([opened, cancelled]);
      clearTimeout(timeout);
      if (this.closed) throw new DOMException('通话已结束', 'AbortError');
      if (this.context) {
        this.player = new Player(this.context, event => this.send(event), this.onDone);
        this.mic = new Microphone(this.context, socket, this.onError, this.player.output, this.onLevel);
        await Promise.race([this.mic.open(this.onStage), cancelled]);
      }
      if (this.closed || socket.readyState !== WebSocket.OPEN) throw new Error('连接已结束');
      socket.onmessage = event => { if (!this.closed) this.onMessage(event); };
      socket.onclose = () => { if (!this.closed) this.onClose(); };
      socket.onerror = () => { if (!this.closed) this.onError('连接发生错误，请重新开始。'); };
    } catch (error) {
      await this.close();
      throw error;
    } finally {
      clearTimeout(timeout);
      socket.onopen = null;
      this.rejectOpen = null;
    }
  }

  send(event) {
    if (!this.closed && this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(event));
    }
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    this.rejectOpen?.(new DOMException('通话已结束', 'AbortError'));
    if (this.socket) {
      this.socket.onopen = this.socket.onmessage = this.socket.onclose = this.socket.onerror = null;
      if (this.socket.readyState < WebSocket.CLOSING) this.socket.close();
    }
    this.mic?.close();
    this.player?.close();
    if (this.context && this.context.state !== 'closed') await this.context.close();
  }
}
