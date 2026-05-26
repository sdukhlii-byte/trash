"""
voice_learner.py — feedback loop: output improves with each generation.

Storage (v2):
  Postgres = source of truth (voice_signals table)
  Redis    = read cache (TTL 5 min, invalidated on write)

  Previously Redis-only: any restart wiped weeks of trained voice.
  Now: Redis falls back to Postgres on miss; writes go to both.

How it works:
  1. After every result — one button «Sounds like me ✓ / Not quite ✗»
  2. «Not quite» → Mira asks what exactly is wrong (1 question)
  3. Answer saved as voice note
  4. On next generation voice notes are injected into system prompt
  5. After 5-7 generations the bot reliably matches the user's voice

Security (v2):
  Voice notes are user-supplied text injected into system prompts.
  They are now sanitised before storage to block prompt injection.
"""
import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_VOICE_NOTES_KEY    = "__voice_notes__"
_APPROVED_KEY       = "__approved_samples__"
_REJECTION_KEY      = "__rejection_patterns__"
_FEEDBACK_STEP_KEY  = "__feedback_step__"

_CACHE_TTL    = 300   # 5 min Redis cache for voice data
MAX_VOICE_NOTES  = 10
MAX_APPROVED     = 5

# ── Injection sanitisation ────────────────────────────────────────────────────

# Patterns that indicate a prompt injection attempt.
_INJECTION_RE = re.compile(
    r"(ignore\s+(all\s+)?(previous|above|prior)|"
    r"забудь\s+(все\s+)?инструкции|"
    r"системн\w+\s+промпт|"
    r"you\s+are\s+now|"
    r"act\s+as\s+|"
    r"<\s*/?system|"
    r"\[INST\]|"
    r"###\s*(system|instruction)|"
    r"игнорир\w+\s+(все\s+)?инструкции|"
    r"новые\s+инструкции)",
    re.IGNORECASE,
)

def _sanitise(text: str, max_len: int = 200) -> Optional[str]:
    """
    Strip and length-limit user voice input.
    Returns None if the input looks like a prompt injection attempt.
    """
    text = text.strip()[:max_len]
    if _INJECTION_RE.search(text):
        logger.warning("Voice note blocked (injection pattern): %r", text[:80])
        return None
    # Wrap in XML-style fence so the LLM treats it as data, not instructions.
    # The fence is added at injection time in build_voice_context().
    return text


# ── Postgres persistence ──────────────────────────────────────────────────────

async def _pg_save_signal(user_id: int, kind: str, agent_key: str, content: str) -> None:
    """Insert one voice signal row into Postgres."""
    try:
        from db import _get_pool
        pool = _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO voice_signals (user_id, kind, agent_key, content) "
                "VALUES ($1, $2, $3, $4)",
                user_id, kind, agent_key, content,
            )
    except Exception as e:
        logger.error("_pg_save_signal failed uid=%s kind=%s: %s", user_id, kind, e)


async def _pg_load_signals(user_id: int, kind: str, limit: int) -> list[dict]:
    """Load voice signal rows from Postgres, newest first."""
    try:
        from db import _get_pool
        pool = _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT agent_key, content FROM voice_signals "
                "WHERE user_id=$1 AND kind=$2 ORDER BY ts DESC LIMIT $3",
                user_id, kind, limit,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("_pg_load_signals failed uid=%s kind=%s: %s", user_id, kind, e)
        return []


# ── Redis cache helpers ───────────────────────────────────────────────────────

