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
  const [state, setState] = useState(initial);
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState('');
  const [scenario, setScenario] = useState('');
  const [forceRelay, setForceRelay] = useState(false);
  const [operator, setOperator] = useState(null);
  const [accessCode, setAccessCode] = useState('');
  const [authError, setAuthError] = useState('');
  const [authBusy, setAuthBusy] = useState(false);
  const [qr, setQr] = useState('');
  const [verification, setVerification] = useState(null);
  const [audit, setAudit] = useState(null);
  const [resultError, setResultError] = useState('');
  const [tamper, setTamper] = useState(null);
  const [copied, setCopied] = useState(false);
  const audio = useRef(null);
  const client = useRef(null);
  const runId = useRef(0);
  const caller = Boolean(invitation);
  const active = !['Ready', 'Ended'].includes(state.callState);
  const online = Boolean(health?.online_mode);
  const needsSignIn = online && !caller && !operator?.authenticated;
  const demo = state.session?.demo_mode ?? health?.demo_mode;
  const fresh = ['Live', 'Collecting speech'].includes(state.analysisState);
  const score = state.score;
  const speech = Math.max(state.progress?.speech_seconds ?? 0, score?.speech_seconds ?? 0);
  const windows = Math.max(state.progress?.windows_scored ?? 0, score ? score.sequence + 1 : 0);

  useEffect(() => {
    let cancelled = false;
    const refresh = () => checkBackend().then(async (data) => {
      if (!cancelled) { setHealth(data); setHealthError(''); }
      if (data.online_mode && !caller) {
        try {
          const access = await checkOperator();
          if (!cancelled) { setOperator(access); }
        } catch (error) {
          if (!cancelled) {
            if ([401, 403].includes(error.status)) setOperator({ authenticated: false });
            setAuthError(error.message);
          }
        }
      }
    }).catch((error) => { if (!cancelled) setHealthError(error.message); });
    void refresh();
    const timer = setInterval(refresh, 10000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [caller]);

  useEffect(() => {
    const expired = () => {
      if (online && !caller) {
        setOperator({ authenticated: false });
        setAuthError('Operator access has expired or was rejected. Sign in again.');
      }
    };
    window.addEventListener('vif:operator-auth-required', expired);
    return () => window.removeEventListener('vif:operator-auth-required', expired);
  }, [online, caller]);

  useEffect(() => {
    let cancelled = false;
    if (state.inviteUrl) QRCode.toDataURL(state.inviteUrl, { width: 220, margin: 2 })
      .then((url) => { if (!cancelled) setQr(url); })
      .catch(() => { if (!cancelled) setQr(''); });
    return () => { cancelled = true; };
  }, [state.inviteUrl]);

  useEffect(() => {
    let cancelled = false;
    if (state.verdict && !needsSignIn) {
      Promise.all([verifyVerdict(state.verdict), getAudit()]).then(([check, log]) => {
        if (!cancelled) { setVerification(check); setAudit(log); setResultError(''); }
      }).catch((error) => { if (!cancelled) setResultError(error.message); });
    }
    return () => { cancelled = true; };
  }, [state.verdict, needsSignIn]);

  useEffect(() => {
    const dispose = () => { void client.current?.end(); };
    window.addEventListener('pagehide', dispose);
    return () => { window.removeEventListener('pagehide', dispose); dispose(); };
  }, []);

  async function start() {
    const id = ++runId.current;
    setState(initial);
    setVerification(null); setAudit(null); setResultError(''); setTamper(null); setQr(''); setCopied(false);
    const current = new CallClient(audio.current, (patch) => {
      if (id !== runId.current) return;
      setState((previous) => ({ ...previous, ...patch,
        history: patch.score ? [...previous.history, patch.score].slice(-90) : previous.history }));
    });
    client.current = current;
    try { await current.start({ invitation, scenario, forceRelay: online && forceRelay, online }); }
    catch (error) {
      await current.end();
      setState((previous) => ({ ...previous, error: error.name === 'NotAllowedError'
        ? 'Microphone permission was denied. Allow access in the address bar, then try again.' : error.message }));
    }
  }

  async function signIn(event) {
    event.preventDefault();
    setAuthBusy(true); setAuthError('');
    try {
      const access = await loginOperator(accessCode.trim());
      setOperator(access); setAccessCode('');
    } catch (error) { setAuthError(error.message); }
    finally { setAuthBusy(false); }
  }

  async function signOut() {
    setAuthBusy(true); setAuthError('');
    try {
      await logoutOperator();
      setOperator({ authenticated: false });
      setState(initial); setVerification(null); setAudit(null); setResultError(''); setTamper(null);
    } catch (error) { setAuthError(error.message); }
    finally { setAuthBusy(false); }
  }

  async function tamperCheck() {
    try {
      const altered = structuredClone(state.verdict);
      altered.payload.spoof_probability = altered.payload.spoof_probability === 0 ? 1 : 0;
      setTamper(await verifyVerdict(altered));
    } catch (error) { setResultError(error.message); }
  }

  const final = state.verdict?.payload;
  const entry = audit?.entries.find((item) => item.payload.payload.session_id === final?.session_id);
  const actionText = {
    PROCEED: 'Policy recommends continuing. Live scores are advisory.',
    CHALLENGE: 'Ask the caller to repeat the phrase below and verify manually.',
    GATE_ACTION: 'Pause sensitive actions and verify the caller. The conversation stays connected.',
  };

  return <div className="app-shell">
    <header className="topbar">
      <a className="brand" href="/" aria-label="Voice Integrity home"><span className="brand-mark">V</span><span>VOICE <b>INTEGRITY</b></span></a>
      <div className="operator-controls"><span className="role-label">{caller ? 'CALLER DEVICE' : 'OPERATOR CONSOLE'}{online ? ' · ONLINE' : ''}</span>
        {online && !caller && operator?.authenticated && <button className="secondary" disabled={active || authBusy} onClick={signOut}>Sign out</button>}
      </div>
    </header>
    <main>
      <div className="intro"><div><p className="eyebrow">TWO DEVICES · ONE CONVERSATION</p>
        <h1>{caller ? 'Join the conversation.' : 'Hear the call. See the evidence.'}</h1>
        <p>{caller ? 'Connect your microphone and talk to the operator. Keep this page open during the call.'
          : 'A live audio call with a separate voice integrity pipeline.'}</p></div>
        <span className={`badge ${healthError ? 'unavailable' : ''}`}>{healthError ? 'Backend unavailable' : health ? 'Backend ready' : 'Checking backend'}</span>
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
          {state.analysisError && <div className="alert" role="alert">{state.analysisError}</div>}
          {(state.droppedChunks > 0 || state.progress?.windows_dropped > 0) && <p className="warning-text">Analysis overload: {state.droppedChunks} audio chunks and {state.progress?.windows_dropped ?? 0} windows dropped.</p>}
          <div className="model-footer"><span>Model: {state.session?.model_version || health?.model_version || '—'}</span><span>VAD: {state.session?.vad || health?.vad || '—'}</span><span>Speaker: {score?.speaker_status || 'NOT_ENROLLED'}</span><span>Liveness: disabled</span><span>{score?.calibrated ? 'Calibrated' : 'Uncalibrated'}</span></div>
        </section>}
      </div>
      {!caller && <section className="card result-card">
        <div className="section-title"><span className="eyebrow">03 / SIGNED VERDICT & AUDIT</span><span className="badge">{verification ? verification.valid ? 'Signature verified' : 'Verification failed' : final ? 'Verifying' : 'Available after call'}</span></div>
        {!final ? <p>End the call to sign the final result and verify its audit record.</p> : <>
          <div className="result-grid"><div><h2>{final.windows_scored ? final.action : 'Insufficient speech'}</h2><p>{final.windows_scored ? `${final.risk} · ${Math.round(final.spoof_probability * 100)} / 100` : 'No speech window was scored. This is not evidence of safety.'}</p><p>{final.speech_seconds.toFixed(1)} seconds of speech · {final.windows_scored} windows</p></div>
            <div><b>{state.verdict.algorithm} signature</b><p>{verification?.reason || 'Checking signature…'}</p><b>Audit chain</b><p>{audit ? `${audit.chain_valid ? 'Valid' : 'Invalid'} · ${audit.count} entries` : 'Checking audit…'}</p></div></div>
          <div className="record-id">Session {final.session_id}<br />Model {final.model_version}<br />{entry ? `Audit entry #${entry.seq} · ${entry.entry_hash}` : 'Matching audit entry not yet loaded'}</div>
          <button className="secondary" onClick={tamperCheck}>Test an altered verdict</button>
          {tamper && <p className={tamper.valid ? 'warning-text' : 'success-text'}>{tamper.valid ? 'Unexpected: altered verdict accepted' : 'Altered verdict rejected'} · {tamper.reason}</p>}
          <details><summary>Inspect signed result</summary><pre>{JSON.stringify(state.verdict, null, 2)}</pre></details>
        </>}
        {resultError && <div className="alert">Verification unavailable: {resultError}</div>}
      </section>}
      </>}
      <footer>Voice Integrity <span>Call audio → speech windows → detector → policy → signed evidence</span></footer>
    </main>
  </div>;
}
export default App;
