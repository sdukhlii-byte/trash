"""
voice_learner.py — петля обратной связи: выход улучшается с каждой генерацией.

ЭТО ГЛАВНЫЙ DIFFERENTIATOR от ChatGPT.

Как работает:
1. После каждого результата — одна кнопка «Звучит как я ✓ / Не совсем ✗»
2. Если «не совсем» → Мира спрашивает что именно не так (1 вопрос)
3. Ответ сохраняется как "voice note" в Redis
4. При следующей генерации voice notes инжектируются в system prompt
5. Через 5-7 генераций бот начинает стабильно попадать в голос

Данные которые накапливаются:
- voice_notes: ["слишком официально", "добавь юмора", "короче абзацы"]
- approved_samples: последние 3 одобренных результата (лучше чем style_examples)
- rejection_patterns: что именно отклоняли

Это создаёт эффект «получает с каждым разом лучше» —
главную причину платить за подписку а не разовый ChatGPT-промпт.
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_VOICE_NOTES_KEY    = "__voice_notes__"
_APPROVED_KEY       = "__approved_samples__"
_REJECTION_KEY      = "__rejection_patterns__"
_FEEDBACK_STEP_KEY  = "__feedback_step__"

MAX_VOICE_NOTES  = 10
MAX_APPROVED     = 5


# ── Сохранение обратной связи ─────────────────────────────────────────────────

async def mark_approved(user_id: int, agent_key: str, content: str) -> None:
    """Пользователь одобрил результат — сохраняем как эталон голоса."""
    from db import kv_get, kv_set
    try:
        raw = await kv_get(user_id, _APPROVED_KEY)
        approved = json.loads(raw) if raw else []
        approved.insert(0, {"agent": agent_key, "text": content[:800]})
        approved = approved[:MAX_APPROVED]
        await kv_set(user_id, _APPROVED_KEY, json.dumps(approved, ensure_ascii=False))
        logger.debug(f"Voice approved: user={user_id} agent={agent_key}")
    except Exception as e:
        logger.warning(f"mark_approved failed: {e}")


async def add_voice_note(user_id: int, note: str) -> None:
    """Добавляет замечание к голосу пользователя."""
    from db import kv_get, kv_set
    try:
        raw = await kv_get(user_id, _VOICE_NOTES_KEY)
        notes = json.loads(raw) if raw else []
        note = note.strip()[:200]
        if note and note not in notes:
            notes.insert(0, note)
            notes = notes[:MAX_VOICE_NOTES]
        await kv_set(user_id, _VOICE_NOTES_KEY, json.dumps(notes, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"add_voice_note failed: {e}")


async def add_rejection_pattern(user_id: int, pattern: str) -> None:
    """Что конкретно отклонили — для future negative reinforcement."""
    from db import kv_get, kv_set
    try:
        raw = await kv_get(user_id, _REJECTION_KEY)
        patterns = json.loads(raw) if raw else []
        patterns.insert(0, pattern.strip()[:150])
        patterns = patterns[:8]
        await kv_set(user_id, _REJECTION_KEY, json.dumps(patterns, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"add_rejection_pattern failed: {e}")


# ── Построение контекста голоса ────────────────────────────────────────────────

async def build_voice_context(user_id: int) -> str:
    """
    Строит блок для инжекции в system prompt генератора.
    Включает одобренные образцы + замечания к голосу + паттерны отклонения.

    Возвращает пустую строку если данных нет (первая генерация).
    """
    from db import kv_get, get_style_examples
    parts = []

    try:
        # 1. Одобренные образцы (важнее style_examples — это то что понравилось)
        raw_approved = await kv_get(user_id, _APPROVED_KEY)
        approved = json.loads(raw_approved) if raw_approved else []
        if approved:
            samples = "\n---\n".join(a["text"] for a in approved[:3])
            parts.append(
                f"ОДОБРЕННЫЕ ОБРАЗЦЫ ГОЛОСА АВТОРА (результаты которые он отметил как «звучит как я»):\n{samples}"
            )

        # 2. Если нет одобренных — берём style_examples
        if not approved:
            style = await get_style_examples(user_id)
            if style:
                samples = "\n---\n".join(style[-3:])
                parts.append(
                    f"ПРИМЕРЫ СТИЛЯ АВТОРА (добавлены вручную):\n{samples}"
                )

        # 3. Замечания к голосу
        raw_notes = await kv_get(user_id, _VOICE_NOTES_KEY)
        notes = json.loads(raw_notes) if raw_notes else []
        if notes:
            notes_text = "\n".join(f"• {n}" for n in notes[:5])
            parts.append(
                f"ВАЖНЫЕ ЗАМЕЧАНИЯ К ГОЛОСУ (то что автор хочет изменить):\n{notes_text}"
            )

        # 4. Паттерны отклонения
        raw_rej = await kv_get(user_id, _REJECTION_KEY)
        rejections = json.loads(raw_rej) if raw_rej else []
        if rejections:
            rej_text = "\n".join(f"• Избегать: {r}" for r in rejections[:4])
            parts.append(rej_text)

    except Exception as e:
        logger.warning(f"build_voice_context failed: {e}")
        return ""

    if not parts:
        return ""

    return "\n\n" + "\n\n".join(parts)


async def get_voice_stats(user_id: int) -> dict:
    """Статистика накопленного голоса для UI."""
    from db import kv_get, get_style_examples
    try:
        raw_approved = await kv_get(user_id, _APPROVED_KEY)
        approved = json.loads(raw_approved) if raw_approved else []
        raw_notes = await kv_get(user_id, _VOICE_NOTES_KEY)
        notes = json.loads(raw_notes) if raw_notes else []
        style = await get_style_examples(user_id)
        return {
            "approved_count": len(approved),
            "notes_count":    len(notes),
            "style_count":    len(style),
            "total_signals":  len(approved) + len(notes) + len(style),
        }
    except Exception:
        return {"approved_count": 0, "notes_count": 0,
                "style_count": 0, "total_signals": 0}


# ── UI: кнопка после результата ───────────────────────────────────────────────

def voice_feedback_kb(result_id: int):
    """Инлайн-кнопки обратной связи по голосу."""
    from utils import kb
    return kb(
        [f"✅ Звучит как я|vf_yes_{result_id}",
         f"✏️ Не совсем|vf_no_{result_id}"],
    )


# ── Callback обработчики ──────────────────────────────────────────────────────

async def handle_voice_feedback_yes(
    update, user_id: int, result_id: int
) -> None:
    """Пользователь одобрил результат."""
    from db import get_result_by_id
    from utils import send, kb

    try:
        r = await get_result_by_id(user_id, result_id)
        if r:
            await mark_approved(user_id, r["agent_key"], r["content"])
    except Exception as e:
        logger.warning(f"voice_feedback_yes result load: {e}")

    stats = await get_voice_stats(user_id)
    total = stats["total_signals"]

    from ui.progress_bar import voice_progress_short
    hint = voice_progress_short(total)

    if total >= 10:
        msg = f"✅ Записала — голос на уровне «Точный» ({total} сигналов). 🎯{hint}"
    elif total >= 5:
        msg = f"✅ Записала этот стиль.{hint}"
    elif total >= 2:
        msg = f"✅ Записала.{hint}"
    else:
        msg = f"✅ Записала твой голос.{hint}"

    await send(update, msg, parse_mode="Markdown",
               reply_markup=kb(["← Меню|menu_main"]))


async def handle_voice_feedback_no(
    update, user_id: int, result_id: int
) -> None:
    """Пользователь отклонил — запрашиваем одно замечание."""
    from db import kv_set
    from utils import send, kb

    await kv_set(user_id, _FEEDBACK_STEP_KEY, str(result_id), ttl=600)
    await send(
        update,
        "Понятно. Что именно не так?\n\n"
        "_Напиши одним предложением — например: «слишком официально», "
        "«длинные абзацы», «без юмора»_",
        parse_mode="Markdown",
        reply_markup=kb(["← Пропустить|menu_main"]),
    )


async def handle_voice_note_text(
    update, user_id: int, text: str
) -> bool:
    """Обрабатывает текстовое замечание к голосу. Возвращает True если потреблено."""
    from db import kv_get, kv_del
    from utils import send, kb

    step = await kv_get(user_id, _FEEDBACK_STEP_KEY)
    if not step:
        return False

    await kv_del(user_id, _FEEDBACK_STEP_KEY)
    await add_voice_note(user_id, text)

    stats = await get_voice_stats(user_id)
    total = stats["total_signals"]

    stats = await get_voice_stats(user_id)
    total = stats["total_signals"]

    from ui.progress_bar import voice_progress_short
    progress = voice_progress_short(total)

    await send(
        update,
        f"✅ Записала: «{text[:80]}»{progress}\n\n_Учту в следующих генерациях._",
        parse_mode="Markdown",
        reply_markup=kb(["← Меню|menu_main"]),
    )
    return True