async def _cache_get(user_id: int, key: str) -> Optional[list]:
    try:
        from db import kv_get
        raw = await kv_get(user_id, key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _cache_set(user_id: int, key: str, data: list) -> None:
    try:
        from db import kv_set
        await kv_set(user_id, key, json.dumps(data, ensure_ascii=False), ttl=_CACHE_TTL)
    except Exception:
        pass


async def _cache_del(user_id: int, key: str) -> None:
    try:
        from db import kv_del
        await kv_del(user_id, key)
    except Exception:
        pass


# ── Public write API ──────────────────────────────────────────────────────────

async def mark_approved(user_id: int, agent_key: str, content: str) -> None:
    """User approved result — save as voice reference sample."""
    entry = {"agent": agent_key, "text": content[:800]}
    # 1. Postgres (source of truth)
    await _pg_save_signal(user_id, "approved", agent_key, content[:800])
    # 2. Redis cache: prepend + trim
    cached = await _cache_get(user_id, _APPROVED_KEY) or []
    cached.insert(0, entry)
    cached = cached[:MAX_APPROVED]
    await _cache_set(user_id, _APPROVED_KEY, cached)
    logger.debug("Voice approved: user=%s agent=%s", user_id, agent_key)

    # 3. Update content_format_affinity in CIP — voice approval is a strong
    # signal that this format resonates with the creator's audience.
    # Score drifts toward 1.0 with each approval (EMA with alpha=0.3).
    try:
        from db import get_cip, save_cip
        _cip = await get_cip(user_id)
        _affinity = _cip.get("content_format_affinity", {})
        _prev = _affinity.get(agent_key, 0.5)
        _affinity[agent_key] = round(_prev * 0.7 + 1.0 * 0.3, 3)  # EMA toward 1.0
        _cip["content_format_affinity"] = _affinity
        await save_cip(user_id, _cip)
    except Exception as _e:
        logger.debug("format_affinity update failed uid=%s: %s", user_id, _e)


async def add_voice_note(user_id: int, note: str) -> bool:
    """
    Add a style correction note.
    Returns False (and logs) if the note is blocked as injection attempt.
    """
    clean = _sanitise(note, max_len=200)
    if clean is None:
        return False
    # 1. Postgres
    await _pg_save_signal(user_id, "note", "", clean)
    # 2. Redis cache: prepend + trim + dedup
    cached = await _cache_get(user_id, _VOICE_NOTES_KEY) or []
    if clean not in cached:
        cached.insert(0, clean)
        cached = cached[:MAX_VOICE_NOTES]
        await _cache_set(user_id, _VOICE_NOTES_KEY, cached)
    return True


async def add_rejection_pattern(user_id: int, pattern: str) -> None:
    """What was specifically rejected — for negative reinforcement."""
    clean = _sanitise(pattern, max_len=150)
    if clean is None:
        return
    await _pg_save_signal(user_id, "rejection", "", clean)
    cached = await _cache_get(user_id, _REJECTION_KEY) or []
    cached.insert(0, clean)
    cached = cached[:8]
    await _cache_set(user_id, _REJECTION_KEY, cached)


# ── Read API (cache → Postgres fallback) ─────────────────────────────────────

async def _get_approved(user_id: int) -> list:
    cached = await _cache_get(user_id, _APPROVED_KEY)
    if cached is not None:
        return cached
    rows = await _pg_load_signals(user_id, "approved", MAX_APPROVED)
    data = [{"agent": r["agent_key"], "text": r["content"]} for r in rows]
    await _cache_set(user_id, _APPROVED_KEY, data)
    return data


async def _get_notes(user_id: int) -> list:
    cached = await _cache_get(user_id, _VOICE_NOTES_KEY)
    if cached is not None:
        return cached
    rows = await _pg_load_signals(user_id, "note", MAX_VOICE_NOTES)
    data = [r["content"] for r in rows]
    await _cache_set(user_id, _VOICE_NOTES_KEY, data)
    return data


async def _get_rejections(user_id: int) -> list:
    cached = await _cache_get(user_id, _REJECTION_KEY)
    if cached is not None:
        return cached
    rows = await _pg_load_signals(user_id, "rejection", 8)
    data = [r["content"] for r in rows]
    await _cache_set(user_id, _REJECTION_KEY, data)
    return data


# ── Build voice context for system prompt injection ───────────────────────────

async def build_voice_context(user_id: int) -> str:
    """
    Build block for injection into generator system prompt.
    Includes approved samples + style notes + rejection patterns.
    Content is XML-fenced to prevent it from being interpreted as instructions.

    Returns empty string on first generation (no data yet).
    """
    from db import get_style_examples
    parts = []

    try:
        # 1. Approved samples (stronger signal than style_examples)
        approved = await _get_approved(user_id)
        if approved:
            samples = "\n---\n".join(a["text"] for a in approved[:3])
            parts.append(
                "APPROVED VOICE SAMPLES (results the author marked as «sounds like me»):\n"
                f"<voice_samples>\n{samples}\n</voice_samples>"
            )

        # 2. Fall back to manually-added style examples if no approved yet
        if not approved:
            style = await get_style_examples(user_id)
            if style:
                samples = "\n---\n".join(style[-3:])
                parts.append(
                    "AUTHOR STYLE EXAMPLES (added manually):\n"
                    f"<style_examples>\n{samples}\n</style_examples>"
                )

        # 3. Style correction notes
        notes = await _get_notes(user_id)
        if notes:
            notes_text = "\n".join(f"• {n}" for n in notes[:5])
            parts.append(
                "STYLE CORRECTION NOTES (what the author wants changed):\n"
                f"<voice_notes>\n{notes_text}\n</voice_notes>"
            )

        # 4. Rejection patterns
        rejections = await _get_rejections(user_id)
        if rejections:
            rej_text = "\n".join(f"• Avoid: {r}" for r in rejections[:4])
            parts.append(f"<rejection_patterns>\n{rej_text}\n</rejection_patterns>")

        # 5. Текущий голосовой контекст (эмоция, тема — из последнего голосового)
        try:
            import json as _json
            from db import kv_get as _kv_get
            _vctx_raw = await _kv_get(user_id, "__voice_ctx__")
            if _vctx_raw:
                _vctx = _json.loads(_vctx_raw)
                _meta_parts = []
                if _vctx.get("emotional_tone"):
                    _meta_parts.append(f"Эмоция автора: {_vctx['emotional_tone']}")
                if _vctx.get("key_theme"):
                    _meta_parts.append(f"Тема запроса: {_vctx['key_theme']}")
                if _vctx.get("urgency") == "high":
                    _meta_parts.append("Срочность: высокая — дай быстрый результат")
                if _vctx.get("creator_context"):
                    _meta_parts.append(f"Контекст автора: {_vctx['creator_context']}")
                if _meta_parts:
                    parts.append(
                        "VOICE SESSION CONTEXT (из текущего голосового запроса):\n"
                        f"<voice_session>\n" + "\n".join(_meta_parts) + "\n</voice_session>"
                    )
        except Exception:
            pass

    except Exception as e:
        logger.warning("build_voice_context failed uid=%s: %s", user_id, e)
        return ""

    if not parts:
        return ""

    return "\n\n" + "\n\n".join(parts)


async def update_voice_meta_profile(user_id: int, voice_ctx: dict) -> None:
    """
    Обновляет мета-профиль голоса после каждого голосового сообщения.
    Сохраняет паттерны речи для долгосрочной персонализации (фаза 4.1).
    EMA по последним 5 сигналам через Redis.
    """
    if not voice_ctx:
        return
    try:
        import json as _json
        from db import kv_get as _kv_get, kv_set as _kv_set
        _KEY = "__voice_meta_profile__"
        raw = await _kv_get(user_id, _KEY)
        meta = _json.loads(raw) if raw else {
            "dominant_tones": [],
            "content_type_history": [],
            "urgency_signals": 0,
            "message_count": 0,
        }

        meta["message_count"] = meta.get("message_count", 0) + 1

        tone = voice_ctx.get("emotional_tone")
        if tone:
            tones = meta.get("dominant_tones", [])
            tones.append(tone)
            meta["dominant_tones"] = tones[-5:]  # последние 5

        ct = voice_ctx.get("content_type_hint")
        if ct:
            hist = meta.get("content_type_history", [])
            hist.append(ct)
            meta["content_type_history"] = hist[-10:]

        if voice_ctx.get("urgency") == "high":
            meta["urgency_signals"] = meta.get("urgency_signals", 0) + 1

        await _kv_set(user_id, _KEY, _json.dumps(meta, ensure_ascii=False), ttl=86400 * 30)
    except Exception as e:
        logger.debug("update_voice_meta_profile failed uid=%s: %s", user_id, e)


async def get_voice_stats(user_id: int) -> dict:
    """Voice accumulation stats for UI."""
    try:
        from db import get_style_examples
        approved   = await _get_approved(user_id)
        notes      = await _get_notes(user_id)
        style      = await get_style_examples(user_id)
        return {
            "approved_count": len(approved),
            "notes_count":    len(notes),
            "style_count":    len(style),
            "total_signals":  len(approved) + len(notes) + len(style),
        }
    except Exception:
        return {"approved_count": 0, "notes_count": 0,
                "style_count": 0, "total_signals": 0}


# ── UI: feedback button after result ─────────────────────────────────────────

_FEEDBACK_SHOW_EVERY = 5   # after 10 signals: show only every N generations

def should_show_voice_feedback(total_signals: int, generation_count: int) -> bool:
    """
    Умная логика показа voice feedback — без fatigue.

    До 5 сигналов: показываем часто (каждые 2 генерации) — учим пользователя.
    5-15 сигналов: каждые 3 генерации.
    15+ сигналов: каждые 5 генерации — Мира уже знает голос, feedback реже.
    """
    if total_signals < 5:
        return (generation_count % 2) == 0
    if total_signals < 15:
        return (generation_count % 3) == 0
    return (generation_count % _FEEDBACK_SHOW_EVERY) == 0


def voice_feedback_kb(result_id: int, extra_rows: list | None = None):
    """
    Inline feedback buttons.

    extra_rows — additional button rows to append after the voice feedback row.
    Используется в agents._after_result для объединения voice feedback + панели правок
    в одно сообщение вместо двух отдельных.
    """
    from utils import kb
    rows = [
        [f"✅ Звучит как я|vf_yes_{result_id}",
         f"✏️ Не совсем|vf_no_{result_id}"],
    ]
    if extra_rows:
        rows.extend(extra_rows)
    return kb(*rows)


# ── Callback handlers ─────────────────────────────────────────────────────────

async def handle_voice_feedback_yes(
    update, user_id: int, result_id: int
) -> None:
    """User approved result."""
    from db import get_result_by_id
    from utils import send, kb

    try:
        r = await get_result_by_id(user_id, result_id)
        if r:
            await mark_approved(user_id, r["agent_key"], r["content"])
    except Exception as e:
        logger.warning("voice_feedback_yes result load: %s", e)

    stats = await get_voice_stats(user_id)
    total = stats["total_signals"]

    from ui.progress_bar import voice_progress_short
    hint = voice_progress_short(total)

    if total >= 10:
        msg = f"✅ Запомнила. Теперь пишу точно в твоём стиле ({total} примеров). 🎯{hint}"
    elif total >= 5:
        msg = f"✅ Запомнила — учту в следующем тексте.{hint}"
    elif total >= 2:
        msg = f"✅ Запомнила.{hint}"
    else:
        msg = f"✅ Запомнила — это мой первый ориентир по твоему стилю.{hint}"

    await send(update, msg, parse_mode="Markdown",
               reply_markup=kb(["← Меню|menu_main"]))


async def handle_voice_feedback_no(
    update, user_id: int, result_id: int
) -> None:
    """User rejected — ask for one correction note, downvote format affinity."""
    from db import kv_set, get_result_by_id, get_cip, save_cip
    from utils import send, kb

    # Downvote format affinity for rejected agent_key
    try:
        r = await get_result_by_id(user_id, result_id)
        if r:
            _cip = await get_cip(user_id)
            _affinity = _cip.get("content_format_affinity", {})
            _prev = _affinity.get(r["agent_key"], 0.5)
            _affinity[r["agent_key"]] = round(_prev * 0.7 + 0.0 * 0.3, 3)  # EMA toward 0
            _cip["content_format_affinity"] = _affinity
            await save_cip(user_id, _cip)
    except Exception as _e:
        logger.debug("format_affinity downvote failed: %s", _e)

    await kv_set(user_id, _FEEDBACK_STEP_KEY, str(result_id), ttl=600)
    await send(
        update,
        "Понятно, поправлю. Что именно не похоже на тебя?\n\n"
        "_Напиши одним предложением — например: «слишком официально», "
        "«длинные абзацы», «без юмора»_",
        parse_mode="Markdown",
        reply_markup=kb(["← Пропустить|menu_main"]),
    )


async def handle_voice_note_text(
    update, user_id: int, text: str
) -> bool:
    """Process text correction note. Returns True if consumed."""
    from db import kv_get, kv_del
    from utils import send, kb

    step = await kv_get(user_id, _FEEDBACK_STEP_KEY)
    if not step:
        return False

    await kv_del(user_id, _FEEDBACK_STEP_KEY)
    saved = await add_voice_note(user_id, text)

    if not saved:
        # Injection attempt blocked — respond neutrally
        await send(
            update,
            "Не смогла сохранить — попробуй описать иначе.",
            reply_markup=kb(["← Меню|menu_main"]),
        )
        return True

    # Single stats call (fixed: was called twice)
    stats = await get_voice_stats(user_id)
    total = stats["total_signals"]

    from ui.progress_bar import voice_progress_short
    progress = voice_progress_short(total)

    await send(
        update,
        f"✅ Поняла: «{text[:80]}»{progress}\n\n_Следующий текст напишу иначе._",
        parse_mode="Markdown",
        reply_markup=kb(["← Меню|menu_main"]),
    )
    return True
