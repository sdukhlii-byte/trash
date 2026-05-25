"""
chat_context.py — контекст чата Миры.

Инжектирует последние 2-3 сгенерированных результата в system prompt чата.

Проблема которую решаем:
  Пользователь: "улучши карусель что написала"
  Мира: "Не вижу никакой карусели — опиши о чём речь"
  → Пользователь злится, уходит.

Решение:
  Если у пользователя есть сохранённые результаты — последние 2 добавляются
  в system prompt как [КОНТЕКСТ]. Мира знает что было создано.
"""
import logging

from db import get_results

logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 600   # обрезаем каждый результат чтобы не раздувать контекст


async def build_chat_system(base_system: str, user_id: int) -> str:
    """
    Возвращает system prompt с инжектированными последними результатами.
    Если результатов нет — возвращает base_system без изменений.
    """
    try:
        results = await get_results(user_id, limit=3)
    except Exception as e:
        logger.warning(f"chat_context: could not load results for {user_id}: {e}")
        return base_system

    if not results:
        return base_system

    # Берём последние 2 результата (не 3 — экономим токены)
    recent = results[:2]
    context_lines = ["\n\n[КОНТЕКСТ — последние материалы которые ты создала для пользователя]"]
    for r in recent:
        ts      = r["ts"][:10] if r["ts"] else ""
        preview = r["content"][:_MAX_RESULT_CHARS].replace("\n", " ")
        if len(r["content"]) > _MAX_RESULT_CHARS:
            preview += "..."
        context_lines.append(
            f"\n{r['agent_name']} [{ts}]:\n{preview}"
        )
    context_lines.append(
        "\nЕсли пользователь ссылается на «тот пост», «карусель», «прогрев» — "
        "это скорее всего один из материалов выше. "
        "Используй контекст чтобы отвечать точно, без лишних уточнений."
    )

    return base_system + "".join(context_lines)
