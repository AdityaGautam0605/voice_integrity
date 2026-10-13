import { useEffect, useRef, useState } from "react";
import {
  checkBackend,
  checkOperator,
  loginOperator,
  getAudit,
  verifyVerdict,
} from "./services/api";
import { AnalysisClient } from "./services/analysis";
import "./App.css";
import CallWorkspace from "./CallWorkspace";
import Enrollment from "./Enrollment";

const pages = ["Dashboard", "Live Call", "Verdict", "Audit"];
const percent = (value) =>
  value == null ? "—" : `${Math.round(value * 100)}%`;
const readable = (value) =>
  value
    ? value
        .replaceAll("_", " ")
        .toLowerCase()
        .replace(/^./, (c) => c.toUpperCase())
    : "Unavailable";
const riskName = (risk) =>
  ({ GREEN: "Low risk", AMBER: "Elevated risk", RED: "High risk" })[risk] ||
  "Not assessed";
const date = (value) =>
  value
    ? new Date(value).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Time unavailable";
const payloadOf = (entry) => entry.payload?.payload || {};

function Icon({ name = "wave", size = 20 }) {
  const paths = {
    wave: (
      <>
        <path d="M3 10v4m4-8v12m5-16v20m5-16v12m4-8v4" />
      </>
    ),
    plus: <path d="M12 5v14M5 12h14" />,
    arrow: <path d="m9 5 7 7-7 7" />,
    shield: (
      <>
        <path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z" />
        <path d="m8 12 3 3 5-6" />
      </>
    ),
    user: (
      <>
        <circle cx="12" cy="8" r="4" />
        <path d="M5 21v-2a7 7 0 0 1 14 0v2z" />
      </>
    ),
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    search: (
      <>
        <circle cx="10" cy="10" r="6" />
        <path d="m15 15 5 5" />
      </>
    ),
    file: (
      <>
        <path d="M5 3h9l5 5v13H5zM14 3v6h5M9 13h6m-6 4h6" />
      </>
    ),
    mic: (
      <>
        <rect x="9" y="2" width="6" height="13" rx="3" />
        <path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8" />
      </>
    ),
    stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </>
    ),
    refresh: (
      <>
        <path d="M20 7v5h-5M4 17v-5h5" />
        <path d="M6 6a8 8 0 0 1 13 2M5 16a8 8 0 0 0 13 2" />
      </>
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] || paths.wave}
    </svg>
  );
}

function Badge({ risk, children, tone = "" }) {
  return (
    <span className={`badge ${risk?.toLowerCase() || tone}`}>
      <span className="badge-dot" />
      {children || riskName(risk)}
    </span>
  );
}

function Empty({ icon = "wave", title, children, action }) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <Icon name={icon} size={28} />
      </span>
      <h2>{title}</h2>
      <p>{children}</p>
      {action}
    </div>
  );
}

function Modal({ title, children, onClose }) {
  const ref = useRef(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      aria-labelledby="modal-title"
      className="modal"
    >
      <button
        className="icon-button modal-close"
        aria-label="Close dialog"
        onClick={onClose}
      >
        <Icon name="close" />
      </button>
      <div className="modal-heading">
        <span className="empty-icon">
          <Icon name="wave" size={28} />
        </span>
        <h2 id="modal-title">{title}</h2>
      </div>
      {children}
    </dialog>
  );
}

