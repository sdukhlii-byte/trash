"""
ui/paywall.py — пейволл с social proof и image_registry.

v3:
- Использует image_registry.py (file_id) вместо чтения PNG с диска
- Social proof: число активных пользователей в тексте (берётся из Redis)
- Expired: показывает конкретные сохранённые материалы пользователя
"""
import logging

from telegram import Update

from user_state import UserState
from utils import send, kb
from lava_payments import TRIAL_DAYS

logger = logging.getLogger(__name__)


async def _send_photo(update: Update, filename: str,
                      caption: str, reply_markup=None, parse_mode: str = None) -> bool:
    """Отправляет фото. Сначала пробует file_id, потом диск."""
    bot = update.get_bot()
    chat_id = update.effective_chat.id

    # 1. Пробуем через image_registry (file_id — мгновенно, без трафика)
    try:
        from image_registry import IMAGE_FILE_IDS
        file_id = IMAGE_FILE_IDS.get(filename)
        if file_id:
            await bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            return True
    except Exception as e:
        logger.debug(f"image_registry miss for {filename}: {e}")

    # 2. Fallback: читаем с диска (пока не запущен upload_images.py)
    import os
    _dirs = [os.path.dirname(os.path.abspath(__file__)),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
             os.getcwd()]
    for _d in _dirs:
        _p = os.path.join(_d, filename)
        if os.path.exists(_p):
            try:
                with open(_p, "rb") as _f:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=_f,
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
                return True
            except Exception as e:
                logger.error(f"[photo disk] send error {filename}: {e}")
    return False


_SOCIAL_PROOF_KEY = "global:user_count"


async def increment_user_count() -> None:
    """Вызывается при сохранении профиля — инкремент O(1)."""
    try:
        from db import get_redis
        r = await get_redis()
        await r.incr(_SOCIAL_PROOF_KEY)
    except Exception:
        pass


async def _get_social_proof() -> str:
    """Возвращает строку social proof. O(1) — один GET из Redis."""
    try:
        from db import get_redis
        r = await get_redis()
        val = await r.get(_SOCIAL_PROOF_KEY)
        count = int(val) if val else 0
        if count > 20:
            rounded = (count // 10) * 10
            return f"\n\n_{rounded}+ экспертов уже создают контент с Мирой._"
    except Exception:
        pass
    return ""


async def show_paywall(update: Update, user_id: int, state: UserState) -> None:
    if state == UserState.ONBOARDED:
        await _paywall_new(update, user_id)
    elif state == UserState.EXPIRED:
        await _paywall_expired(update, user_id)
    else:
        await _paywall_generic(update)


async def _paywall_new(update: Update, user_id: int) -> None:
    social = await _get_social_proof()
    caption = (
        f"Привет, я Мира.\n\n"
        f"Посты, рилсы, прогревы, сторис — в твоём голосе, под твою аудиторию.\n"
        f"Не шаблоны. Не ChatGPT-глянец.\n\n"
        f"*{TRIAL_DAYS} дня бесплатно* — без карты. "
        f"Напиши тему поста или рилса и сама увидишь разницу.\n\n"
        f"_Фрилансер-SMM берёт от 15 000 ₽/мес. Мира — от 2 800 ₽/мес, 24/7.{social}_"
    )
    _kb = kb(
        ["🎁 Попробовать бесплатно|sub_trial"],
        ["💳 Сразу оформить подписку|sub_pay"],
        ["ℹ️ Что умею?|sub_about"],
    )
    if not await _send_photo(update, "posti4.png", caption, _kb, "Markdown"):
        await send(update, caption, parse_mode="Markdown", reply_markup=_kb)


async def _paywall_expired(update: Update, user_id: int) -> None:
    from db import get_results
    results = await get_results(user_id, limit=3)

    if results:
        names = [r["agent_name"] for r in results[:2]]
        personal_line = f"Твои материалы — {', '.join(names)} и другие — никуда не делись."
    else:
        personal_line = "Всё что создавала — сохранено и ждёт тебя."

    caption = (
        f"Доступ закончился.\n\n"
        f"{personal_line}\n\n"
        f"Фрилансер-SMM берёт от €150/мес. Я — €31/мес, 24/7, в твоём голосе.\n\n"
        f"Один клик — и продолжаем."
    )
    _kb = kb(
        ["🔄 Возобновить подписку|sub_pay"],
        ["👤 Личный кабинет|sub_cabinet"],
    )
    if not await _send_photo(update, "posti9.png", caption, _kb):
        await send(update, caption, reply_markup=_kb)


async def _paywall_generic(update: Update) -> None:
    await send(
        update,
        "🔒 Доступ по подписке.\n\nОформи — и начнём работать.",
        parse_mode="Markdown",
        reply_markup=kb(
            ["💳 Оформить подписку|sub_pay"],
            ["👤 Личный кабинет|sub_cabinet"],
        ),
    )
