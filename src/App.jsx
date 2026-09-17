import { useEffect, useRef, useState } from 'react';
import QRCode from 'qrcode';
import { checkBackend, checkOperator, loginOperator, logoutOperator, getAudit, verifyVerdict } from './services/api';
import { CallClient } from './services/call';
import { unknownConnection } from './services/connection-stats';
import './App.css';

const initial = { callState: 'Ready', analysisState: 'Waiting for call', score: null,
  progress: null, session: null, verdict: null, audioLevel: 0, muted: false,
  inviteUrl: '', error: '', analysisError: '', droppedChunks: 0, history: [], connection: unknownConnection() };
const invitationParams = new URLSearchParams(window.location.hash.slice(1));
const invitationId = /^\/join\/([^/]+)\/?$/.exec(window.location.pathname)?.[1] || invitationParams.get('call');
const invitation = invitationId ? {
  call_id: invitationId, token: invitationParams.get('token'),
} : null;

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

      {/* RISK SPECTRUM */}
<div className="risk-spectrum-card">

  <div className="risk-spectrum-header">
    <div>
      <div className="section-label">RISK ASSESSMENT</div>
      <h2>Synthetic Speech Risk</h2>
    </div>

    <div className={`risk-value ${riskClass}`}>
      {final?.spoof_probability != null
        ? `${Math.round(final.spoof_probability * 100)}%`
        : "—"}
    </div>
  </div>

  <div className="risk-spectrum">

    <div className="risk-zone green-zone">
      <span>LOW</span>
      <small>0–40</small>
    </div>

    <div className="risk-zone amber-zone">
      <span>ELEVATED</span>
      <small>40–75</small>
    </div>

    <div className="risk-zone red-zone">
      <span>HIGH</span>
      <small>75–100</small>
    </div>

    {final?.spoof_probability != null && (
      <div
        className="risk-marker"
        style={{
          left: `${Math.min(
            100,
            Math.max(0, final.spoof_probability * 100)
          )}%`,
        }}
      >
        <div className="risk-marker-line"></div>
        <span>You are here</span>
      </div>
    )}

  </div>

  <div className="risk-spectrum-caption">
    Synthetic-speech probability compared with the configured response thresholds.
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
        {verdict?.algorithm || "—"} signed assessment
      </span>
    </div>
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
      {demo && <div className="demo-banner"><b>PIPELINE DEMO</b><span>Simulated detector · scores demonstrate processing and policy, not clone-detection accuracy.</span></div>}
      {state.error && <div className="alert" role="alert">{state.error}</div>}
      {state.notice && <div className="notice">{state.notice}</div>}
      {!active && healthError && <div className="alert">Start the backend, then retry. {healthError}</div>}
      {!window.isSecureContext && <div className="alert">Microphone access needs the trusted HTTPS demo address.</div>}
      {authError && (!needsSignIn || active) && <div className="alert" role="alert">{authError}{active ? ' Established audio can continue.' : ''}</div>}

      {needsSignIn && !active ? <section className="card signin-card">
        <span className="eyebrow">OPERATOR ACCESS</span>
        <h2>Sign in to create a call</h2>
        <p>Enter the access code shown by the online launcher on your laptop. Callers use their invitation to join.</p>
        <form onSubmit={signIn}>
          <label className="scenario" htmlFor="operator-code">Operator access code</label>
          <input id="operator-code" type="password" autoComplete="current-password" required value={accessCode}
            onChange={(event) => setAccessCode(event.target.value)} />
          {authError && <div className="alert" role="alert">{authError}</div>}
          <button className="primary" type="submit" disabled={authBusy || !accessCode.trim()}>{authBusy ? 'Signing in…' : 'Sign in'}</button>
        </form>
      </section> : <>

      <div className={`workspace ${caller ? 'caller-layout' : ''}`}>
        <section className="card connection-card">
          <div className="section-title"><span className="eyebrow">01 / CONVERSATION</span><span className={`badge ${state.callState === 'Connected' ? 'connected' : ''}`}>{state.callState}</span></div>
          <h2>{caller ? 'Your connection' : 'Connect the caller'}</h2>
          {!active && <>
            <p>{caller ? 'Use headphones to avoid echo. Tap below and allow microphone access.' : online
              ? 'Create a call, then share the invitation with the phone. Each device can use a different internet connection.'
              : 'Create a call, then scan the invitation with your Android phone on the same Wi-Fi.'}</p>
            {!caller && <label className="scenario">Detector mode
              <select value={scenario} onChange={(event) => setScenario(event.target.value)}>
                <option value="">{demo ? 'Live audio · simulated detector' : 'Configured model'}</option>
                {health?.demo_scenarios && ['GREEN', 'AMBER', 'RED'].map((band) => <option key={band} value={band}>Policy rehearsal · {band}</option>)}
              </select>
            </label>}
            {online && !caller && <label className="relay-option"><input type="checkbox" aria-label="Force relay" checked={forceRelay} onChange={(event) => setForceRelay(event.target.checked)} />
              <span>Force relay<small>Route both devices through TURN to rehearse relay connectivity.</small></span>
            </label>}
            <button className="primary" disabled={!window.isSecureContext || Boolean(healthError) || !health || (caller && state.callState === 'Ended')} onClick={start}>
              {caller ? 'Join call' : state.callState === 'Ended' ? 'Create another call' : 'Create call'} <span aria-hidden="true">↗</span>
            </button>
            {caller && state.callState === 'Ended' && <p>Ask the operator for a new invitation to call again.</p>}
          </>}
          {state.inviteUrl && state.callState !== 'Connected' && <div className="invitation">
            {qr && <img src={qr} alt="Scan this QR code on the caller phone" width="220" height="220" />}
            <p>Scan to join · invitation expires in 10 minutes</p>
            <input aria-label="Caller invitation URL" readOnly value={state.inviteUrl} onFocus={(event) => event.target.select()} />
            <button className="secondary" onClick={async () => {
              try { await navigator.clipboard.writeText(state.inviteUrl); setCopied(true); }
              catch { setCopied(false); }
            }}>{copied ? 'Copied' : 'Copy invitation'}</button>
            {!online && ['localhost', '127.0.0.1'].includes(window.location.hostname) && <p className="warning-text">For a phone, open the laptop’s HTTPS LAN address before creating the invitation.</p>}
          </div>}
          {active && <>
            <div className="peer-visual"><span>{caller ? 'L' : 'P'}</span><div><b>{caller ? 'Laptop operator' : 'Phone caller'}</b><p>{state.callState === 'Connected' ? 'Two-way audio connected' : 'Waiting for audio connection'}</p></div></div>
            <label className="meter-label">Received audio <span>{Math.round(state.audioLevel * 100)}%</span></label>
            <meter min="0" max="1" value={state.audioLevel} aria-label="Received audio level" />
            {online && <div className="connection-stats" aria-label="Connection diagnostics">
              <div><span>Connection route</span><b aria-label="Connection route">{state.connection.route}</b></div>
              <div><span>Transport</span><b>{state.connection.transport || '—'}</b></div>
              <div><span>Round-trip time</span><b>{state.connection.rttMs === null ? '—' : `${Math.round(state.connection.rttMs)} ms`}</b></div>
              <div><span>Received packet loss</span><b>{state.connection.packetLossPercent === null ? '—' : `${state.connection.packetLossPercent.toFixed(1)}%`}</b></div>
            </div>}
            <p className="quiet-hint">Start with two seconds of quiet, then speak naturally for 15–20 seconds.</p>
            <div className="controls"><button className="secondary" onClick={() => client.current.mute(!state.muted)}>{state.muted ? 'Unmute microphone' : 'Mute microphone'}</button>
              <button className="danger" disabled={state.callState === 'Ending'} onClick={() => { void client.current.end(); }}>End call</button></div>
          </>}
          {state.playbackBlocked && <button className="primary" onClick={() => { void client.current.play(); }}>Enable audio playback</button>}
          {state.signalingLost && active && <p className="warning-text">Pairing service disconnected. Established audio can continue.</p>}
          <audio ref={audio} autoPlay playsInline />
          <div className="device-footer">{caller ? 'PHONE → LAPTOP' : 'PHONE ⇄ LAPTOP'}<span>Audio only · no recording</span></div>
        </section>

        {!caller && <section className="card analysis-card">
          <div className="section-title"><span className="eyebrow">02 / CALLER ANALYSIS</span><span className={`badge ${state.analysisState === 'Unavailable' ? 'unavailable' : ''}`}>{state.analysisState}</span></div>
          <div className="score-row"><div><p className="metric-label">{demo ? 'SIMULATED SPOOF SCORE' : 'SPOOF SCORE'}</p><div className="score-number">{fresh && score ? Math.round(score.spoof_probability * 100) : '—'}<small>/100</small></div></div>
            <span className={`risk ${fresh ? score?.risk?.toLowerCase() || '' : ''}`}>{fresh && score ? score.risk : 'NO CURRENT RESULT'}</span></div>
          <div className="metric-grid"><div><span>Speech processed</span><b>{speech.toFixed(1)} s</b></div><div><span>Windows scored</span><b>{windows}</b></div><div><span>Inference</span><b>{score ? `${score.inference_ms.toFixed(1)} ms` : '—'}</b></div></div>
          <div className="timeline" aria-label="Spoof score history">
            <svg viewBox="0 0 600 110" role="img" aria-label="Recent spoof scores; thresholds shown as dashed lines">
              {[health?.thresholds?.amber ?? 0.5, health?.thresholds?.red ?? 0.8].map((threshold) => <line key={threshold} x1="0" x2="600" y1={105 - threshold * 100} y2={105 - threshold * 100} className="threshold" />)}
              <polyline points={state.history.map((point, index) => `${index * 600 / Math.max(1, state.history.length - 1)},${105 - point.spoof_probability * 100}`).join(' ')} />
              {state.history.length === 1 && <circle cx="4" cy={105 - state.history[0].spoof_probability * 100} r="3" fill="#477eaa" />}
            </svg>
            <div className="timeline-caption"><span>{state.history.length ? 'Recent scored windows' : 'Waiting for the first speech window'}</span><span>AMBER {Math.round((health?.thresholds?.amber ?? 0.5) * 100)} · RED {Math.round((health?.thresholds?.red ?? 0.8) * 100)}</span></div>
          </div>
          <div className="policy"><span className="eyebrow">ADVISORY POLICY</span><h3>{fresh && score ? score.decision.action : 'Waiting for evidence'}</h3>
            <p>{fresh && score ? actionText[score.decision.action] : 'The first result needs 4.04 seconds of detected caller speech. Silence does not count.'}</p>
            {fresh && score?.challenge && <blockquote>“{score.challenge.phrase}”<small>Manual verification prompt · spoken answers are not automatically checked.</small></blockquote>}
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
