async function request(path, body, operator = true) {
  const response = await fetch(`/api${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(15000),
    credentials: "same-origin",
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const failure = new Error(
      typeof error.detail === "string"
        ? error.detail
        : `Request failed (${response.status})`,
    );
    failure.status = response.status;
    if (operator && [401, 403].includes(response.status)) {
      window.dispatchEvent(new Event("vif:operator-auth-required"));
    }
    throw failure;
  }
  return response.json();
}
export const checkBackend = () => request("/health", undefined, false);
export const checkOperator = () => request("/v1/operator", undefined, false);
export const loginOperator = (code) =>
  request("/v1/operator/login", { code }, false);
export const logoutOperator = () => request("/v1/operator/logout", {}, false);
export const createCall = (forceRelay = false) =>
  request("/v1/calls", { force_relay: forceRelay });
export const getCallIce = (id, role, token) =>
  request(`/v1/calls/${encodeURIComponent(id)}/ice`, { role, token }, false);
export const createSession = (scenario, enableLiveness = false) =>
  request("/v1/session", {
    demo_scenario: scenario || null,
    enable_liveness: enableLiveness,
  });
export const getVerdict = (id) => request(`/v1/verdict/${id}`);
export const getAudit = ({ offset = 0, q = "", risk = "" } = {}) =>
  request(
    `/v1/audit?${new URLSearchParams({ limit: 50, offset, q, risk: risk === "ALL" ? "" : risk })}`,
  );
export const enrollAudio = (id, speakerId, pcm) =>
  request(`/v1/enroll/${encodeURIComponent(id)}/audio`, {
    speaker_id: speakerId,
    pcm_b64: pcm,
  });
export const revokeSpeaker = (id, speakerId) =>
  request(
    `/v1/enroll/${encodeURIComponent(id)}/revoke?speaker_id=${encodeURIComponent(speakerId)}`,
    {},
  );
export const verifyVerdict = (verdict) =>
  request("/v1/verdict/verify", {
    payload: verdict.payload,
    algorithm: verdict.algorithm,
    key_id: verdict.key_id,
    signature: verdict.signature,
  });
