import { useEffect, useRef, useState } from "react";
import "./App.css";
import { createSession, getVerdict, getAudit } from "./services/api";
import { connectToStream } from "./services/websocket";

function App() {
  const [activePage, setActivePage] = useState("Dashboard");
  const [sessionId, setSessionId] = useState(null);
  const [socket, setSocket] = useState(null);
  const [micStream, setMicStream] = useState(null);
  const [micActive, setMicActive] = useState(false);
  const audioContextRef = useRef(null);
  const processorRef = useRef(null);
  const [startingCall, setStartingCall] = useState(false);
  const [backendError, setBackendError] = useState("");
  const [liveData, setLiveData] = useState(null);
  const [verdict, setVerdict] = useState(null);
  const [audit, setAudit] = useState(null);
  const [verificationStarted, setVerificationStarted] = useState(false);

  const pages = ["Dashboard", "Live Call", "Verdict", "Audit"];

  const renderPage = () => {
    if (activePage === "Live Call") {
      return (
        <div className="page">
          <div className="page-header">
            <div>
              <h1>Live Call</h1>
              <p>Real-time voice integrity analysis</p>
            </div>
            <div className="status-pill">
              <span className="status-dot"></span>
              Analysis Active
            </div>
          </div>

          <div className="call-grid">
            <div className={`card risk-card ${liveData?.risk?.toLowerCase() || ""}`}>
              <div className="card-label">CURRENT RISK</div>
               <div className="big-risk">
      {liveData?.risk || "WAITING"}
    </div>
               <div className="risk-safe">
      {liveData?.risk === "GREEN"
        ? "LOW RISK"
        : liveData?.risk === "AMBER"
        ? "ELEVATED RISK"
        : liveData?.risk === "RED"
        ? "HIGH RISK"
        : "ANALYZING"}
    </div>

              <div className="risk-bar">
  <div
    className={`risk-fill ${liveData?.risk?.toLowerCase() || ""}`}
  ></div>
</div>

              <div className="risk-scale">
                <span>0</span>
                <span>40</span>
                <span>75</span>
                <span>100</span>
              </div>
            </div>

            <div className="card">
              <div className="card-label">VOICE ANALYSIS</div>

              <div className="signal-row">
                <span>Synthetic Speech</span>
                <strong>
  {liveData
    ? `${Math.round(liveData.spoof_probability * 100)}%`
    : "--"}
</strong>
              </div>

              <div className="signal-row">
                <span>Speaker Match</span>
                <strong className="green-text">
  {liveData?.speaker_status || "—"}
</strong>
              </div>

              <div className="signal-row">
                <span>Liveness</span>
                <strong className="green-text">
  {liveData?.liveness_score != null
    ? `${Math.round(liveData.liveness_score * 100)}%`
    : "—"}
</strong>
              </div>

              <div className="signal-row">
                <span>Inference</span>
                <strong>
  {liveData?.inference_ms != null
    ? `${Math.round(liveData.inference_ms)} ms`
    : "—"}
</strong>
              </div>
            </div>
          </div>

          <div className="card waveform-card">
            <div className="card-header">
              <div>
                <div className="card-label">LIVE AUDIO</div>
                <h2>Caller audio stream</h2>
              </div>
              <span className="live-label">● LIVE</span>
            </div>

            <div className="waveform">
              {Array.from({ length: 60 }).map((_, index) => (
                <span
                  key={index}
                  style={{
                    height: `${20 + ((index * 37) % 65)}%`,
                  }}
                ></span>
              ))}
            </div>
          </div>

          <div className="action-card">
  <div>
    <div className="action-title">Recommended Action</div>

    <div className="action-text">
      {!liveData
        ? "Waiting for voice analysis..."
        : liveData.decision?.action === "PROCEED"
        ? "Continue call. No additional verification required."
        : liveData.decision?.action === "CHALLENGE"
        ? verificationStarted
          ? "Verification in progress. Confirm the caller before continuing."
          : "Elevated voice-cloning risk detected. Additional verification is required."
        : liveData.decision?.action === "GATE"
        ? "Sensitive action should be blocked until verification is completed."
        : "Analyzing call risk..."}
    </div>

    {liveData?.decision?.action === "CHALLENGE" && !verificationStarted && (
      <button
        className={`verify-btn ${
  liveData?.decision?.action === "GATE" ? "gate-btn" : ""
}`}
        onClick={() => setVerificationStarted(true)}
      >
        Start Verification
      </button>
    )}

    {liveData?.decision?.action === "GATE" && !verificationStarted && (
      <button
        className={`verify-btn ${
  liveData?.decision?.action === "GATE" ? "gate-btn" : ""
}`}
        onClick={() => setVerificationStarted(true)}
      >
        Verify Before Action
      </button>
    )}
  </div>

  <div
    className={`action-badge ${
      liveData?.decision?.action?.toLowerCase() || ""
    }`}
  >
    {liveData?.decision?.action || "WAITING"}
  </div>
</div>
          <button
            className="end-call-btn"
            onClick={async () => {
  if (processorRef.current) {
    processorRef.current.disconnect();
    processorRef.current = null;
  }

  if (audioContextRef.current) {
    await audioContextRef.current.close();
    audioContextRef.current = null;
  }

  if (micStream) {
    micStream.getTracks().forEach((track) => track.stop());
  }

  setMicActive(false);

  if (socket) {
    socket.send("end");
  }
}}
          >
            End Call
          </button>

        </div>
      );
    }

    if (activePage === "Verdict") {
  const final = verdict?.payload;

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <div className="eyebrow">FINAL RESULT</div>
          <h1>Call Verdict</h1>
          <p>Final AI assessment for the completed call.</p>
        </div>
      </div>

      <div className="verdict-grid">

        <div className="card verdict-main-card">
          <div className="card-label">FINAL DECISION</div>

          <div className="verdict-action">
            {final?.action || "WAITING"}
          </div>

          <div className="verdict-risk">
            Risk Level: <strong>{final?.risk || "—"}</strong>
          </div>
        </div>

        <div className="card">
          <div className="card-label">VOICE ANALYSIS</div>

          <div className="signal-row">
            <span>Synthetic Speech</span>
            <strong>
              {final?.spoof_probability != null
                ? `${Math.round(final.spoof_probability * 100)}%`
                : "—"}
            </strong>
          </div>

          <div className="signal-row">
            <span>Speaker Status</span>
            <strong>
              {final?.speaker_status || "—"}
            </strong>
          </div>

          <div className="signal-row">
            <span>Liveness</span>
            <strong>
              {final?.liveness_score != null
                ? `${Math.round(final.liveness_score * 100)}%`
                : "—"}
            </strong>
          </div>

          <div className="signal-row">
            <span>Speech Analyzed</span>
            <strong>
              {final?.speech_seconds != null
                ? `${final.speech_seconds.toFixed(1)} s`
                : "—"}
            </strong>
          </div>

          <div className="signal-row">
            <span>Windows Scored</span>
            <strong>
              {final?.windows_scored ?? "—"}
            </strong>
          </div>
        </div>

        <div className="card">
          <div className="card-label">MODEL & POLICY</div>

          <div className="signal-row">
            <span>Model Version</span>
            <strong>
              {final?.model_version || "—"}
            </strong>
          </div>

          <div className="signal-row">
            <span>Policy Version</span>
            <strong>
              {final?.policy_version || "—"}
            </strong>
          </div>

          <div className="signal-row">
            <span>Session ID</span>
            <strong className="session-id">
              {final?.session_id || sessionId || "—"}
            </strong>
          </div>

          <div className="signal-row">
            <span>Signature</span>
            <strong>
              {verdict?.algorithm || "—"}
            </strong>
          </div>
        </div>

      </div>
    </div>
  );
}
    if (activePage === "Audit") {
  if (!audit) {
    getAudit()
      .then((data) => {
        console.log("AUDIT DATA:", data);
        setAudit(data);
      })
      .catch((error) => {
        console.error("Failed to load audit:", error);
      });
  }

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Audit Trail</h1>
          <p>Signed and tamper-evident activity records</p>
        </div>

        <div className="status-pill">
          <span className="status-dot"></span>
          {audit?.chain_valid ? "Chain Valid" : "Chain Invalid"}
        </div>
      </div>

      <div className="card audit-summary">
        <div>
          <div className="card-label">AUDIT STATUS</div>
          <h2>
            {audit?.chain_valid
              ? "Integrity verified"
              : "Integrity check failed"}
          </h2>
          <p>
            {audit?.reason || "Checking audit chain integrity..."}
          </p>
        </div>

        <div className="audit-check">
          {audit?.chain_valid ? "✓" : "!"}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <div>
            <div className="card-label">RECENT EVENTS</div>
            <h2>Activity</h2>
          </div>

          <div className="card-label">
            {audit?.count ?? 0} ENTRIES
          </div>
        </div>

        {!audit ? (
          <p>Loading audit events...</p>
        ) : audit.entries?.length === 0 ? (
          <p>No audit events recorded yet.</p>
        ) : (
          audit.entries.slice(-5).reverse().map((entry, index) => (
            <div className="audit-item" key={index}>
              <div className="audit-icon">✓</div>

              <div>
                <strong>
                  {entry.event_type ||
                    entry.event ||
                    entry.type ||
                    "Audit event"}
                </strong>

                <p>
                  {entry.risk
                    ? `Risk ${Math.round(entry.risk * 100)}`
                    : entry.action ||
                      entry.message ||
                      entry.description ||
                      "Recorded activity"}
                </p>
              </div>

              <span>
                {entry.timestamp
                  ? new Date(entry.timestamp).toLocaleTimeString()
                  : "—"}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

    return (
      <div className="page">
        <div className="page-header">
          <div>
            <h1>Dashboard</h1>
            <p>Voice Integrity monitoring overview</p>
          </div>

          <button
  className="primary-button"
  onClick={async () => {
    console.log("1. BUTTON CLICKED");

    try {
      console.log("2. CALLING BACKEND");

      setStartingCall(true);
      setBackendError("");

      const session = await createSession();

console.log("3. SESSION CREATED:", session);

setSessionId(session.session_id);

const ws = connectToStream(
  session.session_id,
  (data) => {
  console.log("LIVE DATA:", data);

  if (data.type === "verdict") {
    console.log("FINAL VERDICT:", data);
    setVerdict(data);
    setActivePage("Verdict");
    return;
  }

  setLiveData(data);
},
  (error) => {
    console.error("STREAM ERROR:", error);
  },
  () => {
    console.log("STREAM CLOSED");
  }
);

setSocket(ws);

try {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: true,
  });

  setMicStream(stream);
  setMicActive(true);

  console.log("Microphone connected");

  const audioContext = new AudioContext({
  sampleRate: 16000,
});
audioContextRef.current = audioContext;

const source = audioContext.createMediaStreamSource(stream);
const processor = audioContext.createScriptProcessor(4096, 1, 1);

processorRef.current = processor;

processor.onaudioprocess = (event) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    return;
  }

  const input = event.inputBuffer.getChannelData(0);

  const output = new Int16Array(input.length);

  for (let i = 0; i < input.length; i++) {
    const sample = Math.max(-1, Math.min(1, input[i]));
    output[i] = sample < 0 ? sample * 32768 : sample * 32767;
  }

  console.log("Sending audio:", output.length);
  ws.send(output.buffer);
};

source.connect(processor);
processor.connect(audioContext.destination);

console.log("Audio streaming started");
} catch (error) {
  console.error("Microphone error:", error);
  setBackendError("Microphone permission was denied.");
}

setActivePage("Live Call");
    } catch (error) {
      console.error("BACKEND ERROR:", error);
      setBackendError(error.message);
    } finally {
      setStartingCall(false);
    }
  }}
>
  <span>＋</span>
  {startingCall ? "Starting..." : "Start New Call"}
</button>

{backendError && (
  <div className="backend-error">
    {backendError}
  </div>
)}

        </div>

        <div className="overview-grid">
          <div className="card main-risk">
            <div className="card-label">CURRENT RISK</div>

            <div className="risk-number-row">
              <span className="dashboard-risk">32</span>
              <span className="risk-out-of">/ 100</span>
            </div>

            <div className="risk-safe">LOW RISK</div>

            <div className="risk-bar">
              <div className="risk-fill"></div>
            </div>

            <div className="risk-description">
              Current call is within the safe operating range.
            </div>
          </div>

          <div className="card">
            <div className="card-label">SPOOF PROBABILITY</div>
            <div className="metric-number">18%</div>
            <div className="metric-caption">Synthetic speech indicators</div>
          </div>

          <div className="card">
            <div className="card-label">SPEAKER</div>
            <div className="metric-number green-text">Verified</div>
            <div className="metric-caption">Identity confidence: 91%</div>
          </div>

          <div className="card">
            <div className="card-label">LIVENESS</div>
            <div className="metric-number">91%</div>
            <div className="metric-caption">Conversational liveness</div>
          </div>
        </div>

        <div className="dashboard-columns">
          <div className="card">
            <div className="card-header">
              <div>
                <div className="card-label">LIVE ACTIVITY</div>
                <h2>Recent Analysis</h2>
              </div>
              <span className="live-label">● LIVE</span>
            </div>

            <div className="activity-list">
              <div className="activity-item">
                <span className="activity-dot green"></span>
                <div>
                  <strong>Voice analysis running</strong>
                  <p>Risk score updated to 32</p>
                </div>
                <span>Now</span>
              </div>

              <div className="activity-item">
                <span className="activity-dot green"></span>
                <div>
                  <strong>Speaker verified</strong>
                  <p>Identity confidence 91%</p>
                </div>
                <span>1m</span>
              </div>

              <div className="activity-item">
                <span className="activity-dot"></span>
                <div>
                  <strong>Call session created</strong>
                  <p>Monitoring initialized</p>
                </div>
                <span>2m</span>
              </div>
            </div>
          </div>

          <div className="card policy-card">
            <div className="card-label">RISK POLICY</div>
            <h2>Response thresholds</h2>

            <div className="policy-row">
              <span className="policy-indicator green-bg"></span>
              <div>
                <strong>0–40</strong>
                <p>Proceed</p>
              </div>
            </div>

            <div className="policy-row">
              <span className="policy-indicator amber-bg"></span>
              <div>
                <strong>40–75</strong>
                <p>Challenge</p>
              </div>
            </div>

            <div className="policy-row">
              <span className="policy-indicator red-bg"></span>
              <div>
                <strong>75–100</strong>
                <p>Gate sensitive action</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">V</div>
          <div>
            <div className="brand-name">VOICE</div>
            <div className="brand-subtitle">INTEGRITY</div>
          </div>
        </div>

        <div className="sidebar-section">
          <div className="sidebar-label">MONITORING</div>

          {pages.map((page) => (
            <button
              key={page}
              className={
                activePage === page ? "nav-item active" : "nav-item"
              }
              onClick={() => setActivePage(page)}
            >
              <span className="nav-icon">
                {page === "Dashboard" && "⌂"}
                {page === "Live Call" && "◉"}
                {page === "Verdict" && "✓"}
                {page === "Audit" && "≡"}
              </span>
              {page}
            </button>
          ))}
        </div>

        <div className="sidebar-bottom">
          <div className="system-status">
            <span className="status-dot"></span>
            <div>
              <strong>System Online</strong>
              <span>Backend connected</span>
            </div>
          </div>

          <div className="model-info">
            <span>MODEL</span>
            <strong>XLS-R + AASIST</strong>
          </div>
        </div>
      </aside>

      <main className="main-content">
        {renderPage()}
      </main>
    </div>
  );
}

export default App;

