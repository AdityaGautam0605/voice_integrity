import ThemeToggle from "./ThemeToggle";
import AudioHistory from "./AudioHistory";
import { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { CallClient } from "./services/call";
import {
  checkBackend,
  checkOperator,
  loginOperator,
  logoutOperator,
  verifyVerdict,
} from "./services/api";
import Enrollment from "./Enrollment";
import "./App.css";

const invitation = () => {
  const match = window.location.pathname.match(/^\/join\/([^/]+)$/);
  return match
    ? {
        call_id: decodeURIComponent(match[1]),
        token:
          new URLSearchParams(window.location.hash.slice(1)).get("token") || "",
      }
    : null;
};
export default function CallWorkspace({ embedded = false, onBusyChange } = {}) {
  const [invite] = useState(invitation);
  const [access, setAccess] = useState(null);
  const [health, setHealth] = useState(null);
  const [online, setOnline] = useState(false);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [data, setData] = useState({
    callState: "Ready",
    analysisState: "Waiting",
    audioLevel: 0,
  });
  const [scenario, setScenario] = useState("");
  const [relay, setRelay] = useState(false);
  const [qr, setQr] = useState("");
  const [history, setHistory] = useState([]);
  const [levels, setLevels] = useState(Array(40).fill(3));
  const [verification, setVerification] = useState(null);
  const [tamper, setTamper] = useState("");
  const [starting, setStarting] = useState(false);
  const [authBusy, setAuthBusy] = useState(false);
  const audio = useRef(null);
  const client = useRef(null);
  const generation = useRef(0);
  const active = !["Ready", "Ended"].includes(data.callState);
  useEffect(() => { onBusyChange?.(active || starting); }, [active, starting, onBusyChange]);
  const allowed =
    invite || (access && (!access.online_mode || access.authenticated));
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const value = await checkBackend();
        if (!cancelled) {
          setHealth(value);
          setOnline(true);
        }
      } catch {
        if (!cancelled) setOnline(false);
      }
    };
    if (!invite)
      checkOperator()
        .then((value) => {
          if (!cancelled) setAccess(value);
        })
        .catch((err) => {
          if (!cancelled) setError(err.message);
        });
    const expired = () => {
      setAccess({ online_mode: true, authenticated: false });
      void client.current?.end();
      setCode("");
    };
    window.addEventListener("vif:operator-auth-required", expired);
    void refresh();
    const timer = setInterval(refresh, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
      window.removeEventListener("vif:operator-auth-required", expired);
      void client.current?.end();
    };
  }, [invite]);
  useEffect(() => {
    let cancelled = false;
    if (data.inviteUrl)
      QRCode.toDataURL(data.inviteUrl)
        .then((value) => {
          if (!cancelled) setQr(value);
        })
        .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [data.inviteUrl]);
  useEffect(() => {
    let cancelled = false;
    if (data.verdict)
      verifyVerdict(data.verdict)
        .then((value) => {
          if (!cancelled) setVerification(value);
        })
        .catch((err) => {
          if (!cancelled)
            setVerification({ valid: false, reason: err.message });
        });
    return () => {
      cancelled = true;
    };
  }, [data.verdict]);
  async function start() {
    if (starting || active) return;
    const run = ++generation.current;
    setStarting(true);
    setHistory([]);
    setError("");
    setVerification(null);
    setTamper("");
    setData({
      callState: "Requesting microphone",
      analysisState: "Waiting",
      audioLevel: 0,
    });
    const connection = new CallClient(audio.current, (update) => {
      if (generation.current !== run) return;
      if (update.audioLevel != null)
        setLevels((previous) => [
          ...previous.slice(1),
          3 + update.audioLevel * 92,
        ]);
      if (update.callState === "Ended") setLevels(Array(40).fill(3));
      setData((previous) => ({
        ...previous,
        ...update,
        ...(update.analysisState === "Unavailable" ? { score: null } : {}),
      }));
      if (update.analysisState === "Unavailable") setHistory([]);
      else if (update.score) setHistory(previous => [...previous.slice(-39), update.score.spoof_probability]);
      if (update.error) setError(update.error);
    });
    client.current = connection;
    try {
      await connection.start({
        invitation: invite,
        scenario,
        forceRelay: relay,
        online: health?.online_mode,
      });
    } catch (err) {
      setError(
        err.name === "NotAllowedError"
          ? "Microphone permission was denied. Allow access and try again."
          : err.message,
      );
      await connection.end();
    } finally {
      setStarting(false);
    }
  }
  async function signIn(event) {
    event.preventDefault();
    setAuthBusy(true);
    setError("");
    try {
      await loginOperator(code.trim());
      setAccess(await checkOperator());
      setCode("");
    } catch (err) {
      setError(err.message);
    } finally {
      setAuthBusy(false);
    }
  }
  async function signOut() {
    try {
      await logoutOperator();
      if (embedded) window.dispatchEvent(new Event("vif:operator-auth-required"));
      setAccess({ online_mode: true, authenticated: false });
      setData({ callState: "Ready", audioLevel: 0 });
      setVerification(null);
    } catch (err) {
      setError(err.message);
    }
  }
  async function testTamper() {
    try {
      const altered = {
        ...data.verdict,
        payload: {
          ...data.verdict.payload,
          spoof_probability:
            data.verdict.payload.spoof_probability === 0.99 ? 0.01 : 0.99,
        },
      };
      const result = await verifyVerdict(altered);
      setTamper(
        (result.signature_valid ?? result.valid)
          ? "Unexpected verification success"
          : "Altered verdict rejected",
      );
    } catch (err) {
      setTamper(err.message);
    }
  }
  const final = data.verdict?.payload;
  const integrity = verification?.signature_valid ?? verification?.valid;
  return (
    <div className={embedded ? "embedded-call" : "app"}>
      {!embedded && <header className="app-header">
        <a className="brand" href="/">
          Voice Integrity
        </a>
        <ThemeToggle />
        <span>{online ? "Backend ready" : "Backend unavailable"}</span>
        {!invite && access?.online_mode && access.authenticated && (
          <button
            className="button secondary"
            disabled={active || starting}
            onClick={signOut}
          >
            Sign out
          </button>
        )}
      </header>}
      <div className="page live-page">
        <div className="page-heading">
          <div>
            <h1>{invite ? "Join voice call" : "Live Call"}</h1>
            <p>
              {invite
                ? "Your voice is analyzed by the operator’s workspace."
                : "Talk to a caller on another device. Only the received caller audio is scored for spoofing."}
            </p>
          </div>
        </div>
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
        {!allowed ? (
          <section className="panel access-panel">
            <h2>Sign in to create a call</h2>
            {access ? (
              <form onSubmit={signIn}>
                <label>
                  Operator access code
                  <input
                    type="password"
                    value={code}
                    onChange={(event) => setCode(event.target.value)}
                    required
                    autoComplete="current-password"
                  />
                </label>
                <button className="button primary" disabled={authBusy}>
                  Sign in
                </button>
              </form>
            ) : (
              <p>Checking access…</p>
            )}
          </section>
        ) : (
          <>
            {!embedded && health?.demo_mode && (
              <p className="notice">
                Demo · simulated detector. Scores are not detection evidence.
              </p>
            )}
            <div className={invite ? "live-workspace caller-workspace" : "live-workspace"}>
              <section className="panel sources-panel call-setup">
                <div className="panel-heading"><h2>Setup</h2></div>
                <div className="panel-body call-source-body">
                  <div className="source-device"><div><strong>{invite ? "Your microphone" : "Remote caller"}</strong><small>{invite ? "Audio shared with the operator" : "Received audio is used for analysis"}</small></div></div>
                                  {!invite && (
                  <>
                    <label>
                      Detector mode
                      <select
                        value={scenario}
                        onChange={(event) => setScenario(event.target.value)}
                        disabled={active || starting}
                      >
                        <option value="">
                          Live audio · configured detector
                        </option>
                        {health?.demo_scenarios && (
                          <>
                            <option value="GREEN">
                              GREEN policy rehearsal
                            </option>
                            <option value="AMBER">
                              AMBER policy rehearsal
                            </option>
                            <option value="RED">RED policy rehearsal</option>
                          </>
                        )}
                      </select>
                    </label>
                    <label>
                      <input
                        type="checkbox"
                        checked={relay}
                        onChange={(event) => setRelay(event.target.checked)}
                        disabled={active || starting}
                      />{" "}
                      Force relay
                    </label>
                  </>
                )}

                                {data.inviteUrl && (
                <div className="invitation">
                  <label>
                    Caller invitation URL
                    <input readOnly value={data.inviteUrl} />
                  </label>
                  {qr && (
                    <img
                      src={qr}
                      alt="Scan to join this call"
                      width="160"
                      height="160"
                    />
                  )}
                  <p className="subtle">
                    Share privately. For a phone, open the operator page using
                    your trusted HTTPS LAN or public address before creating the
                    invitation.
                  </p>
                </div>
              )}

                  {!invite && !data.inviteUrl && <p className="subtle">Create a call to get a private invitation link and QR code.</p>}
                                {["Direct", "Relayed"].includes(data.connection?.route) && (
                    <p>
                      Connection route: <span aria-label="Connection route">{data.connection.route}</span>
                    </p>
                  )}

                </div>
                <div className="panel-footnote">Share invitations only with your intended caller.</div>
              </section>
              <section className="panel analysis-panel">
                <div className="panel-heading"><h2>Audio &amp; controls</h2><span className="subtle" role="status">{data.callState}</span></div>
                <div className="panel-body">
                  <audio ref={audio} autoPlay playsInline />
                  <div className="waveform-area lavender">
                    <div className="waveform-title"><span>Live audio</span><span>{active ? "Listening" : "Standby"}</span></div>
                    <div className="waveform" aria-label="Received caller audio level">{levels.map((height, i) => <i key={i} style={{ height: height + "%" }} />)}</div>
                    <span className="waveform-caption">{active ? "Audio received from the other device" : "Create or join a call to see received audio here"}</span>
                  </div>
                  <label className="call-audio-level">Received audio<meter min="0" max="1" value={data.audioLevel || 0} /></label>
                  {!invite && <AudioHistory history={history} score={data.score} />}
                                {data.playbackBlocked && (
                <button
                  className="button secondary"
                  onClick={() => client.current?.play()}
                >
                  Enable audio playback
                </button>
              )}

                  {data.session && active && !invite && <Enrollment key={data.session.session_id} sessionId={data.session.session_id} disabled={data.analysisState === "Unavailable"} />}
                </div>
                <div className="call-controls">                {!active && (
                  <button
                    className="button primary"
                    disabled={starting || !online || (invite && !invite.token)}
                    onClick={start}
                  >
                    {invite
                      ? "Join call"
                      : data.callState === "Ended"
                        ? "Create another call"
                        : "Create call"}
                  </button>
                )}
                {active && (
                  <>
                    <button
                      className="button primary"
                      onClick={() => client.current?.end()}
                    >
                      End call
                    </button>
                    <button
                      className="button secondary"
                      onClick={() => client.current?.mute(!data.muted)}
                    >
                      {data.muted ? "Unmute microphone" : "Mute microphone"}
                    </button>
                  </>
                )}</div>
              </section>
            {!invite && (
              <section className="panel signals-panel">
                <div className="panel-heading"><h2>Analysis</h2></div>
                <div className="panel-body">
                <p role="status">{data.analysisState}</p>
                {data.analysisError && (
                  <p className="notice error">{data.analysisError}</p>
                )}
                <div className="call-signal-list">
                  <div className={`signal-tile ${data.score?.risk === "RED" ? "rose" : data.score?.risk === "AMBER" ? "peach" : data.score ? "mint" : "neutral"}`}>
                    <div>
                      <span>Spoof probability</span>
                      <strong>
                        {data.score
                          ? `${Math.round(data.score.spoof_probability * 100)}%`
                          : "Unavailable"}
                      </strong>
                      <h3>
                        {data.score?.decision?.action || "Awaiting analysis"}
                      </h3>
                    </div>
                  </div>
                  <div className="signal-tile blue">
                    <div>
                      <span>Speaker</span>
                      <strong className="signal-word">
                        {data.score?.speaker_status || "Not assessed"}
                      </strong>
                    </div>
                  </div>
                  <div className="signal-tile lavender">
                    <div>
                      <span>Liveness evidence</span>
                      <strong className="signal-word">
                        {data.score?.liveness_score != null
                          ? data.score.liveness_score.toFixed(2)
                          : "Waiting for conversational turns"}
                      </strong>
                      <small>Experimental; separate from spoof risk.</small>
                    </div>
                  </div>
                </div>
                {data.score?.challenge && (
                  <p className="notice">
                    Manual verification prompt: {data.score.challenge.phrase}
                  </p>
                )}
                </div>
              </section>
            )}
            </div>
            {final && (
              <section className="panel panel-body">
                <h2>
                  {final.windows_scored === 0
                    ? "Insufficient speech"
                    : "Session verdict"}
                </h2>
                <p>
                  {final.action} · {final.risk}
                </p>
                <p className="record-id">{final.session_id}</p>
                <p>
                  {verification
                    ? integrity
                      ? "Signature verified"
                      : "Signature verification failed"
                    : "Verifying signature…"}
                </p>
                {verification && !verification.valid && (
                  <p className="notice">
                    {verification.reason}. This result must not authorize a new
                    action.
                  </p>
                )}
                <button className="button secondary" onClick={testTamper}>
                  Test an altered verdict
                </button>
                {tamper && <p role="status">{tamper}</p>}
              </section>
            )}
          </>
        )}
      </div>
    </div>
  );
}
