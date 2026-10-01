import { createCall, getCallIce } from './api';
import { opened, socketUrl } from './websocket';
import { AnalysisClient } from './analysis';
import { summarizeConnectionStats, unknownConnection } from './connection-stats';

export class CallClient {
  constructor(audio, emit) {
    this.audio = audio;
    this.emit = emit;
    this.closed = false;
    this.pendingIce = [];
  }
  async start({ invitation, scenario, forceRelay = false, online = false }) {
    this.role = invitation ? 'caller' : 'operator';
    this.scenario = scenario;
    this.online = online;
    this.emit({ callState: 'Requesting microphone', error: '' });
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw new Error('Open the trusted HTTPS address to enable the microphone.');
    }
    this.context = new AudioContext();
    await this.context.resume();
    this.local = await navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    }, video: false });
    if (this.closed) { this.local.getTracks().forEach((track) => track.stop()); return; }
    let room = invitation;
    if (!room) {
      const created = await createCall(forceRelay);
      room = { call_id: created.call_id, token: created.credentials.operator };
      const url = new URL(created.public_origin || window.location.origin);
      url.pathname = `/join/${created.call_id}`;
      url.search = '';
      url.hash = new URLSearchParams({ token: created.credentials.caller }).toString();
      this.emit({ inviteUrl: url.href });
    }
    if (this.closed) return;
    const config = await getCallIce(room.call_id, this.role, room.token);
    if (this.closed) return;
    this.online = config.online_mode ?? this.online;
    const pc = this.pc = new RTCPeerConnection({ iceServers: config.iceServers,
      iceTransportPolicy: config.iceTransportPolicy });
    this.local.getTracks().forEach((track) => pc.addTrack(track, this.local));
    pc.onicecandidate = ({ candidate }) => { if (candidate) this.send('ice', candidate.toJSON()); };
    pc.ontrack = ({ track }) => this.receiveAudio(new MediaStream([track]));
    pc.ondatachannel = ({ channel }) => this.attachControl(channel);
    if (this.role === 'operator') this.attachControl(pc.createDataChannel('call-control'));
    pc.onconnectionstatechange = () => {
      if (this.closed) return;
      if (pc.connectionState === 'connected') {
        clearTimeout(this.connectTimeout);
        clearTimeout(this.disconnectTimeout);
        this.emit({ callState: 'Connected' });
        if (!this.statsTimer) {
          void this.refreshStats();
          this.statsTimer = setInterval(() => { void this.refreshStats(); }, 2000);
        }
      } else if (pc.connectionState === 'failed') {
        this.emit({ error: this.online
          ? 'The audio connection failed. Check internet access and the TURN service, then create a new call.'
          : 'The devices could not maintain a direct connection. Check Wi-Fi client isolation.' });
        void this.end();
      } else if (pc.connectionState === 'disconnected') {
        this.emit({ callState: 'Connection interrupted', connection: unknownConnection() });
        this.disconnectTimeout = setTimeout(() => { void this.end(); }, 10000);
      }
    };
    const ws = this.signal = new WebSocket(socketUrl(`/v1/calls/${room.call_id}/signal`));
    let processing = Promise.resolve();
    ws.onmessage = ({ data }) => {
      processing = processing.then(() => this.handleSignal(JSON.parse(data))).catch((error) => {
        this.emit({ error: error.message });
        void this.end();
      });
    };
    ws.onclose = () => {
      if (this.closed) return;
      this.emit({ signalingLost: true });
      if (pc.connectionState !== 'connected' && pc.connectionState !== 'disconnected') {
        this.emit({ error: 'Pairing connection closed. Create a new call.' });
        void this.end();
      }
    };
    await opened(ws);
    if (this.closed) { ws.close(); return; }
    ws.send(JSON.stringify({ role: this.role, token: room.token }));
    this.emit({ callState: 'Waiting for peer' });
  }
  send(type, data) {
    if (this.signal?.readyState === 1) this.signal.send(JSON.stringify({ type, data }));
  }
  async refreshStats() {
    if (this.closed || this.readingStats || this.pc?.connectionState !== 'connected') return;
    this.readingStats = true;
    try {
      const report = await this.pc.getStats();
      if (this.closed || this.pc.connectionState !== 'connected') return;
      const result = summarizeConnectionStats(report, this.statsCounters);
      this.statsCounters = result.counters;
      this.emit({ connection: result.connection });
    } catch {
      if (!this.closed) this.emit({ connection: unknownConnection() });
    } finally { this.readingStats = false; }
  }
  attachControl(channel) {
    this.control = channel;
    channel.onmessage = ({ data }) => { if (data === 'hangup') void this.end(false); };
  }
  async handleSignal(message) {
    if (this.closed) return;
    const pc = this.pc;
    if (message.type === 'error') throw new Error(message.detail);
    if (message.type === 'hangup') { this.emit({ notice: message.reason }); await this.end(false); return; }
    if (message.type === 'ready') {
      this.emit({ callState: 'Connecting' });
      this.connectTimeout = setTimeout(() => {
        this.emit({ error: this.online
          ? 'Call connection timed out. Check internet access and TURN availability, then create a new invitation.'
          : 'Call connection timed out. Use the same Wi-Fi with client isolation disabled.' });
        void this.end();
      }, this.online ? 45000 : 25000);
      if (this.role === 'operator') {
        await pc.setLocalDescription(await pc.createOffer());
        this.send('offer', pc.localDescription.toJSON());
      }
    }
    if (message.type === 'offer' || message.type === 'answer') {
      await pc.setRemoteDescription(message.data);
      for (const candidate of this.pendingIce) await pc.addIceCandidate(candidate);
      this.pendingIce = [];
      if (message.type === 'offer') {
        await pc.setLocalDescription(await pc.createAnswer());
        this.send('answer', pc.localDescription.toJSON());
      }
    }
    if (message.type === 'ice') {
      if (pc.remoteDescription) await pc.addIceCandidate(message.data);
      else this.pendingIce.push(message.data);
    }
  }
  receiveAudio(stream) {
    if (this.closed) return;
    this.remote = stream;
    this.audio.srcObject = stream;
    void this.play();
    const source = this.levelSource = this.context.createMediaStreamSource(stream);
    const analyser = this.analyser = this.context.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    this.levelTimer = setInterval(() => {
      analyser.getFloatTimeDomainData(samples);
      const rms = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length);
      this.emit({ audioLevel: Math.min(1, rms * 5) });
    }, 100);
    if (this.role === 'operator' && !this.analysis) {
      this.analysis = new AnalysisClient(this.context, this.emit);
      this.analysisStart = this.analysis.start(stream, this.scenario).catch(async (error) => {
        await this.analysis.stop();
        this.emit({ analysisState: 'Unavailable', analysisError: error.message });
      });
    }
  }
  async play() {
    try {
      await this.context.resume();
      await this.audio.play();
      this.emit({ playbackBlocked: false });
    } catch { this.emit({ playbackBlocked: true }); }
  }
  mute(muted) {
    this.local?.getAudioTracks().forEach((track) => { track.enabled = !muted; });
    this.emit({ muted });
  }
  async end(notify = true) {
    if (this.closed) return;
    this.closed = true;
    this.emit({ callState: 'Ending' });
    clearTimeout(this.connectTimeout);
    clearTimeout(this.disconnectTimeout);
    clearInterval(this.levelTimer);
    clearInterval(this.statsTimer);
    if (notify) {
      if (this.control?.readyState === 'open') this.control.send('hangup');
      this.send('hangup');
    }
    this.local?.getTracks().forEach((track) => track.stop());
    this.remote?.getTracks().forEach((track) => track.stop());
    this.pc?.close();
    this.signal?.close();
    this.audio.srcObject = null;
    this.levelSource?.disconnect();
    this.analyser?.disconnect();
    await this.analysis?.stop();
    await this.analysisStart;
    if (this.context && this.context.state !== 'closed') await this.context.close();
    this.emit({ callState: 'Ended', audioLevel: 0, inviteUrl: '', connection: unknownConnection() });
  }
}
