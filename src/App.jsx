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
  const analyserRef = useRef(null);
  const animationRef = useRef(null);
  const [startingCall, setStartingCall] = useState(false);
  const [backendError, setBackendError] = useState("");
  const [liveData, setLiveData] = useState(null);
  const [verdict, setVerdict] = useState(null);
  const [audit, setAudit] = useState(null);
  const [auditError, setAuditError] = useState("");
  const [verificationStarted, setVerificationStarted] = useState(false);
  const [showGateWarning, setShowGateWarning] = useState(false);
  const previousActionRef = useRef(null);
  const [waveformData, setWaveformData] = useState(
  Array(60).fill(20)
);

useEffect(() => {
  if (activePage !== "Audit") return;

  setAudit(null);
  setAuditError("");

  getAudit()
    .then((data) => {
      console.log("AUDIT DATA:", data);
      setAudit(data);
    })
    .catch((error) => {
      console.error("AUDIT ERROR:", error);
      setAuditError(error.message || "Unable to load audit trail.");
    });
}, [activePage]);

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
            <div
  className={`card risk-card ${
    liveData?.speech_seconds > 0
      ? liveData?.risk?.toLowerCase() || ""
      : ""
  }`}
>
  <div className="card-label">CURRENT RISK</div>

  <div
  className={`big-risk ${
    liveData?.risk?.toLowerCase() || "waiting"
  }`}
>
  {liveData?.risk || "WAITING"}
