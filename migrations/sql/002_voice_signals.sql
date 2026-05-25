-- 002_voice_signals.sql
-- Persistent storage for voice learning data.
-- Previously stored only in Redis (volatile). Now Redis = cache, Postgres = source of truth.

CREATE TABLE IF NOT EXISTS voice_signals (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    kind        TEXT        NOT NULL,  -- 'approved', 'note', 'rejection'
    agent_key   TEXT        NOT NULL DEFAULT '',
    content     TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_voice_signals_user_id ON voice_signals(user_id);
CREATE INDEX IF NOT EXISTS idx_voice_signals_user_kind ON voice_signals(user_id, kind);
