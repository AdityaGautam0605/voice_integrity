async function request(path, body, operator = true) {
  const response = await fetch(`/api${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(15000),
    credentials: 'same-origin',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const failure = new Error(typeof error.detail === 'string' ? error.detail : `Request failed (${response.status})`);
    failure.status = response.status;
    if (operator && [401, 403].includes(response.status)) {
      window.dispatchEvent(new Event('vif:operator-auth-required'));
    }
    throw failure;
  }
  return response.json();
}
export const checkBackend = () => request('/health', undefined, false);
export const checkOperator = () => request('/v1/operator', undefined, false);
export const loginOperator = (code) => request('/v1/operator/login', { code }, false);
export const logoutOperator = () => request('/v1/operator/logout', {}, false);
export const createCall = (forceRelay = false) => request('/v1/calls', { force_relay: forceRelay });
export const getCallIce = (id, role, token) => request(`/v1/calls/${encodeURIComponent(id)}/ice`, { role, token }, false);
export const createSession = (scenario) => request('/v1/session', { demo_scenario: scenario || null });
export const getVerdict = (id) => request(`/v1/verdict/${id}`);
export const getAudit = () => request('/v1/audit');
export const verifyVerdict = (verdict) => request('/v1/verdict/verify', {
  payload: verdict.payload, algorithm: verdict.algorithm,
  key_id: verdict.key_id, signature: verdict.signature,
});
