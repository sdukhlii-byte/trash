"""
migrations/runner.py — лёгкий мигратор без Alembic.

Принцип:
  - Миграции — это пронумерованные SQL-файлы: 001_init.sql, 002_add_column.sql
  - При старте приложения runner.run() применяет только новые
  - Уже применённые хранятся в таблице schema_migrations
  - Идемпотентны: если миграция упала — можно перезапустить

Использование в main.py:
    from migrations.runner import run as run_migrations
    await run_migrations()
"""
import logging
import os

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__))


async def run() -> None:
    """Применяет все новые миграции из папки migrations/sql/."""
    from db import _get_pool
    pool = _get_pool()

    async with pool.acquire() as conn:
        # Создаём таблицу учёта если нет
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Читаем уже применённые
        applied = {r["version"] for r in
                   await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")}

        # Читаем SQL-файлы из migrations/sql/
        sql_dir = os.path.join(_MIGRATIONS_DIR, "sql")
        if not os.path.exists(sql_dir):
            return

        files = sorted(f for f in os.listdir(sql_dir) if f.endswith(".sql"))
        new_count = 0

        for filename in files:
            version = filename.replace(".sql", "")
            if version in applied:
                continue

            sql_path = os.path.join(sql_dir, filename)
            sql = open(sql_path).read().strip()
            if not sql:
                continue

            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)", version
                    )
                logger.info(f"[migration] applied: {version}")
                new_count += 1
            except Exception as e:
                logger.error(f"[migration] FAILED {version}: {e}")
                raise  # останавливаем при ошибке — безопаснее чем пропускать

        if new_count:
            logger.info(f"[migration] {new_count} migration(s) applied")
        else:
            logger.debug("[migration] schema up to date")
