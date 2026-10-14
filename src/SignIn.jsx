export default function SignIn({ code, setCode, busy, error, onSubmit }) {
  return <section className="sign-in-page" aria-labelledby="sign-in-heading">
    <div className="sign-in-story">
      <span className="eyebrow">VOICE INTEGRITY</span>
      <h2>A clearer view of every conversation.</h2>
      <p>Connect with a caller, review voice signals, and keep a verifiable record of your decisions.</p>
      <div className="sign-in-art" aria-hidden="true">{[18,32,50,72,94,64,40,76,100,68,42,60,86,52,30,18].map((height,i)=><i key={i} style={{height: height + '%'}} />)}</div>
      <span className="sign-in-caption">Your workspace for voice analysis</span>
    </div>
    <div className="panel sign-in-card">
      <span className="eyebrow">OPERATOR ACCESS</span>
      <h1 id="sign-in-heading">Welcome to your workspace</h1>
      <p>Sign in with the operator code provided by your workspace administrator.</p>
      <form onSubmit={onSubmit}>
        <label htmlFor="operator-code">Operator code</label>
        <input id="operator-code" type="password" autoComplete="current-password" required maxLength={256} value={code} onChange={event=>setCode(event.target.value)} disabled={busy} aria-describedby={error ? 'sign-in-error' : 'sign-in-help'} />
        {error && <p id="sign-in-error" className="notice error" role="alert">{error}</p>}
        <button className="button primary" disabled={busy || !code.trim()}>{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>
      <p id="sign-in-help" className="sign-in-help">Need access? Ask the person who manages this workspace for your operator code.</p>
    </div>
  </section>;
}
