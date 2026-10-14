-- DESIGN PROPOSAL ONLY. Not an applied migration or a replacement for the
-- current SQLite audit log. PostgreSQL; application supplies random UUIDs.
BEGIN;
CREATE TABLE users (
    id uuid PRIMARY KEY,
    email text NOT NULL CHECK (email = lower(btrim(email))),
    display_name text NOT NULL,
    password_hash text NOT NULL, -- Argon2id PHC string, never plaintext
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    email_verified_at timestamptz,
    theme text NOT NULL DEFAULT 'system' CHECK (theme IN ('system','light','dark')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (email)
);
CREATE TABLE workspaces (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE workspace_memberships (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    user_id uuid NOT NULL REFERENCES users(id),
    role text NOT NULL CHECK (role IN ('owner','operator','viewer')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX membership_by_user ON workspace_memberships(user_id, workspace_id);
CREATE TABLE auth_sessions (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    CHECK (expires_at > created_at)
);
CREATE INDEX auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX auth_sessions_expiry ON auth_sessions(expires_at);
CREATE TABLE voice_sessions (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    id text NOT NULL, -- retain existing opaque backend session IDs
    operator_id uuid REFERENCES users(id),
    mode text NOT NULL CHECK (mode IN ('call','microphone')),
    status text NOT NULL CHECK (status IN ('active','ended','failed')),
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    model_version text,
    demo_mode boolean NOT NULL DEFAULT false,
    PRIMARY KEY (workspace_id, id),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX voice_sessions_recent ON voice_sessions(workspace_id, started_at DESC, id DESC);
CREATE TABLE verdicts (
    workspace_id uuid NOT NULL,
    session_id text NOT NULL,
    -- Exact signed representation must be retained. Never reconstruct signed
    -- bytes from jsonb: key order and numeric serialization may change.
    signed_envelope text NOT NULL,
    risk text NOT NULL CHECK (risk IN ('GREEN','AMBER','RED')),
    action text NOT NULL,
    spoof_probability double precision CHECK (spoof_probability BETWEEN 0 AND 1),
    speech_seconds double precision NOT NULL CHECK (speech_seconds >= 0),
    speaker_status text NOT NULL,
    liveness_score double precision,
    key_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, session_id),
    FOREIGN KEY (workspace_id, session_id) REFERENCES voice_sessions(workspace_id, id)
);
CREATE INDEX verdicts_recent ON verdicts(workspace_id, created_at DESC, session_id DESC);
CREATE INDEX verdicts_by_risk ON verdicts(workspace_id, risk, created_at DESC, session_id DESC);
CREATE TABLE account_events (
    id uuid PRIMARY KEY,
    workspace_id uuid REFERENCES workspaces(id),
    actor_user_id uuid REFERENCES users(id),
    event_type text NOT NULL,
    target_id text,
    occurred_at timestamptz NOT NULL DEFAULT now()
    -- Explicit metadata only: no credentials, tokens, audio, or embeddings.
);
CREATE INDEX account_events_recent ON account_events(workspace_id, occurred_at DESC, id DESC);
COMMIT;
