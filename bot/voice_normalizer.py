"""
voice_normalizer.py — нормализация сырой транскрипции Whisper перед routing'ом.

Встраивается между Whisper и classify_intent.
Убирает мусор, сохраняет голос и эмоцию, возвращает структурированный контекст.
"""
import json
import logging

logger = logging.getLogger(__name__)

VOICE_NORMALIZE_SYSTEM = """
Ты получаешь расшифровку голосового сообщения пользователя.
Пользователь — создатель контента (блогер, SMM-специалист, эксперт), говорит хаотично и эмоционально.

Твоя задача: извлечь структурированный intent без потери голоса и эмоции.

Верни ТОЛЬКО JSON (без markdown, без пояснений):
{
  "normalized_request": "чистый запрос для агента — убери слова-паразиты (ну, вот, типа, блин, там, как бы), незавершённые мысли, повторы; СОХРАНИ ключевые слова темы, эмоциональные маркеры, slang создателя",
  "content_type_hint": "post|reel|carousel|stories|warmup|talking_head|tg_plan|quick_ideas|competitor|profile|chat|null",
  "emotional_tone": "frustrated|excited|uncertain|playful|serious|urgent|casual",
  "key_theme": "главная тема в 3-5 словах или null",
  "creator_context": "что понятно о нише/аудитории из речи или null",
  "urgency": "low|medium|high"
}

Правила:
- normalized_request — всегда заполнен, даже если просто перефраз
- Если поле неизвестно — null
- Не добавляй ничего чего не было в исходном сообщении
- Сохраняй язык пользователя (русский/английский/смешанный)
"""


async def normalize_voice(raw_transcript: str) -> dict:
    """
    Нормализует сырую транскрипцию Whisper.

    Returns:
        dict с полями: normalized_request, content_type_hint,
                       emotional_tone, key_theme, creator_context, urgency
        При ошибке — fallback с raw_transcript как normalized_request.
    """
    _fallback = {
        "normalized_request": raw_transcript,
        "content_type_hint": None,
        "emotional_tone": None,
        "key_theme": None,
        "creator_context": None,
        "urgency": "medium",
    }

    if not raw_transcript or not raw_transcript.strip():
        return _fallback

    # Короткие сообщения не нормализуем — слишком мало контекста
    if len(raw_transcript.strip()) < 30:
        return _fallback

    try:
        from llm import chat as llm_chat
        messages = [{"role": "user", "content": f"Расшифровка голосового:\n\n{raw_transcript}"}]
        result = await llm_chat(
            messages,
            system=VOICE_NORMALIZE_SYSTEM,
            model_key="claude",
            temperature=0.2,
        )
        # Strip markdown fences if any
        clean = result.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
            clean = clean.strip()

        parsed = json.loads(clean)

        # Validate required field
        if not parsed.get("normalized_request"):
            parsed["normalized_request"] = raw_transcript

        logger.info(
            "[voice_normalizer] hint=%s tone=%s urgency=%s",
            parsed.get("content_type_hint"),
            parsed.get("emotional_tone"),
            parsed.get("urgency"),
        )
        return parsed

    except Exception as e:
        logger.warning("[voice_normalizer] failed, using raw: %s", e)
        return _fallback
