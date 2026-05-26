"""
flows/onboarding.py v3 — умный онбординг с LLM micro-observation.

WOW-момент в первые 30 секунд:
  После «фитнес» пользователь получает не «Поняла — фитнес 👇»,
  а реальное экспертное наблюдение про нишу + умный вопрос.
  Именно это отличает инструмент от продукта.
"""
import asyncio
import logging

from telegram import Update

from db import (
    get_profile, save_profile,
    get_onboarding_state, save_onboarding_state, clear_onboarding_state,
)
from llm import complete
from security import protect
from user_state import get_user_state, has_access, invalidate_state_cache, UserState
from lava_payments import TRIAL_DAYS
from utils import send, kb
from config import QUICK_IDEAS_SYSTEM
from prompt_editor import get_prompt

logger = logging.getLogger(__name__)
async def _typing_loop(chat) -> None:
    """Typing indicator пока идёт LLM-вызов."""
    try:
        while True:
            await chat.send_action("typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


_ONB_STEPS = ["niche", "audience", "tone", "push_time"]

_ONB_Q_NICHE = (
    "Привет! Я Мира — твой SMM-стратег.\n\n"
    "Расскажи голосом или текстом: с чем работаешь и кто твои люди?\n\n"
    "_Говори как есть — ниша, тема, аудитория. "
    "Фитнес, психология, бизнес, дизайн — или что-то своё.\n"
    "Голосом — так я лучше пойму твою подачу 🎙_"
)

# ── LLM prompts для умных ack ─────────────────────────────────────────────────

_NICHE_OBS_SYSTEM = """\
Ты Мира — SMM-эксперт. Пользователь только что назвал свою нишу при регистрации.

Твоя задача: написать умный ответ из двух частей.

ЧАСТЬ 1 — наблюдение про нишу (2 предложения):
Что конкретно сложно или пересыщено в этой нише. Где окно возможностей.
НЕ льсти. НЕ говори «отличная ниша». Говори честно как эксперт.

ЧАСТЬ 2 — вопрос про аудиторию (1 строка):
Спроси про конкретного покупателя, не абстрактную ЦА.

Примеры хороших наблюдений:
- Фитнес: «В фитнесе уже 10 тысяч постов про похудение и "просто начни". Самое ценное — найти угол которого нет у конкурентов.»
- Психология: «В психологии доверие строится месяцами, но покупают дорого и надолго. Контент здесь работает иначе чем в инфобизнесе.»
- Дизайн: «Большинство дизайнеров показывают портфолио — мало кто объясняет что клиент получит конкретно. Это твоё окно.»
- Коучинг: «Ниша перегрета экспертами "до и после". Выигрывают те у кого есть конкретная история трансформации — не абстрактный результат.»

Пиши коротко. Без вступлений. Сразу наблюдение + вопрос.\
"""

_AUDIENCE_OBS_SYSTEM = """\
Ты Мира — SMM-эксперт. Пользователь описал свою аудиторию при регистрации.

Твоя задача: написать умный ответ из двух частей.

ЧАСТЬ 1 — наблюдение про аудиторию (1-2 предложения):
Что важно для этих людей в контексте контента. Что их останавливает или цепляет.

ЧАСТЬ 2 — вопрос про тон (1 строка):
Как автор общается с этой аудиторией — стиль, голос, подача.

Примеры:
- «Такие люди уже пробовали решить это сами — им не нужна базовая теория, нужен честный разговор о том почему не получилось.»
- «Предприниматели в этой стадии очень чуют "продажный" тон — им важна конкретика и уважение ко времени.»
- «Молодые мамы читают между делом, в 5 утра или в очереди — короткие, конкретные форматы работают лучше всего.»

Пиши коротко. Без вступлений. Сразу наблюдение + вопрос.\
"""


async def _smart_ack_niche(user_id: int, niche: str) -> str:
    try:
        result = await complete(
            _NICHE_OBS_SYSTEM,
            f"Ниша пользователя: «{niche}»",
            temperature=0.65,
        )
        if result and result.strip():
            return result.strip()
        raise ValueError("empty response")
    except Exception as e:
        logger.warning(f"smart_ack_niche failed (fallback): {e}")
        return (
            f"Поняла — {niche[:40]}.\n\n"
            "Кто твои люди?\n\n"
            "_Опиши одного конкретного человека который уже покупает у тебя или точно купит._"
        )


async def _smart_ack_audience(user_id: int, audience: str) -> str:
    try:
        result = await complete(
            _AUDIENCE_OBS_SYSTEM,
            f"Аудитория: «{audience}»",
            temperature=0.55,
        )
        if result and result.strip():
            return result.strip()
        raise ValueError("empty response")
    except Exception as e:
        logger.warning(f"smart_ack_audience failed (fallback): {e}")
        return (
            "Вижу с кем работаешь.\n\n"
            "Как ты с ними разговариваешь?\n\n"
            "_Дерзко и прямо, тепло и поддерживающе, экспертно и по делу — или своё._"
        )


async def onb_next(update: Update, user_id: int, state: dict) -> None:
    idx = state.get("step", 0)
    if idx == 0:
        await send(update, _ONB_Q_NICHE, parse_mode="Markdown")
    elif idx >= len(_ONB_STEPS):
        await _finish_onboarding(update, user_id, state)


async def _finish_onboarding(update: Update, user_id: int, state: dict) -> None:
    profile = {**state.get("data", {}), "onboarded": True}
    await save_profile(user_id, profile)
    # O(1) инкремент счётчика для social proof на пейволле
    from ui.paywall import increment_user_count
    await increment_user_count()
    await clear_onboarding_state(user_id)
    await invalidate_state_cache(user_id)

    # Планируем утренний пуш если юзер указал время в онбординге
    _push_data = state.get("data", {})
    if _push_data.get("push_enabled"):
        try:
            from flows.daily_push import get_push_settings, save_push_settings, schedule_daily_push
            import telegram.ext as _tg_ext
            # Сохраняем настройки
            _ps = await get_push_settings(user_id)
            _ps.update({
                "enabled": True,
                "hour": _push_data.get("push_hour_utc", 7),
                "minute": _push_data.get("push_minute", 0),
                "hour_local": _push_data.get("push_hour_local", 9),
            })
            await save_push_settings(user_id, _ps)
            # Планируем задание (нужно приложение — берём из контекста через глобальный app)
            from bot_context import get_app
            _app = get_app()
            if _app:
                await schedule_daily_push(
                    _app, user_id,
                    _ps["hour"], _ps["minute"],
                )
                logger.info(f"[onboarding] daily push scheduled for uid={user_id}")
        except Exception as _pe:
            logger.warning(f"[onboarding] push setup failed uid={user_id}: {_pe}")

    new_state = await get_user_state(user_id)
    if has_access(new_state):
        await send(update, "✅ Профиль обновлён.", parse_mode="Markdown")
        from ui.menu import show_menu
        await show_menu(update, user_id)
        return

    _data = state.get("data", {})
    _preview = None

    if _data.get("niche"):
        try:
            _status = await update.effective_chat.send_message(
                "Записала всё — секунду, пишу первый пример 👇"
            )
            # Генерируем полноценный короткий пост, а не просто 3 строчки
            _prompt = (
                f"Ниша: {_data['niche']}\n"
                f"Аудитория: {_data.get('audience', '')}\n"
                f"Тон: {_data.get('tone', '')}\n\n"
                "Напиши один короткий живой пост для Instagram (150-250 слов).\n"
                "Начни с сильного хука — первая строка останавливает скролл.\n"
                "Пиши в голосе автора, не как шаблон.\n"
                "В конце — один открытый вопрос к аудитории.\n"
                "Только текст поста, без пояснений."
            )
            _sys = protect(user_id, await get_prompt(user_id, "quick_ideas", QUICK_IDEAS_SYSTEM))
            _tt_onb = asyncio.create_task(_typing_loop(update.effective_chat))
            try:
                _preview = await complete(_sys, _prompt, temperature=0.85)
            finally:
                _tt_onb.cancel()
            try:
                await _status.delete()
            except Exception:
                pass
        except Exception as _e:
            logger.warning(f"onboarding preview failed: {_e}")
            try:
                await _status.delete()
            except Exception:
                pass

    if _preview:
        await send(
            update,
            f"*Вот как это выглядит под твою нишу:*\n\n{_preview}\n\n"
            f"_Это черновик за 10 секунд. Агенты делают глубже — с интервью, голосом, хуками под аудиторию._",
            parse_mode="Markdown",
        )
        await asyncio.sleep(1.2)

    # Планируем нудж через 15 мин если не нажмут кнопку триала
    try:
        from flows.utm import track_event as _te
        await _te(user_id, "onboarding_done")
    except Exception:
        pass

    await send(
        update,
        f"Это только начало.\n\n"
        f"Посты, рилсы, карусели, прогревы — в твоём голосе, под твою аудиторию.\n\n"
        f"*{TRIAL_DAYS} дня бесплатно* — без карты, без подвоха.\n\n"
        f"_Фрилансер-SMM берёт от 15 000 ₽/мес. Мира — от 2 800 ₽/мес, 24/7._",
        parse_mode="Markdown",
        reply_markup=kb(
            ["🎁 Активировать бесплатный доступ|sub_trial"],
            ["💳 Сразу оформить подписку|sub_pay"],
            ["ℹ️ Что умею?|sub_about"],
        ),
    )


async def handle_onboarding(
    update: Update, user_id: int, text: str, state: dict
) -> bool:
    """
    Обрабатывает ввод во время онбординга. Возвращает True если потреблён.

    v3: умные LLM micro-observation встроены в ack — вопрос следующего шага
    генерируется самим LLM как часть наблюдения, не вызывается отдельно.
    """
    idx = state.get("step", 0)
    if idx >= len(_ONB_STEPS):
        return False

    # Нормализация голосового ввода при онбординге (шаги 0-1 — ниша и аудитория)
    _input_text = text
    if idx in (0, 1) and len(text) > 40:
        try:
            from voice_normalizer import normalize_voice
            _nctx = await normalize_voice(text)
            # Для онбординга берём нормализованный запрос — он чище
            if _nctx.get("normalized_request"):
                _input_text = _nctx["normalized_request"]
                # Если есть creator_context — добавляем к нише
                if idx == 0 and _nctx.get("creator_context"):
                    _input_text = f"{_input_text} ({_nctx['creator_context']})"
        except Exception:
            pass

    data = state.get("data", {})
    data[_ONB_STEPS[idx]] = _input_text
    state["data"] = data
    state["step"] = idx + 1
    await save_onboarding_state(user_id, state)

    if idx == 0:
        # Нишевой ответ → LLM-наблюдение + вопрос про аудиторию (всё в одном)
        ack = await _smart_ack_niche(user_id, text)
        await send(update, ack)
    elif idx == 1:
        # Аудитория → LLM-наблюдение + вопрос про тон (всё в одном)
        ack = await _smart_ack_audience(user_id, text)
        await send(update, ack)
    elif idx == 2:
        # Тон → вопрос про время пуша
        await send(
            update,
            "⏰ *В какое время присылать идею дня?*\n\n"
            "_Каждое утро — конкретная идея поста, формат и одна задача. "
            "Под твою нишу и аудиторию._\n\n"
            "Напиши время: `9:00`, `8:30` — или нажми «Пропустить».",
            parse_mode="Markdown",
            reply_markup=kb(["⏭ Пропустить|onb_skip_push"]),
        )

    elif idx == 3:
        # push_time → парсим и включаем пуш
        import re as _re
        m = _re.match(r"(\d{1,2}):(\d{2})", text.strip())
        if m:
            hour_local = int(m.group(1))
            minute = int(m.group(2))
            tz_offset = 2  # UTC+2 (ES/HR/RS по умолчанию)
            hour_utc = (hour_local - tz_offset) % 24
            state["data"]["push_hour_utc"] = hour_utc
            state["data"]["push_minute"] = minute
            state["data"]["push_hour_local"] = hour_local
            state["data"]["push_enabled"] = True
            await save_onboarding_state(user_id, state)
            await _finish_onboarding(update, user_id, state)
        else:
            # Неверный формат — просим ввести снова
            await send(
                update,
                "Не понял формат. Напиши например: `9:00` или `08:30`\n\n"
                "_Или нажми «Пропустить» чтобы настроить позже._",
                parse_mode="Markdown",
                reply_markup=kb(["⏭ Пропустить|onb_skip_push"]),
            )
            state["step"] = 3
            await save_onboarding_state(user_id, state)
            return True

    return True  # ← единственный return True
