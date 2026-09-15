const API_BASE = "/api";

export async function checkBackend() {
  const response = await fetch(`${API_BASE}/health`);

  if (!response.ok) {
    throw new Error("Backend is not responding");
  }

  return response.json();
}

export async function createSession(speakerId = null) {
  const response = await fetch(`${API_BASE}/v1/session`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(
      speakerId ? { speaker_id: speakerId } : {}
    ),
  });

  if (!response.ok) {
    throw new Error(`Session creation failed: ${response.status}`);
  }

  return response.json();
}

export async function getVerdict(sessionId) {
  const response = await fetch(
    `${API_BASE}/v1/verdict/${sessionId}`
  );

  if (!response.ok) {
    throw new Error(`Verdict request failed: ${response.status}`);
  }

  return response.json();
}

export async function getAudit() {
  const response = await fetch(`${API_BASE}/v1/audit`);

  if (!response.ok) {
    throw new Error(`Audit request failed: ${response.status}`);
  }

  return response.json();
}