-- 001_baseline.sql
-- Baseline: фиксируем текущую схему.
-- Все таблицы уже созданы через CREATE TABLE IF NOT EXISTS в db.py и lava_payments.py.
-- Эта миграция просто регистрирует точку отсчёта.

-- Добавляем индекс на subscriptions.user_id если нет (ускоряет get_user_access_state)
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_trials_user_id ON trials(user_id);
CREATE INDEX IF NOT EXISTS idx_results_user_id ON results(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_user_id_mode ON messages(user_id, mode);
