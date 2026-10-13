import { createSession, getVerdict } from "./api";
import { opened, socketUrl } from "./websocket";
import { canSendAudio, encodeFloat32LE } from "../audio/pcm";
import processorUrl from "../audio/processor.js?worker&url";

export class AnalysisClient {
  constructor(context, emit) {
    this.context = context;
    this.emit = emit;
    this.stopped = false;
    this.nodes = [];
    this.dropped = 0;
  }
  async start(stream, scenario, referenceStream) {
    this.emit({ analysisState: "Connecting" });
    this.info = await createSession(scenario, !!referenceStream);
    this.emit({ session: this.info });
    const ws = (this.socket = new WebSocket(
      socketUrl(`/v1/stream/${this.info.session_id}`),
    ));
    this.finished = new Promise((resolve) => {
      this.resolveFinished = resolve;
    });
    ws.addEventListener("message", ({ data }) => {
      let message;
      try {
        message = JSON.parse(data);
      } catch {
        this.emit({
          analysisState: "Unavailable",
          analysisError: "Invalid analysis response",
        });
        return;
      }
      this.lastMessage = Date.now();
      if (message.type === "score")
        this.emit({ score: message, analysisState: "Live", analysisError: "" });
      if (message.type === "progress")
        this.emit({
          progress: message,
          analysisError: "",
          analysisState: message.windows_scored ? "Live" : "Collecting speech",
        });
      if (message.type === "verdict") {
        this.verdict = message;
        this.emit({ verdict: message, analysisState: "Complete" });
        this.resolveFinished(message);
      }
      if (message.type === "error")
        this.emit({
          analysisState: "Unavailable",
          analysisError: message.detail,
        });
    });
    ws.addEventListener("close", () => {
      this.detach();
      if (!this.verdict)
        this.emit({
          analysisState: "Unavailable",
          analysisError: "Analysis connection closed. The call can continue.",
        });
      this.resolveFinished(this.verdict);
    });
    await opened(ws);
    if (this.stopped) {
      ws.send("end");
      await this.waitForFinish();
      ws.close();
      return;
    }
    await this.context.audioWorklet.addModule(processorUrl);
    if (this.stopped || ws.readyState !== 1) {
      if (ws.readyState === 1) ws.send("end");
      return;
    }
    const source = this.context.createMediaStreamSource(stream);
    const processor = new AudioWorkletNode(this.context, "caller-pcm", {
      processorOptions: { sampleRate: this.info.sample_rate },
    });
    const silent = this.context.createGain();
    silent.gain.value = 0;
    this.nodes = [source, processor, silent];
    processor.port.onmessage = ({ data }) => {
      if (this.stopped) return;
      const bytes = encodeFloat32LE(data);
      if (canSendAudio(ws, bytes.byteLength, this.info.sample_rate))
        ws.send(bytes);
      else this.emit({ droppedChunks: ++this.dropped });
    };
    source.connect(processor).connect(silent).connect(this.context.destination);
    if (referenceStream) {
      const reference = this.context.createMediaStreamSource(referenceStream);
      const referenceProcessor = new AudioWorkletNode(
        this.context,
        "caller-pcm",
        {
          processorOptions: { sampleRate: this.info.sample_rate },
        },
      );
      referenceProcessor.port.onmessage = ({ data }) => {
        if (
          this.stopped ||
          !canSendAudio(ws, data.byteLength * 2, this.info.sample_rate)
        )
          return;
        const bytes = new Uint8Array(encodeFloat32LE(data));
        ws.send(
          JSON.stringify({
            type: "agent_audio",
            pcm_b64: btoa(String.fromCharCode(...bytes)),
          }),
        );
      };
      reference.connect(referenceProcessor).connect(silent);
      this.nodes.push(reference, referenceProcessor);
    }
    this.lastMessage = Date.now();
    this.monitor = setInterval(() => {
      if (Date.now() - this.lastMessage > 4000)
        this.emit({
          analysisState: "Unavailable",
          analysisError: "No analysis updates received. The call can continue.",
        });
    }, 1000);
    this.emit({ analysisState: "Collecting speech" });
  }
  detach() {
    clearInterval(this.monitor);
    this.nodes.forEach((node) => node.disconnect());
    this.nodes = [];
  }
  async waitForFinish() {
    let timeout;
    await Promise.race([
      this.finished,
      new Promise((resolve) => {
        timeout = setTimeout(resolve, 10000);
      }),
    ]);
    clearTimeout(timeout);
  }
  async stop() {
    if (this.stopped) return;
    this.stopped = true;
    this.detach();
    const ws = this.socket;
    if (!ws) return;
    if (ws.readyState === 1) {
      this.emit({ analysisState: "Finalizing" });
      ws.send("end");
      await this.waitForFinish();
    }
    if (!this.verdict && this.info) {
      try {
        this.verdict = await getVerdict(this.info.session_id);
        this.emit({ verdict: this.verdict, analysisState: "Complete" });
      } catch {
        this.emit({
          analysisState: "Unavailable",
          analysisError:
            "No final verdict is available. Treat the result as elevated risk.",
        });
      }
    }
    ws.close();
  }
}
