-- migrations/sql/003_scoped_callbacks.sql
-- PHASE 3: Callback context store + Result state extension
-- Safe to run on existing DB: все изменения additive (ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT EXISTS)

-- ── 1. Расширяем таблицу results ─────────────────────────────────────────────

ALTER TABLE results
    ADD COLUMN IF NOT EXISTS keyboard_version   INT     NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS completed_actions  JSONB   NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS allowed_actions    JSONB,
    ADD COLUMN IF NOT EXISTS parent_result_id   BIGINT  REFERENCES results(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS source_input       TEXT,
    ADD COLUMN IF NOT EXISTS metadata_json      JSONB,
    ADD COLUMN IF NOT EXISTS updated_at         TIMESTAMPTZ DEFAULT NOW();

-- Индекс для parent chain
CREATE INDEX IF NOT EXISTS idx_results_parent
    ON results (parent_result_id)
    WHERE parent_result_id IS NOT NULL;

-- Индекс для быстрого lookup по id + user_id (используется в get_result_kb_state)
CREATE INDEX IF NOT EXISTS idx_results_id_user
    ON results (id, user_id);

-- ── 2. Создаём таблицу callback_contexts ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS callback_contexts (
    token           TEXT        PRIMARY KEY,           -- 8-char hex
    user_id         BIGINT      NOT NULL,
    tool_id         TEXT        NOT NULL,
    result_id       BIGINT      NOT NULL,              -- soft ref (no FK — result может быть удалён)
    action          TEXT        NOT NULL,
    keyboard_version INT        NOT NULL DEFAULT 1,
    metadata_json   JSONB,
    allow_repeat    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_cb_user_result
    ON callback_contexts (user_id, result_id);

CREATE INDEX IF NOT EXISTS idx_cb_expires
    ON callback_contexts (expires_at)
    WHERE used_at IS NULL;

-- ── 3. TTL-очистка (опционально — запускать через pg_cron или вручную) ────────
-- DELETE FROM callback_contexts WHERE expires_at < NOW() - INTERVAL '7 days';
