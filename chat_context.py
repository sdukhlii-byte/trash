"""
chat_context.py — Mira chat context.

Injects:
  1. Last 2-3 generated results → Mira remembers what was created
  2. Voice context (approved samples, style notes, rejections) → Mira's
     chat replies also respect the user's trained style preferences

Problem solved:
  User: "improve the carousel you wrote"
  Mira: "I don't see any carousel — describe what you mean"
  → user frustrated, leaves.

Solution:
  Last 2 results + full voice context appended to system prompt.
"""
import logging

from db import get_results

logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 600


async def build_chat_system(base_system: str, user_id: int) -> str:
    """
    Returns system prompt with injected recent results AND voice context.
    Falls back to base_system unchanged on any error.
    """
    system = base_system

    # 1. Recent results context
    try:
        results = await get_results(user_id, limit=3)
        if results:
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
            system = system + "".join(context_lines)
    except Exception as e:
        logger.warning("chat_context: results load failed uid=%s: %s", user_id, e)

    # 2. Voice context — so chat replies also respect trained style
    try:
        from voice_learner import build_voice_context
        voice_ctx = await build_voice_context(user_id)
        if voice_ctx:
            system = system + voice_ctx
    except Exception as e:
        logger.warning("chat_context: voice_context load failed uid=%s: %s", user_id, e)

    return system
