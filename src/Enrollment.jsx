import { useState } from "react";
import { enrollAudio, revokeSpeaker } from "./services/api";

export default function Enrollment({ sessionId, disabled = false }) {
  const [speakerId, setSpeakerId] = useState("");
  const [enrolled, setEnrolled] = useState("");
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function enroll(event) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    let context;
    try {
      if (!file || file.size > 10 * 1024 * 1024)
        throw new Error("Choose a reference audio file smaller than 10 MB.");
      context = new AudioContext();
      const decoded = await context.decodeAudioData(await file.arrayBuffer());
      if (decoded.duration < 3 || decoded.duration > 10)
        throw new Error("Use 3–10 seconds of trusted reference speech.");
      const offline = new OfflineAudioContext(
        1,
        Math.ceil(decoded.duration * 16000),
        16000,
      );
      const source = offline.createBufferSource();
      source.buffer = decoded;
      source.connect(offline.destination);
      source.start();
      const buffer = await offline.startRendering();
      const samples = buffer.getChannelData(0);
      const bytes = new Uint8Array(samples.length * 4);
      const view = new DataView(bytes.buffer);
      samples.forEach((value, index) =>
        view.setFloat32(index * 4, value, true),
      );
      let binary = "";
      for (let i = 0; i < bytes.length; i += 8192)
        binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
      const result = await enrollAudio(
        sessionId,
        speakerId.trim(),
        btoa(binary),
      );
      setEnrolled(result.speaker_id);
      setMessage(
        result.demo_mode
          ? "Reference enrolled for this demo. Speaker comparison is simulated."
          : "Reference enrolled for this session.",
      );
    } catch (error) {
      setMessage(error.message);
    } finally {
      await context?.close();
      setBusy(false);
    }
  }
  async function revoke() {
    setBusy(true);
    try {
      await revokeSpeaker(sessionId, enrolled);
      setEnrolled("");
      setMessage("Reference revoked.");
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel panel-body enrollment">
      <h3>Speaker reference</h3>
      <p className="subtle">
        Upload a trusted recording of the known speaker, with their permission.
        Do not enroll the unverified live caller.
      </p>
      <form onSubmit={enroll}>
        <label>
          Speaker ID
          <input
            required
            maxLength={128}
            value={speakerId}
            onChange={(event) => setSpeakerId(event.target.value)}
            disabled={busy || disabled}
          />
        </label>
        <label>
          Reference audio (3–10 seconds)
          <input
            type="file"
            accept="audio/*"
            onChange={(event) => setFile(event.target.files[0])}
            disabled={busy || disabled}
          />
        </label>
        <button
          className="button secondary"
          disabled={
            !sessionId || !speakerId.trim() || !file || busy || disabled
          }
        >
          {busy ? "Working…" : "Enroll reference"}
        </button>
      </form>
      {enrolled && (
        <button
          className="button secondary"
          onClick={revoke}
          disabled={busy || disabled}
        >
          Revoke reference
        </button>
      )}
      {message && (
        <p role="status" className="notice">
          {message}
        </p>
      )}
    </section>
  );
}
