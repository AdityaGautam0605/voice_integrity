# Accounts, storage, and Redis proposal

Status: design only. The theme and operator sign-in UI are implemented; individual
accounts, PostgreSQL migration, and Redis are not enabled by this change.
The current backend uses one operator code, process-memory login sessions and
call rooms, SQLite signed audit records, and a separate encrypted voiceprint
vault. A new sign-in design does not create a user database.

## Proposed storage

Use PostgreSQL for durable account and workspace data. The companion
`user-storage-proposal.sql` gives initial tables, constraints, and indexes.

| Table | Stored data | Purpose |
| --- | --- | --- |
| users | ID, normalized email, display name, Argon2id password hash, account status, verification timestamp, theme, timestamps | Individual identity and preferences |
| workspaces | ID, name, creation time | Group calls and records under an organization |
| workspace_memberships | Workspace/user IDs, role | Owner/operator/viewer authorization |
| auth_sessions | User ID, hash of random session token, expiry, revocation | Persistent login, logout and session revocation |
| voice_sessions | Workspace/session IDs, operator, mode, start/end, status, model version, simulation flag | Call/microphone history, without recordings |
| verdicts | Original signed envelope, indexed risk/action/score fields, speaker status, liveness evidence, key ID, timestamp | Searchable analysis results with signature preservation |
| account_events | Actor, event type, target and timestamp | Account and membership change history |

The frontend currently stores only `vif-theme` (light/dark) in browser storage.
For future accounts, load the user preference after sign-in and persist changes
to `users.theme`; honor the system preference when no explicit choice exists.

Passwords must be hashed on the backend using a maintained Argon2id library;
the encoded hash contains parameters and salt. Never store passwords, operator
codes, access tokens or credentials in browser storage, logs, or cache entries.
Set a random login token in a Secure, HttpOnly cookie and store only its SHA-256
hash in `auth_sessions`. Use CSRF/origin checks for state-changing requests.
Normalize email consistently and update `updated_at` on writes.

Each backend request must authorize workspace membership before reading any
workspace row or cache entry. Never trust a workspace ID supplied by the browser.
Composite foreign keys keep verdict/session relationships within a workspace.
Add PostgreSQL row-level policies as defense in depth before production rollout;
this draft does not define those policies or application database privileges.
Use a restricted runtime role, not the schema owner's credentials.

Do not store raw audio, transcripts, waveform samples, plaintext embeddings,
private signing keys, or TURN/invitation credentials in these tables. Keep
voiceprints in the existing encrypted vault with tenant ownership and explicit
enrollment consent. Treat migrations of that vault separately. Caller identities
do not need user records simply to join an invitation.

The existing hash-chained audit `entries` and signed `checkpoints` must remain
intact. `account_events` is a separate activity log, not a replacement or a
tamper-evident log. Store verdict copies transactionally with a durable outbox
and idempotent worker if the audit writer remains in another database; do not
pretend writes to PostgreSQL and SQLite are a single transaction. Restrict
UPDATE/DELETE on finalized verdicts. Establish retention and account deletion
rules before collecting user data; disabling accounts preserves historical
references until the agreed retention process runs. Registration, email
verification and password recovery need separate expiring, hashed tokens and
delivery workflows if those features are introduced.

## Where Redis helps

First measure database query p50/p95, query counts, and index usage with realistic
record counts. The current demo may be faster with indexed local SQLite than a
network round trip to Redis. Redis does not speed up model inference or fix
WebSocket routing. Add it once repeated database reads are a measured cost.

Recommended first cached endpoint: workspace dashboard aggregates. Use cache-aside:

1. Authenticate and check current membership against the database.
2. Read `vif:v1:workspace:<id>:summary:<data-version>` from Redis.
3. On a miss, run an indexed SQL aggregate and cache the DTO for 15–30 seconds.
4. On finalized verdict writes, increment a durable workspace data version in
   the same database transaction, and publish invalidation using an outbox.
   Old versions become unreachable and expire, preventing stale fill races.
5. Bound Redis latency (for example 100 ms). On cache errors or corrupt values,
   fall back to the database; cache failure must not prevent normal reads.

The version field/outbox are a follow-up migration, intentionally absent from
the initial account schema until the cache is implemented. Add small randomized
TTL jitter and a short fill lock for popular keys if load testing shows a
stampede. Cap cache payload size and never let arbitrary searches create
unbounded long-lived entries. Use cursor pagination by `(created_at, id)` and
the proposed workspace/risk indexes for history; start without caching arbitrary
audit searches. For substring searches at scale, evaluate a trigram index after
measuring actual search patterns.

| Data | Initial cache policy |
| --- | --- |
| Dashboard counts and aggregate risks | 15–30 seconds, workspace/version scoped |
| Recent verdict list | Optional 15 seconds, include workspace, filters and cursor in key |
| Finalized signed envelope | Optional 5 minutes; authorization still checked for each request |
| Login sessions, roles, account status | Read authoritative state; avoid delayed revocation |
| Signature freshness/replay checks, live scores | Never reuse cached authorization or freshness decisions |
| Voiceprints, audio, passwords, tokens | Do not cache |

Deploy Redis privately near the backend, with TLS, ACL credentials supplied
through server-side secrets, memory limits and an eviction policy appropriate
for disposable cache entries. Do not expose Redis credentials to Vite. Cache
outage fallback is safe for read acceleration; a future security rate limiter
must define its own outage behavior instead of silently failing open.

Redis may later support shared rate limits and signaling pub/sub, but storing
call room JSON in Redis alone does not make WebSockets or inference state work
across multiple backend processes. Keep the current single-worker limit until
connection ownership, pub/sub, session state and routing are implemented.

## Implementation order

1. Provision PostgreSQL, finalize workspace roles and signup/invitation policy.
2. Add migrations, account login/logout, password hashing, authorization, and
   ownership tests. Keep the operator-code deployment working during migration.
3. Move/index durable session metadata and verdict history without changing
   the signed audit contract. Measure queries and persistence across restarts.
4. Add optional Redis dashboard caching, invalidation/outbox and fallback tests.
5. Verify that logout/role changes take effect immediately, workspaces cannot
   access each other's cached data, and Redis outages do not lose durable data.

References:
- https://redis.io/docs/latest/develop/use-cases/cache-aside/
- https://www.postgresql.org/docs/current/indexes.html
- https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