</div>

  <div className={`risk-safe ${liveData?.risk?.toLowerCase() || "waiting"}`}>
    {liveData?.speech_seconds > 0
      ? liveData?.risk === "GREEN"
        ? "LOW RISK"
        : liveData?.risk === "AMBER"
        ? "ELEVATED RISK"
        : liveData?.risk === "RED"
        ? "HIGH RISK"
        : "ANALYZING"
      : "WAITING FOR VOICE ANALYSIS"}
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
  {waveformData.map((height, index) => (
    <span
      key={index}
      style={{
        height: `${height}%`,
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
    processorRef.current.onaudioprocess = null;
    processorRef.current.disconnect();
    processorRef.current = null;
  }

  if (audioContextRef.current) {
    await audioContextRef.current.close();
    audioContextRef.current = null;
  }

  if (micStream) {
    micStream.getTracks().forEach((track) => track.stop());
    setMicStream(null);
  }

  if (animationRef.current) {
  cancelAnimationFrame(animationRef.current);
  animationRef.current = null;
}

if (analyserRef.current) {
  analyserRef.current.disconnect();
  analyserRef.current = null;
}
  setMicActive(false);

   if (socket && socket.readyState === WebSocket.OPEN) {
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

  const risk = final?.risk || "UNKNOWN";
  const action = final?.action || "—";

  const riskClass = risk.toLowerCase();

  const riskMessage =
    risk === "GREEN"
      ? "The call shows a low level of synthetic-speech risk."
      : risk === "AMBER"
      ? "The call shows elevated synthetic-speech risk and may require additional verification."
      : risk === "RED"
      ? "The call shows a high level of synthetic-speech risk."
      : "Risk assessment is unavailable.";

  const actionMessage =
    action === "PROCEED"
      ? "Continue the call normally."
      : action === "CHALLENGE"
      ? "Additional verification is recommended before sensitive actions."
      : action === "GATE"
      ? "Sensitive actions should not proceed without independent verification."
      : "No action recommendation available.";

  return (
    <div className="page verdict-page">

      <div className="verdict-header">
        <div>
          <div className="section-label">FINAL RESULT</div>
          <h1>Call Verdict</h1>
          <p>Final AI assessment for the completed call.</p>
        </div>

        <button
          className="verdict-back-btn"
          onClick={() => setActivePage("Dashboard")}
        >
          ← Dashboard
        </button>
      </div>

      {/* HERO VERDICT */}
      <div className={`verdict-hero ${riskClass}`}>

        <div className="verdict-hero-left">

          <div className="verdict-status-row">
            <span className={`verdict-status-dot ${riskClass}`}></span>
            <span>FINAL ASSESSMENT</span>
          </div>

          <div className="verdict-main-row">

            <div className={`risk-circle ${riskClass}`}>
              <div className="risk-circle-inner">
                <span>RISK</span>
                <strong>{risk}</strong>
              </div>
            </div>

            <div className="verdict-main-text">
              <div className="verdict-eyebrow">FINAL DECISION</div>

              <h2>{action}</h2>

              <p>{actionMessage}</p>

              <div className={`risk-description ${riskClass}`}>
                {riskMessage}
              </div>
            </div>

          </div>
        </div>

        <div className="verdict-hero-right">

          <div className="hero-stat">
            <span>Synthetic Speech</span>
            <strong>
              {final?.spoof_probability != null
                ? `${Math.round(final.spoof_probability * 100)}%`
                : "—"}
            </strong>
          </div>

          <div className="hero-stat">
            <span>Speaker Status</span>
            <strong>{final?.speaker_status || "—"}</strong>
          </div>

          <div className="hero-stat">
            <span>Liveness</span>
            <strong>
              {final?.liveness_score != null
                ? `${Math.round(final.liveness_score * 100)}%`
                : "—"}
            </strong>
          </div>

        </div>
      </div>

      {/* ANALYSIS */}
      <div className="verdict-section-title">
        <div>
          <div className="section-label">VOICE ANALYSIS</div>
          <h2>Detection Summary</h2>
        </div>
      </div>

      <div className="verdict-metrics-grid">

        <div className="verdict-metric-card">
          <div className="metric-icon">◈</div>
          <div>
            <span>Synthetic Speech</span>
            <strong>
              {final?.spoof_probability != null
                ? `${Math.round(final.spoof_probability * 100)}%`
                : "—"}
            </strong>
          </div>
        </div>

        <div className="verdict-metric-card">
          <div className="metric-icon">◎</div>
          <div>
            <span>Speaker Status</span>
            <strong>{final?.speaker_status || "—"}</strong>
          </div>
        </div>

        <div className="verdict-metric-card">
          <div className="metric-icon">◉</div>
          <div>
            <span>Liveness</span>
            <strong>
              {final?.liveness_score != null
                ? `${Math.round(final.liveness_score * 100)}%`
                : "—"}
            </strong>
          </div>
        </div>

        <div className="verdict-metric-card">
          <div className="metric-icon">▣</div>
          <div>
            <span>Speech Analyzed</span>
            <strong>
              {final?.speech_seconds != null
                ? `${Number(final.speech_seconds).toFixed(1)} s`
                : "—"}
            </strong>
          </div>
        </div>

      </div>

      {/* PROCESSING SUMMARY */}
      <div className="verdict-analysis-card">

        <div className="analysis-card-header">
          <div>
            <div className="section-label">ANALYSIS COVERAGE</div>
            <h2>Processing Summary</h2>
          </div>

          <div className="windows-badge">
            {final?.windows_scored ?? 0} windows scored
          </div>
        </div>

        <div className="coverage-bar">
          <div
            className={`coverage-fill ${riskClass}`}
            style={{
              width: final?.spoof_probability != null
                ? `${Math.max(8, Math.min(100, final.spoof_probability * 100))}%`
                : "0%",
            }}
          ></div>
        </div>

        <div className="coverage-scale">
          <span>LOW RISK</span>
          <span>ANALYSIS COMPLETE</span>
          <span>HIGH RISK</span>
        </div>

      </div>

      {/* MODEL + SECURITY */}
      <div className="verdict-two-column">

        <div className="verdict-detail-card">
          <div className="section-label">MODEL & POLICY</div>
          <h2>Detection Configuration</h2>

          <div className="detail-row">
            <span>Model Version</span>
            <strong>{final?.model_version || "—"}</strong>
          </div>

          <div className="detail-row">
            <span>Policy Version</span>
            <strong>{final?.policy_version || "—"}</strong>
          </div>

          <div className="detail-row">
            <span>Session ID</span>
            <strong className="session-value">
              {final?.session_id || "—"}
            </strong>
          </div>
        </div>

        <div className="verdict-detail-card security-card">

          <div className="section-label">SECURITY</div>
          <h2>Signed Verdict</h2>

          <div className="signature-status">
            <div className="signature-check">✓</div>

            <div>
              <strong>
                {verdict?.signature
                  ? "Cryptographic signature attached"
                  : "Signature unavailable"}
              </strong>

              <span>
                {verdict?.algorithm || "—"} verification
              </span>
            </div>
          </div>

          <div className="signature-key">
            <span>Key ID</span>
            <strong>{verdict?.key_id || "—"}</strong>
          </div>

        </div>

      </div>

      {/* FOOTER */}
      <div className="verdict-footer-card">

        <div>
          <div className="section-label">SESSION COMPLETE</div>
          <h3>Voice analysis has finished</h3>
          <p>
            The final assessment has been generated and the session is now
            complete.
          </p>
        </div>

        <button
          className="verdict-dashboard-btn"
          onClick={() => setActivePage("Dashboard")}
        >
          Return to Dashboard
        </button>

      </div>

    </div>
  );
}
    if (activePage === "Audit") {
  const entries = audit?.entries || [];

  return (
    <div className="page audit-page">

      <div className="audit-header">
        <div>
          <div className="section-label">SECURITY LOG</div>
          <h1>Audit Trail</h1>
          <p>Signed and tamper-evident activity records</p>
        </div>

        <div
          className={`chain-status ${
  !audit
    ? "loading"
    : audit.chain_valid
    ? "valid"
    : "invalid"
}`}
        >
          <span className="chain-dot"></span>
          {!audit
  ? "Loading"
  : audit.chain_valid
  ? "Chain Valid"
  : "Chain Invalid"}
        </div>
      </div>

      {auditError ? (
  <div className="audit-loading-card">
    <div className="audit-empty-icon">!</div>
    <h3>Unable to load audit trail</h3>
    <p>{auditError}</p>
  </div>
) : !audit ? (
  <div className="audit-loading-card">
    <div className="audit-loading-spinner"></div>
    <h3>Loading audit trail</h3>
    <p>Retrieving security records...</p>
  </div>
) : (
        <>
          <div className="audit-summary-grid">

            <div className="audit-summary-card">
              <div className="audit-summary-icon green">✓</div>
              <div>
                <span>CHAIN INTEGRITY</span>
                <strong>
                  {audit.chain_valid ? "Verified" : "Invalid"}
                </strong>
                <small>
                  {audit.reason || "Integrity status confirmed"}
                </small>
              </div>
            </div>

            <div className="audit-summary-card">
              <div className="audit-summary-icon blue">#</div>
              <div>
                <span>TOTAL RECORDS</span>
                <strong>{entries.length}</strong>
                <small>Signed audit records</small>
              </div>
            </div>

            <div className="audit-summary-card">
              <div className="audit-summary-icon purple">⌁</div>
              <div>
                <span>CHAIN STATUS</span>
                <strong>
                  {audit.chain_valid ? "INTACT" : "CHECK REQUIRED"}
                </strong>
                <small>Hash-linked record chain</small>
              </div>
            </div>

          </div>

          <div className="audit-events-card">

            <div className="audit-events-header">
              <div>
                <div className="section-label">RECENT RECORDS</div>
                <h2>Activity Log</h2>
              </div>

              <div className="audit-count">
                {entries.length} RECORDS
              </div>
            </div>

            <div className="audit-timeline">

              {entries.length === 0 ? (
                <div className="audit-empty">
                  <div className="audit-empty-icon">—</div>
                  <h3>No audit records</h3>
                  <p>No signed activity has been recorded yet.</p>
                </div>
              ) : (
                entries
                  .slice()
                  .reverse()
                  .slice(0, 5)
                  .map((entry, index) => {

                    const signedPayload = entry.payload || {};
const verdictPayload = signedPayload.payload || {};

const risk = verdictPayload.risk || "—";

const action =
  verdictPayload.decision?.action ||
  verdictPayload.action ||
  "VERDICT";

const sessionId = verdictPayload.session_id || "—";

const timestamp = entry.ts_ms
  ? new Date(entry.ts_ms).toLocaleString()
  : "Unknown time";
                    return (
                      <div
                        className="audit-event-row"
                        key={entry.entry_hash || index}
                      >

                        <div className="audit-event-marker">
                          <span>✓</span>
                        </div>

                        <div className="audit-event-content">

                          <div className="audit-event-top">
                            <strong>
  {action === "VERDICT"
    ? "Signed Verdict"
    : `${action} Decision`}
</strong>

                            <span className="audit-verified-tag">
                              SIGNED
                            </span>
                          </div>

                          <div className="audit-event-bottom">
  <span>{timestamp}</span>
  <span>•</span>
  <span>Risk: {risk}</span>
  <span>•</span>
  <span>
    Session: {sessionId === "—" ? "—" : sessionId.slice(0, 8) + "..."}
  </span>
</div>

                        </div>

                        <div className="audit-event-index">
                          #{entry.seq ?? index}
                        </div>

                      </div>
                    );
                  })
              )}

            </div>
          </div>

          <div className="audit-security-card">

            <div className="audit-security-icon">✓</div>

            <div className="audit-security-content">
              <div className="section-label">INTEGRITY PROTECTION</div>
              <h2>Audit chain is tamper-evident</h2>
              <p>
                Each record is linked to the previous record through a
                cryptographic hash chain and contains a signed verdict payload.
              </p>
            </div>

            <div className="audit-security-status">
              <span></span>
              {audit.chain_valid
                ? "INTEGRITY VERIFIED"
                : "REVIEW REQUIRED"}
            </div>

          </div>
        </>
      )}
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

      setLiveData(null);
      setVerdict(null);
      setShowGateWarning(false);
      previousActionRef.current = null;

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

  const action = data.decision?.action;

  if (action === "GATE" && previousActionRef.current !== "GATE") {
    setShowGateWarning(true);
  }

  previousActionRef.current = action;
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

const analyser = audioContext.createAnalyser();
analyser.fftSize = 128;
analyser.smoothingTimeConstant = 0.75;

analyserRef.current = analyser;

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

  ws.send(output.buffer);
};

source.connect(analyser);
analyser.connect(processor);
processor.connect(audioContext.destination);

const dataArray = new Uint8Array(analyser.frequencyBinCount);

const updateWaveform = () => {
  analyser.getByteFrequencyData(dataArray);

  const bars = Array.from({ length: 60 }, (_, index) => {
    const sourceIndex = Math.floor(
      (index / 60) * dataArray.length
    );

    const value = dataArray[sourceIndex] || 0;

    return Math.max(8, Math.min(100, value * 0.8));
  });

  setWaveformData(bars);

  animationRef.current = requestAnimationFrame(updateWaveform);
};

updateWaveform();

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
  <div
    className={`card main-risk ${
      liveData?.risk?.toLowerCase() || ""
    }`}
  >
    <div className="card-label">CURRENT RISK</div>

    <div className="risk-number-row">
      <span className="dashboard-risk">
        {liveData
          ? liveData.risk_score != null
            ? Math.round(liveData.risk_score)
            : Math.round((liveData.spoof_probability || 0) * 100)
          : "—"}
      </span>

      <span className="risk-out-of">/ 100</span>
    </div>

    <div className="risk-safe">
      {!liveData
        ? "NO ACTIVE CALL"
        : liveData.risk === "GREEN"
        ? "LOW RISK"
        : liveData.risk === "AMBER"
        ? "ELEVATED RISK"
        : liveData.risk === "RED"
        ? "HIGH RISK"
        : "ANALYZING"}
    </div>

    <div className="risk-bar">
      <div
        className={`risk-fill ${
          liveData?.risk?.toLowerCase() || ""
        }`}
        style={{
          width: `${
            liveData
              ? Math.min(
                  100,
                  liveData.risk_score != null
                    ? liveData.risk_score
                    : (liveData.spoof_probability || 0) * 100
                )
              : 0
          }%`,
        }}
      ></div>
    </div>

    <div className="risk-description">
      {!liveData
        ? "Start a call to begin voice integrity monitoring."
        : liveData.risk === "GREEN"
        ? "Current call is within the safe operating range."
        : liveData.risk === "AMBER"
        ? "Additional verification is recommended."
        : liveData.risk === "RED"
        ? "Sensitive actions should be gated until verification."
        : "Voice analysis is currently in progress."}
    </div>
  </div>

  <div className="card">
    <div className="card-label">SPOOF PROBABILITY</div>

    <div className="metric-number">
      {liveData?.spoof_probability != null
        ? `${Math.round(liveData.spoof_probability * 100)}%`
        : "—"}
    </div>

    <div className="metric-caption">
      {liveData
        ? "Synthetic speech indicators"
        : "Waiting for voice analysis"}
    </div>
  </div>

  <div className="card">
    <div className="card-label">SPEAKER</div>

    <div
      className={`metric-number ${
        liveData?.speaker_status === "VERIFIED"
          ? "green-text"
          : ""
      }`}
    >
      {liveData?.speaker_status || "—"}
    </div>

    <div className="metric-caption">
      {liveData?.speaker_similarity != null
        ? `Identity confidence: ${Math.round(
            liveData.speaker_similarity * 100
          )}%`
        : liveData
        ? "Speaker not enrolled"
        : "Waiting for speaker verification"}
    </div>
  </div>

  <div className="card">
    <div className="card-label">LIVENESS</div>

    <div className="metric-number">
      {liveData?.liveness_score != null
        ? `${Math.round(liveData.liveness_score * 100)}%`
        : "—"}
    </div>

    <div className="metric-caption">
      {liveData
        ? "Conversational liveness"
        : "Waiting for voice analysis"}
    </div>
  </div>
</div>

        <div className="dashboard-columns">
          <div className="card">
  <div className="card-header">
    <div>
      <div className="card-label">LIVE ACTIVITY</div>
      <h2>Recent Analysis</h2>
    </div>

    <span className="live-label">
      {liveData ? "● LIVE" : "○ IDLE"}
    </span>
  </div>

  <div className="activity-list">
    {!liveData ? (
      <div className="activity-item">
        <span className="activity-dot"></span>

        <div>
          <strong>No active call</strong>
          <p>Start a call to begin monitoring</p>
        </div>

        <span>—</span>
      </div>
    ) : (
      <>
        <div className="activity-item">
          <span
            className={`activity-dot ${
              liveData.risk === "RED"
                ? "red"
                : liveData.risk === "AMBER"
                ? "amber"
                : "green"
            }`}
          ></span>

          <div>
            <strong>Voice analysis running</strong>
            <p>
              Risk: {liveData.risk || "ANALYZING"}
              {liveData.spoof_probability != null
                ? ` · Spoof ${Math.round(
                    liveData.spoof_probability * 100
                  )}%`
                : ""}
            </p>
          </div>

          <span>Now</span>
        </div>

        <div className="activity-item">
          <span className="activity-dot green"></span>

          <div>
            <strong>
              {liveData.speaker_status || "Speaker analysis"}
            </strong>

            <p>
              {liveData.speaker_similarity != null
                ? `Identity confidence ${Math.round(
                    liveData.speaker_similarity * 100
                  )}%`
                : "Speaker verification status updated"}
            </p>
          </div>

          <span>Live</span>
        </div>

        <div className="activity-item">
          <span className="activity-dot"></span>

          <div>
            <strong>Inference active</strong>

            <p>
              {liveData.inference_ms != null
                ? `Latest inference ${Math.round(
                    liveData.inference_ms
                  )} ms`
                : "Processing audio stream"}
            </p>
          </div>

          <span>Live</span>
        </div>
      </>
    )}
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
      {showGateWarning && (
  <div className="gate-overlay">
    <div className="gate-popup">
      <div className="gate-icon">!</div>

      <div className="gate-popup-label">GATE ACTION</div>

      <h2>Highly Suspicious Call Detected</h2>

      <p>
        The call has reached a high synthetic-speech risk level.
        Sensitive actions should not proceed based on this call
        without independent verification.
      </p>

      <div className="gate-warning-text">
        It is strongly recommended to end the call if the caller
        cannot be independently verified.
      </div>

      <div className="gate-popup-actions">
        <button
          className="gate-end-btn"
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

            if (socket && socket.readyState === WebSocket.OPEN) {
              socket.send("end");
            }

            setShowGateWarning(false);
            setLiveData(null);
            setActivePage("Dashboard");
          }}
        >
          End Call
        </button>

        <button
          className="gate-dismiss-btn"
          onClick={() => setShowGateWarning(false)}
        >
          Dismiss Warning
        </button>
      </div>
    </div>
  </div>
)}
    </div>
  );
}

export default App;