function App() {
  const [page, setPage] = useState(window.location.pathname === "/call" ? "Live Call" : "Dashboard");
  const [callMode, setCallMode] = useState("call");
  const [callBusy, setCallBusy] = useState(false);
  const startNewSession = () => callMode === "microphone" && !callBusy ? setNewSession(true) : setPage("Live Call");
  const [operator, setOperator] = useState(null);
  const [operatorCode, setOperatorCode] = useState("");
  const [authError, setAuthError] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [health, setHealth] = useState(null);
  const [audit, setAudit] = useState(null);
  const [auditOffset, setAuditOffset] = useState(0);
  const [auditVersion, setAuditVersion] = useState(0);
  const [auditError, setAuditError] = useState("");
  const [auditBusy, setAuditBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [riskFilter, setRiskFilter] = useState("ALL");
  const [selected, setSelected] = useState(null);
  const [session, setSession] = useState(null);
  const [score, setScore] = useState(null);
  const [history, setHistory] = useState([]);
  const [progress, setProgress] = useState(null);
  const [verdict, setVerdict] = useState(null);
  const [verificationResult, setVerification] = useState(null);
  const [verifyBusy, setVerifyBusy] = useState(false);
  const [analysisState, setAnalysisState] = useState("Ready");
  const [error, setError] = useState("");
  const [active, setActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [newSession, setNewSession] = useState(false);
  const [gate, setGate] = useState(false);
  const [manualCheck, setManualCheck] = useState(false);
  const [bars, setBars] = useState(Array(56).fill(3));
  const [elapsed, setElapsed] = useState(0);
  const audio = useRef(null);
  const gateShown = useRef(false);
  const verificationRequest = useRef(0);

  useEffect(() => {
    let cancelled = false;
    const requireSignIn = () => {
      setOperator({ online_mode: true, authenticated: false });
      setAudit(null);
      setActive(false);
      setScore(null);
      setNewSession(false);
      const current = audio.current;
      if (current) {
        cancelAnimationFrame(current.frame);
        current.stream?.getTracks().forEach((track) => track.stop());
        void current.client?.stop().finally(() => current.context?.close());
        audio.current = null;
      }
      setAuthError("Your session expired. Please sign in again.");
    };
    window.addEventListener("vif:operator-auth-required", requireSignIn);
    checkOperator()
      .then((value) => {
        if (!cancelled) {
          setOperator(value);
          setAuthError("");
        }
      })
      .catch((err) => {
        if (!cancelled) setAuthError(err.message);
      });
    checkBackend()
      .then((value) => {
        if (!cancelled) setHealth(value);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      window.removeEventListener("vif:operator-auth-required", requireSignIn);
    };
  }, [attempt]);

  const canAccess =
    operator && (!operator.online_mode || operator.authenticated);
  useEffect(() => {
    if (!canAccess) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      setAuditBusy(true);
      getAudit(
        page === "Audit"
          ? { offset: auditOffset, q: query, risk: riskFilter }
          : {},
      )
        .then((value) => {
          if (!cancelled) {
            setAudit(value);
            setAuditError("");
          }
        })
        .catch((err) => {
          if (!cancelled) {
            setAuditError(err.message);
            setAudit(null);
          }
        })
        .finally(() => {
          if (!cancelled) setAuditBusy(false);
        });
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [canAccess, verdict, page, auditOffset, query, riskFilter, auditVersion]);

  useEffect(() => {
    let cancelled = false;
    const timer = setInterval(() => {
      checkBackend()
        .then((value) => {
          if (!cancelled) setHealth(value);
        })
        .catch(() => {
          if (!cancelled) setHealth(null);
        });
    }, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [active]);

  useEffect(
    () => () => {
      const current = audio.current;
      if (!current) return;
      cancelAnimationFrame(current.frame);
      current.stream?.getTracks().forEach((track) => track.stop());
      current.client?.detach();
      current.client?.socket?.close();
      current.context?.close().catch(() => {});
    },
    [],
  );

  async function signIn(event) {
    event.preventDefault();
    setAuthBusy(true);
    setAuthError("");
    try {
      await loginOperator(operatorCode.trim());
      const status = await checkOperator();
      if (!status.authenticated)
        throw new Error(
          "The sign-in cookie was not accepted. Open the current demo URL and retry.",
        );
      setOperator(status);
      setOperatorCode("");
    } catch (err) {
      setAuthError(err.message);
    } finally {
      setAuthBusy(false);
    }
  }

  function refreshAudit() {
    setAuditBusy(true);
    setAuditVersion((value) => value + 1);
  }

  async function releaseAudio() {
    const current = audio.current;
    if (!current) return;
    cancelAnimationFrame(current.frame);
    current.stream?.getTracks().forEach((track) => track.stop());
    current.analyser?.disconnect();
    try {
      await current.client?.stop();
    } finally {
      if (current.context?.state !== "closed") await current.context?.close();
      audio.current = null;
    }
  }

  async function startSession() {
    if (busy || active) return;
    setBusy(true);
    setError("");
    setAnalysisState("Connecting");
    try {
      if (!navigator.mediaDevices?.getUserMedia)
        throw new Error("Microphone access requires HTTPS or localhost.");
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const current = { stream };
      audio.current = current;
      const context = new AudioContext();
      current.context = context;
      await context.resume();
      setSession(null);
      setScore(null);
      setHistory([]);
      setProgress(null);
      setVerdict(null);
      setVerification(null);
      verificationRequest.current = Symbol();
      setElapsed(0);
      setManualCheck(false);
      gateShown.current = false;
      const client = new AnalysisClient(context, (update) => {
        if (update.session) setSession(update.session);
        if (update.analysisState) setAnalysisState(update.analysisState);
        if (update.analysisState === "Unavailable") {
          setScore(null);
          setHistory([]);
          setActive(false);
          setBars(Array(56).fill(3));
          if (!current.lost) {
            current.lost = true;
            queueMicrotask(() => {
              void releaseAudio().catch(() => {});
            });
          }
        }
        if (update.analysisError !== undefined) setError(update.analysisError);
        if (update.progress) setProgress(update.progress);
        if (update.score) {
          setScore(update.score);
          setHistory((items) => [
            ...items.slice(-89),
            update.score.spoof_probability,
          ]);
          if (
            ["GATE", "GATE_ACTION"].includes(update.score.decision?.action) &&
            !gateShown.current
          ) {
            gateShown.current = true;
            setGate(true);
          }
        }
        if (update.verdict) {
          setVerdict(update.verdict);
          setVerification(null);
          setPage("Verdict");
        }
      });
      current.client = client;
      await client.start(stream);
      if (client.socket?.readyState !== WebSocket.OPEN)
        throw new Error(
          "The analysis connection closed. Please try a new session.",
        );
      const source = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser();
      // Read audio samples, not frequency bins. Each bar records the measured
      // microphone level over time, so quiet input cannot animate the display.
      analyser.fftSize = 2048;
      current.analyser = analyser;
      source.connect(analyser);
      const samples = new Float32Array(analyser.fftSize);
      const levels = Array(56).fill(3);
      let lastPaint = 0;
      const draw = (time) => {
        if (time - lastPaint >= 40) {
          analyser.getFloatTimeDomainData(samples);
          let energy = 0;
          let mean = 0;
          for (const sample of samples) mean += sample;
          mean /= samples.length;
          for (const sample of samples) energy += (sample - mean) ** 2;
          const rms = Math.sqrt(energy / samples.length);
          // A fixed dB scale preserves loudness differences without amplifying
          // silence through automatic visual normalization. Floor: -60 dBFS.
          const db = 20 * Math.log10(Math.max(rms, 0.000001));
          const height = 3 + 92 * Math.max(0, Math.min(1, (db + 60) / 54));
          levels.shift();
          levels.push(height);
          setBars([...levels]);
          lastPaint = time;
        }
        current.frame = requestAnimationFrame(draw);
      };
      current.frame = requestAnimationFrame(draw);
      setBars([...levels]);
      setActive(true);
      setNewSession(false);
      setPage("Live Call");
    } catch (err) {
      setError(
        err.message ||
          "Unable to access the microphone. Check browser permissions.",
      );
      await releaseAudio().catch(() => {});
      setAnalysisState("Unavailable");
    } finally {
      setBusy(false);
    }
  }

  async function endSession() {
    setBusy(true);
    setGate(false);
    setActive(false);
    try {
      await releaseAudio();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
      setBars(Array(56).fill(3));
    }
  }

  async function verify() {
    const request = Symbol();
    verificationRequest.current = request;
    setVerifyBusy(true);
    try {
      const result = await verifyVerdict(verdict);
      if (request === verificationRequest.current)
        setVerification({ verdict, result });
    } catch (err) {
      if (request === verificationRequest.current)
        setVerification({
          verdict,
          result: { valid: false, reason: err.message },
        });
    } finally {
      setVerifyBusy(false);
    }
  }

  function openRecord(entry) {
    if (active || busy) {
      setPage("Live Call");
      return;
    }
    setVerdict(entry.payload);
    setVerification(null);
    setPage("Verdict");
  }

  const entries = [...(audit?.entries || [])].sort(
    (a, b) => (b.seq ?? b.ts_ms) - (a.seq ?? a.ts_ms),
  );
  const filtered = entries.filter((entry) => {
    const payload = payloadOf(entry);
    return (
      (riskFilter === "ALL" || payload.risk === riskFilter) &&
      `${payload.session_id || ""} ${payload.action || ""} ${payload.risk || ""}`
        .toLowerCase()
        .includes(query.toLowerCase())
    );
  });
  const selectedEntry =
    filtered.find((entry) => entry.entry_hash === selected) || filtered[0];
  const final = verdict?.payload;
  const verification =
    verificationResult?.verdict === verdict
      ? {
          ...verificationResult.result,
          fresh:
            verificationResult.result.fresh ?? verificationResult.result.valid,
          valid:
            verificationResult.result.signature_valid ??
            verificationResult.result.valid,
        }
      : null;
  const demo = session?.demo_mode ?? health?.demo_mode;
  const duration = `${String(Math.floor(elapsed / 60)).padStart(2, "0")}:${String(elapsed % 60).padStart(2, "0")}`;
  const newButton = (
    <button
      className="button primary"
      disabled={busy}
      onClick={() => (active ? setPage("Live Call") : startNewSession())}
    >
      <Icon name={active ? "wave" : "plus"} size={17} />
      {active ? "Open live session" : "New session"}
    </button>
  );
  const pageHeading = (title, subtitle, action) => (
    <div className="page-heading">
      <div>
        <h1>{title}</h1>
        <p>{subtitle}</p>
      </div>
      {action}
    </div>
  );

  function renderDashboard() {
    return (
      <div className="page dashboard-page">
        <div className="eyebrow">YOUR WORKSPACE</div>
        {pageHeading(
          "A little more clarity. A lot more trust.",
          "Start a session, understand the signals, and keep a record of every assessment.",
          newButton,
        )}
        <div className="summary-grid">
          <div className="summary-card lavender">
            <span className="tile-icon">
              <Icon name="wave" />
            </span>
            <div>
              <span>Recorded sessions</span>
              <strong>{audit ? (audit.count ?? entries.length) : "—"}</strong>
              <small>Signed records in your audit log</small>
            </div>
          </div>
          <div className="summary-card mint">
            <span className="tile-icon">
              <Icon name="shield" />
            </span>
            <div>
              <span>For review</span>
              <strong>
                {audit
                  ? entries.filter((entry) =>
                      ["AMBER", "RED"].includes(payloadOf(entry).risk),
                    ).length
                  : "—"}
              </strong>
              <small>Elevated risk in loaded records</small>
            </div>
          </div>
          <div className="summary-card blue">
            <span className="tile-icon">
              <Icon name="mic" />
            </span>
            <div>
              <span>Current session</span>
              <strong className="summary-word">
                {active ? "In progress" : "Ready when you are"}
              </strong>
              <small>
                {active
                  ? `${duration} · ${analysisState}`
                  : "Live microphone analysis"}
              </small>
            </div>
          </div>
        </div>
        <div className="section-heading">
          <h2>Recent sessions</h2>
          <button
            className="text-button"
            onClick={() => {
              setPage("Audit");
              refreshAudit();
            }}
          >
            View audit trail <Icon name="arrow" size={16} />
          </button>
        </div>
        {auditError && (
          <p className="notice error" role="alert">
            Session history unavailable: {auditError}{" "}
            <button className="text-button" onClick={refreshAudit}>
              Retry
            </button>
          </p>
        )}
        <div className="session-grid">
          <button
            className="session-card create-card"
            disabled={busy}
            onClick={() =>
              active ? setPage("Live Call") : startNewSession()
            }
          >
            <span className="create-icon">
              <Icon name={active ? "wave" : "plus"} size={24} />
            </span>
            <h3>{active ? "Return to live session" : "Create new session"}</h3>
            <p>Give every conversation a closer look.</p>
          </button>
          {entries.slice(0, 7).map((entry, index) => {
            const payload = payloadOf(entry);
            return (
              <button
                className={`session-card ${["peach", "blue", "lavender", "mint"][index % 4]}`}
                key={entry.entry_hash || index}
                onClick={() => openRecord(entry)}
              >
                <div className="session-card-top">
                  <span className="tile-icon">
                    <Icon name="wave" size={26} />
                  </span>
                  <Icon name="arrow" size={17} />
                </div>
                <h3>Session {payload.session_id?.slice(0, 8) || "record"}</h3>
                <p>{date(entry.ts_ms)}</p>
                <Badge risk={payload.risk} />
              </button>
            );
          })}
          {!entries.length && (
            <div className="first-session">
              <Icon name="file" size={26} />
              <h3>Your sessions will live here</h3>
              <p>
                Complete your first analysis to see its signed result and return
                to it anytime.
              </p>
            </div>
          )}
        </div>
        <div className="workspace-note">
          <Icon name="shield" />
          <p>
            <strong>Three signals. A clearer picture.</strong> Synthetic speech,
            speaker identity, and liveness are assessed separately. A low spoof
            score does not verify identity.
          </p>
        </div>
      </div>
    );
  }

  function renderLive() {
    return (
      <div className="page live-page">
        {pageHeading(
          "Live Call",
          "A closer look at the voice on the other end.",
          <Badge
            tone={active && analysisState !== "Unavailable" ? "green" : ""}
          >
            {active ? analysisState : "No active session"}
          </Badge>,
        )}
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
        <div className="live-workspace">
          <section className="panel sources-panel">
            <div className="panel-heading">
              <h2>Sources</h2>
              <Icon name="mic" size={17} />
            </div>
            <div className="panel-body">
              <span className="label">SESSION</span>
              <h3 className="session-id">
                {session ? session.session_id.slice(0, 12) : "No session yet"}
              </h3>
              <div className="source-device">
                <Icon name="mic" />
                <div>
                  <strong>Default microphone</strong>
                  <small>Local audio source</small>
                </div>
              </div>
              <Badge tone={active ? "green" : ""}>
                {active ? "Microphone active" : "Microphone idle"}
              </Badge>
              <div className="source-duration">
                <Icon name="clock" />
                <div>
                  <span className="label">DURATION</span>
                  <strong>{duration}</strong>
                </div>
              </div>
              <dl className="source-details">
                <div>
                  <dt>Speech analyzed</dt>
                  <dd>
                    {Number(
                      score?.speech_seconds ?? progress?.speech_seconds ?? 0,
                    ).toFixed(1)}{" "}
                    s
                  </dd>
                </div>
                <div>
                  <dt>Sample rate</dt>
                  <dd>
                    {session?.sample_rate
                      ? `${session.sample_rate / 1000} kHz`
                      : "—"}
                  </dd>
                </div>
                <div>
                  <dt>Model</dt>
                  <dd>
                    {session?.model_version ||
                      health?.model_version ||
                      "Unavailable"}
                  </dd>
                </div>
              </dl>
            </div>
            <div className="panel-footnote">
              <Icon name="shield" size={17} />
              <span>
                Analysis informs the next action. It does not establish
                identity.
              </span>
            </div>
          </section>
          <section className="panel analysis-panel">
            <div className="panel-heading">
              <h2>Analysis</h2>
              <span className="subtle">
                {demo ? "Simulated detector" : "Live voice analysis"}
              </span>
            </div>
            <div className="panel-body">
              <div className="waveform-area lavender">
                <div className="waveform-title">
                  <span>Live audio</span>
                  <span>{active ? "Listening" : "Standby"}</span>
                </div>
                <div
                  className="waveform"
                  aria-label={
                    active ? "Live microphone waveform" : "Microphone inactive"
                  }
                >
                  {bars.map((height, index) => (
                    <i key={index} style={{ height: `${height}%` }} />
                  ))}
                </div>
                <span className="waveform-caption">
                  {active
                    ? "Listening for speech from your microphone"
                    : "Start a session to see your audio here"}
                </span>
              </div>
              <div className="chart-card">
                <div className="section-heading">
                  <h3>Spoof probability</h3>
                  <strong>{percent(score?.spoof_probability)}</strong>
                </div>
                <div className="chart">
                  <div className="chart-labels">
                    <span>100%</span>
                    <span>50%</span>
                    <span>0%</span>
                  </div>
                  <svg
                    viewBox="0 0 500 150"
                    preserveAspectRatio="none"
                    role="img"
                    aria-label="Spoof probability over recent analysis windows"
                  >
                    <path
                      className="chart-grid"
                      d="M0 1H500M0 75H500M0 149H500M100 0V150M200 0V150M300 0V150M400 0V150"
                    />
                    {history.length > 0 && (
                      <polyline
                        className="chart-line"
                        points={history
                          .map(
                            (value, index) =>
                              `${history.length === 1 ? 250 : (index / (history.length - 1)) * 500},${149 - value * 148}`,
                          )
                          .join(" ")}
                      />
                    )}
                    {history.length === 1 && (
                      <circle
                        cx="250"
                        cy={149 - history[0] * 148}
                        r="3"
                        fill="#8666cd"
                      />
                    )}
                  </svg>
                  {!history.length && (
                    <span className="chart-empty">
                      Waiting for the first analysis window
                    </span>
                  )}
                </div>
                <div className="chart-footer">
                  <span>Recent analysis windows</span>
                  <span>
                    {score?.inference_ms != null
                      ? `${Math.round(score.inference_ms)} ms inference`
                      : "No score yet"}
                  </span>
                </div>
              </div>
            </div>
            <div className="panel-body">
              {session && active && (
                <Enrollment
                  key={session.session_id}
                  sessionId={session.session_id}
                />
              )}
            </div>
            <div className="call-controls">
              <span className="mic-indicator">
                <Icon name="mic" />
              </span>
              {active ? (
                <button
                  className="button primary"
                  onClick={endSession}
                  disabled={busy}
                >
                  <Icon name="stop" size={16} />
                  End session
                </button>
              ) : busy ? (
                <span className="subtle">Finalizing session…</span>
              ) : (
                newButton
              )}
            </div>
          </section>
          <section className="panel signals-panel">
            <div className="panel-heading">
              <h2>Signals</h2>
              <Icon name="grid" size={17} />
            </div>
            <div className="panel-body">
              <div
                className={`signal-tile ${score?.risk === "RED" ? "rose" : score?.risk === "AMBER" ? "peach" : score ? "mint" : "neutral"}`}
              >
                <Icon name="shield" />
                <div>
                  <span>Spoof probability</span>
                  <strong>{percent(score?.spoof_probability)}</strong>
                  <Badge risk={score?.risk} />
                </div>
              </div>
              <div className="signal-tile blue">
                <Icon name="user" />
                <div>
                  <span>Speaker</span>
                  <strong className="signal-word">
                    {readable(score?.speaker_status)}
                  </strong>
                  <small>
                    {score?.speaker_similarity != null
                      ? `${percent(score.speaker_similarity)} similarity`
                      : "Identity assessed separately"}
                  </small>
                </div>
              </div>
              <div className="signal-tile lavender">
                <Icon name="wave" />
                <div>
                  <span>Liveness</span>
                  <strong className="signal-word">
                    {score?.liveness_score != null
                      ? Number(score.liveness_score).toFixed(2)
                      : "Unavailable"}
                  </strong>
                  <small>Conversational timing signals</small>
                </div>
              </div>
              <div className="signal-tile peach">
                <Icon name="file" />
                <div>
                  <span>Policy</span>
                  <strong className="signal-word">
                    {readable(
                      score?.decision?.action || "WAITING_FOR_ANALYSIS",
                    )}
                  </strong>
                  <small>
                    {score?.decision?.reason ||
                      "A recommendation appears after analysis."}
                  </small>
                </div>
              </div>
              {["CHALLENGE", "GATE", "GATE_ACTION"].includes(
                score?.decision?.action,
              ) && (
                <div className="challenge-card">
                  <h3>Independent verification</h3>
                  <p>
                    {score?.challenge?.phrase ||
                      "Confirm the caller through an independent, trusted channel before sensitive actions."}
                  </p>
                  <button
                    className="button secondary"
                    onClick={() => setManualCheck(true)}
                    disabled={manualCheck}
                  >
                    {manualCheck
                      ? "Manual check in progress"
                      : "Start manual check"}
                  </button>
                  <small>This does not automatically verify identity.</small>
                </div>
              )}
            </div>
          </section>
        </div>
      </div>
    );
  }

  function renderVerdict() {
    return (
      <div className="page verdict-page">
        {pageHeading(
          "Session verdict",
          final
            ? `Detailed results for session ${final.session_id?.slice(0, 12) || "—"}.`
            : "A signed summary of your completed analysis.",
          <button
            className="button secondary"
            onClick={() => setPage("Dashboard")}
          >
            Back to workspace
          </button>,
        )}
        {!final ? (
          <div className="panel">
            <Empty
              icon="shield"
              title="Every session ends with a clearer picture"
              action={newButton}
            >
              Complete a session or open a recent record to view its assessment
              and verify its signature.
            </Empty>
          </div>
        ) : (
          <>
            {(final.demo_mode || final.model_version?.startsWith("stub-")) && (
              <p className="notice">
                Demo result · simulated detector. This is not detection
                evidence.
              </p>
            )}
            <section
              className={`verdict-banner ${final.risk === "GREEN" ? "mint" : final.risk === "RED" ? "rose" : final.risk === "AMBER" ? "peach" : "neutral"}`}
            >
              <span className="tile-icon">
                <Icon name="shield" size={28} />
              </span>
              <div>
                <h2>{riskName(final.risk)}</h2>
                <p>
                  {final.decision?.reason ||
                    `Recommended action: ${readable(final.action)}`}
                </p>
              </div>
              <div className="verdict-probability">
                <span>Spoof probability</span>
                <strong>{percent(final.spoof_probability)}</strong>
              </div>
            </section>
            <div className="verdict-signals">
              <div className="signal-tile mint">
                <Icon name="shield" />
                <div>
                  <span>Spoof risk</span>
                  <strong>{percent(final.spoof_probability)}</strong>
                  <Badge risk={final.risk} />
                </div>
              </div>
              <div className="signal-tile blue">
                <Icon name="user" />
                <div>
                  <span>Speaker</span>
                  <strong className="signal-word">
                    {readable(final.speaker_status)}
                  </strong>
                  <small>Separate from synthetic-speech risk</small>
                </div>
              </div>
              <div className="signal-tile lavender">
                <Icon name="wave" />
                <div>
                  <span>Liveness</span>
                  <strong className="signal-word">
                    {final.liveness_score != null
                      ? Number(final.liveness_score).toFixed(2)
                      : "Unavailable"}
                  </strong>
                  <small>Conversational timing assessment</small>
                </div>
              </div>
            </div>
            <section className="panel signature-panel">
              <div className="section-heading">
                <div className="title-with-icon">
                  <Icon name="file" size={28} />
                  <div>
                    <h2>Signature verification</h2>
                    <p>Check that this assessment has not been altered.</p>
                  </div>
                </div>
                <button
                  className="button primary"
                  disabled={verifyBusy || !verdict.signature}
                  onClick={verify}
                >
                  <Icon name="shield" size={16} />
                  {verifyBusy ? "Verifying…" : "Verify signature"}
                </button>
              </div>
              <dl className="signature-details">
                <div>
                  <dt>Status</dt>
                  <dd>
                    <Badge
                      tone={
                        verification
                          ? verification.valid
                            ? "green"
                            : "red"
                          : ""
                      }
                    >
                      {verification
                        ? verification.valid
                          ? "Verified"
                          : "Verification failed"
                        : verdict.signature
                          ? "Not yet verified"
                          : "Signature unavailable"}
                    </Badge>
                  </dd>
                </div>
                <div>
                  <dt>Algorithm</dt>
                  <dd>{verdict.algorithm || "—"}</dd>
                </div>
                <div>
                  <dt>Session ID</dt>
                  <dd className="mono">{final.session_id || "—"}</dd>
                </div>
              </dl>
              {verification && (
                <p
                  className={`notice ${verification.valid ? "" : "error"}`}
                  role="status"
                >
                  {(verification.valid && !verification.fresh
                    ? "Signature intact, but this verdict is not fresh enough to authorize a new action. "
                    : "") +
                    (verification.reason ||
                      (verification.valid
                        ? "The signature is valid. This confirms integrity, not the accuracy of the assessment."
                        : "Do not rely on this verdict until it is independently verified."))}
                </p>
              )}
            </section>
            <section className="panel details-panel">
              <h2>Assessment details</h2>
              <dl className="assessment-details">
                <div>
                  <dt>Model</dt>
                  <dd>{final.model_version || "Unavailable"}</dd>
                </div>
                <div>
                  <dt>Policy action</dt>
                  <dd>{readable(final.action || final.decision?.action)}</dd>
                </div>
                <div>
                  <dt>Completed</dt>
                  <dd>{date(final.timestamp)}</dd>
                </div>
                <div>
                  <dt>Speech analyzed</dt>
                  <dd>
                    {final.speech_seconds != null
                      ? `${Number(final.speech_seconds).toFixed(1)} seconds`
                      : "Unavailable"}
                  </dd>
                </div>
              </dl>
            </section>
          </>
        )}
      </div>
    );
  }

  function renderAudit() {
    const detail = selectedEntry ? payloadOf(selectedEntry) : null;
    return (
      <div className="page audit-page">
        {pageHeading(
          "Audit trail",
          "A traceable record of every completed assessment.",
          <button
            className="button secondary"
            onClick={refreshAudit}
            disabled={auditBusy}
          >
            <Icon name="refresh" size={16} />
            {auditBusy ? "Refreshing…" : "Refresh records"}
          </button>,
        )}
        {auditError && (
          <p className="notice error" role="alert">
            {auditError}
          </p>
        )}
        <div
          className={`chain-banner ${audit ? (audit.chain_valid ? "mint" : "rose") : "neutral"}`}
        >
          <span className="tile-icon">
            <Icon name="shield" size={25} />
          </span>
          <div>
            <span className="label">HASH CHAIN</span>
            <h2>
              {audit
                ? audit.chain_valid
                  ? "Integrity verified"
                  : "Review required"
                : auditError
                  ? "Unavailable"
                  : "Loading records…"}
            </h2>
            <p>
              {audit
                ? audit.chain_valid
                  ? "The backend verified the links between audit records."
                  : audit.reason || "The audit chain could not be verified."
                : "Chain status will appear when records are loaded."}
            </p>
          </div>
          <span className="record-count">{audit?.count ?? "—"} records</span>
        </div>
        <div className="audit-toolbar">
          <label className="search-field">
            <Icon name="search" size={17} />
            <input
              aria-label="Search sessions"
              placeholder="Search sessions or actions…"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setAuditOffset(0);
              }}
            />
          </label>
          <select
            aria-label="Filter by risk"
            value={riskFilter}
            onChange={(event) => {
              setRiskFilter(event.target.value);
              setAuditOffset(0);
            }}
          >
            <option value="ALL">All risk levels</option>
            <option value="GREEN">Low risk</option>
            <option value="AMBER">Elevated risk</option>
            <option value="RED">High risk</option>
          </select>
          <span className="subtle">
            {filtered.length} shown · {audit?.total ?? filtered.length} matching
          </span>
        </div>
        <div className="audit-toolbar" aria-label="Audit pagination">
          <button
            className="button secondary"
            disabled={auditBusy || auditOffset === 0}
            onClick={() => setAuditOffset(Math.max(0, auditOffset - 50))}
          >
            Previous records
          </button>
          <span>Page {Math.floor(auditOffset / 50) + 1}</span>
          <button
            className="button secondary"
            disabled={auditBusy || !audit?.has_more}
            onClick={() => setAuditOffset(auditOffset + 50)}
          >
            Next records
          </button>
        </div>
        <div className="audit-layout">
          <section className="panel audit-table-panel">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Session</th>
                    <th>Completed</th>
                    <th>Risk</th>
                    <th>Record</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((entry, index) => {
                    const payload = payloadOf(entry);
                    return (
                      <tr
                        key={entry.entry_hash || index}
                        className={
                          selectedEntry === entry ? "selected-row" : ""
                        }
                      >
                        <td>
                          <button
                            className="record-button"
                            onClick={() => setSelected(entry.entry_hash)}
                            aria-label={`View session ${payload.session_id}`}
                          >
                            {payload.session_id?.slice(0, 8) || "Unknown"}
                          </button>
                        </td>
                        <td>{date(entry.ts_ms)}</td>
                        <td>
                          <Badge risk={payload.risk} />
                        </td>
                        <td>
                          {entry.payload?.signature ? "Signed" : "Unsigned"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {!filtered.length && (
              <Empty
                icon="file"
                title={
                  query || riskFilter !== "ALL"
                    ? "No matching sessions"
                    : "No audit records yet"
                }
              >
                {query || riskFilter !== "ALL"
                  ? "Try another search or risk filter."
                  : "Completed sessions will appear here with their signed assessment."}
              </Empty>
            )}
          </section>
          {detail && (
            <aside className="panel record-details">
              <div className="panel-heading">
                <h2>Session details</h2>
                <Icon name="file" size={17} />
              </div>
              <div className="panel-body">
                <dl>
                  <div>
                    <dt>Session ID</dt>
                    <dd className="mono">
                      {detail.session_id || "Unavailable"}
                    </dd>
                  </div>
                  <div>
                    <dt>Completed</dt>
                    <dd>{date(selectedEntry.ts_ms)}</dd>
                  </div>
                  <div>
                    <dt>Policy action</dt>
                    <dd>
                      {readable(detail.action || detail.decision?.action)}
                    </dd>
                  </div>
                  <div>
                    <dt>Risk</dt>
                    <dd>
                      <Badge risk={detail.risk} />
                    </dd>
                  </div>
                  <div>
                    <dt>Signature</dt>
                    <dd>
                      {selectedEntry.payload?.signature
                        ? "Attached · not verified here"
                        : "Unavailable"}
                    </dd>
                  </div>
                  <div>
                    <dt>Record hash</dt>
                    <dd className="mono hash">
                      {selectedEntry.entry_hash || "Unavailable"}
                    </dd>
                  </div>
                </dl>
                <button
                  className="button secondary full-width"
                  onClick={() => openRecord(selectedEntry)}
                >
                  {active ? "Open live session" : "Open verdict"}
                  <Icon name="arrow" size={16} />
                </button>
              </div>
            </aside>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="app-header">
        <button
          className="brand"
          onClick={() => setPage("Dashboard")}
          aria-label="Voice Integrity home"
        >
          <span className="brand-mark">
            <Icon name="wave" size={21} />
          </span>
          <span>Voice Integrity</span>
        </button>
        <nav aria-label="Main navigation">
          {pages.map((item) => (
            <button
              key={item}
              className={`nav-tab ${page === item ? "active" : ""}`}
              aria-current={page === item ? "page" : undefined}
              onClick={() => setPage(item)}
            >
              {item}
            </button>
          ))}
        </nav>
        <div className="header-status">

          <span
            className={`connection-dot ${canAccess && health ? "connected" : ""}`}
          />
          <span>
            {canAccess && health
              ? "Workspace connected"
              : "Backend unavailable"}
          </span>
          <span className="avatar" aria-label="Operator workspace">
            <Icon name="user" size={17} />
          </span>
        </div>
      </header>
      {canAccess && demo && (
        <div className="demo-strip">
          <span className="badge-dot" />
          Demo · simulated detector
          <span className="demo-detail">
            Scores demonstrate the workflow and are not detection evidence.
          </span>
        </div>
      )}
      <main id="main">
        {!operator ? (
          <section className="panel access-panel">
            <Empty icon="shield" title="Connecting to your workspace">
              Checking backend and operator access.
            </Empty>
            {authError && (
              <>
                <p className="notice error" role="alert">
                  {authError}
                </p>
                <button
                  className="button primary"
                  onClick={() => {
                    setAuthError("");
                    setAttempt((value) => value + 1);
                  }}
                >
                  Retry connection
                </button>
              </>
            )}
          </section>
        ) : !canAccess ? (
          <section className="panel access-panel">
            <span className="empty-icon">
              <Icon name="shield" size={28} />
            </span>
            <h1>Welcome to your workspace</h1>
            <p>Enter the operator code from your demo launcher to continue.</p>
            <form onSubmit={signIn}>
              <label htmlFor="operator-code">Operator code</label>
              <input
                id="operator-code"
                type="password"
                required
                autoComplete="current-password"
                maxLength={256}
                value={operatorCode}
                onChange={(event) => setOperatorCode(event.target.value)}
                disabled={authBusy}
              />
              {authError && (
                <p className="notice error" role="alert">
                  {authError}
                </p>
              )}
              <button
                className="button primary"
                disabled={authBusy || !operatorCode.trim()}
              >
                {authBusy ? "Signing in…" : "Sign in"}
              </button>
            </form>
          </section>
        ) : page === "Dashboard" ? (
          renderDashboard()
        ) : page === "Live Call" ? (
          <>
            <div className="call-mode-switch" role="group" aria-label="Live call mode">
              <button className={`button ${callMode === "call" ? "primary" : "secondary"}`} aria-pressed={callMode === "call"} disabled={active || busy || callBusy} onClick={() => setCallMode("call")}>Call another device</button>
              <button className={`button ${callMode === "microphone" ? "primary" : "secondary"}`} aria-pressed={callMode === "microphone"} disabled={active || busy || callBusy} onClick={() => setCallMode("microphone")}>Test my microphone</button>
            </div>
            {(active || busy || callBusy) && <p className="call-mode-note">End the current session before switching modes.</p>}
            {callMode === "microphone" && renderLive()}
          </>
        ) : page === "Verdict" ? (
          renderVerdict()
        ) : (
          renderAudit()
        )}
        {canAccess && <div hidden={page !== "Live Call" || callMode !== "call"}><CallWorkspace embedded onBusyChange={setCallBusy} /></div>}
      </main>
      <footer className="app-footer">
        <Icon name="wave" size={14} />
        <span>Voice Integrity</span>
        <span>Clarity in every conversation.</span>
      </footer>
      {newSession && (
        <Modal
          title="Start a new voice session"
          onClose={() => {
            if (!busy) setNewSession(false);
          }}
        >
          <p className="modal-description">
            Bring your conversation into focus with live voice analysis.
          </p>
          <div className="source-choice">
            <span className="empty-icon">
              <Icon name="mic" size={27} />
            </span>
            <h3>Use your microphone</h3>
            <p>
              Your browser will ask for microphone access. Audio is sent to your
              configured analysis backend.
            </p>
            <Badge tone="">Live audio source</Badge>
          </div>
          {demo && (
            <p className="notice">
              This backend uses a simulated detector. Scores are for
              demonstration.
            </p>
          )}
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
          <div className="modal-actions">
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => setNewSession(false)}
            >
              Cancel
            </button>
            <button
              className="button primary"
              disabled={busy}
              onClick={startSession}
            >
              <Icon name="mic" size={17} />
              {busy ? "Connecting…" : "Start session"}
            </button>
          </div>
        </Modal>
      )}
      {gate && (
        <Modal
          title="High synthetic-speech risk"
          onClose={() => setGate(false)}
        >
          <p className="modal-description">
            Pause sensitive actions and independently verify the caller. The
            risk assessment does not automatically end your session.
          </p>
          <div className="notice error">
            {score?.decision?.reason ||
              "The policy recommends gating sensitive actions."}
          </div>
          <div className="modal-actions">
            <button className="button secondary" onClick={() => setGate(false)}>
              Continue monitoring
            </button>
            <button
              className="button primary"
              onClick={endSession}
              disabled={busy}
            >
              End session
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

export default function RootApp() {
  return window.location.pathname.startsWith("/join/") ? (
    <CallWorkspace />
  ) : (
    <App />
  );
}
