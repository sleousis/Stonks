-- Auth and principals (roadmap 15.2 / 13.1 backend, step S2 of
-- docs/design/accounts-and-modes.md).
--
-- Secrets are never stored in the clear: session ids, CSRF tokens, API
-- tokens and recovery codes are random, so only their SHA-256 is kept;
-- passwords use argon2id (users.password_hash, migration 010); the TOTP
-- secret is sealed with SecretBox (users.totp_secret_enc). Only stonks.auth
-- writes these tables.

-- ---- users: second-factor state -------------------------------------------
-- totp_secret_enc may hold a pending secret during enrolment; the factor is
-- live only once mfa_enrolled_at is set.
ALTER TABLE users ADD COLUMN mfa_enrolled_at TEXT;
-- Last accepted TOTP time step: a code is never accepted twice.
ALTER TABLE users ADD COLUMN totp_last_step INTEGER;
ALTER TABLE users ADD COLUMN password_changed_at TEXT;

-- ---- sessions (browser) ---------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    id_hash         TEXT PRIMARY KEY,                   -- sha256 of the cookie value
    user_id         TEXT NOT NULL REFERENCES users(id),
    csrf_hash       TEXT NOT NULL,                      -- sha256 of the CSRF token
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    expires_at      TEXT NOT NULL,                      -- absolute limit
    mfa_verified_at TEXT,                               -- NULL = second factor still due
    ip              TEXT,
    user_agent      TEXT,
    revoked_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, revoked_at);

-- ---- api_tokens (scripts, MCP) --------------------------------------------
CREATE TABLE IF NOT EXISTS api_tokens (
    id           TEXT PRIMARY KEY,                      -- the <id> in stk_<id>_<secret>
    user_id      TEXT NOT NULL REFERENCES users(id),
    name         TEXT NOT NULL CHECK (length(trim(name)) > 0),
    token_hash   TEXT NOT NULL UNIQUE,                  -- sha256 of the whole token
    scopes       TEXT NOT NULL,                         -- JSON list: read, trade, lab, admin
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    expires_at   TEXT,                                  -- NULL = no expiry
    revoked_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id, revoked_at);

-- ---- recovery_codes -------------------------------------------------------
CREATE TABLE IF NOT EXISTS recovery_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL REFERENCES users(id),
    code_hash  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    used_at    TEXT,
    UNIQUE (user_id, code_hash)
);

-- ---- login_attempts (rate limit and lockout) ------------------------------
-- email is stored lowercased as typed, so unknown accounts are limited too.
CREATE TABLE IF NOT EXISTS login_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT,
    ip         TEXT,
    stage      TEXT NOT NULL CHECK (stage IN ('password', 'mfa')),
    success    INTEGER NOT NULL CHECK (success IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_email ON login_attempts(email, created_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, created_at);
