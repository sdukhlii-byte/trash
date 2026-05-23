import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut, RetryAfter, BadRequest
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)



def md_escape(text: str) -> str:
    """Экранирует спецсимволы Markdown v1 для Telegram.

    Telegram Markdown v1 ломается на: _ * ` [ ]
    Эмодзи и Unicode безопасны — их не трогаем.
    Также экранируем обратный слэш чтобы не сломать уже экранированные символы.
    """
    if not isinstance(text, str):
        text = str(text)
    # Сначала backslash, потом остальные (иначе двойное экранирование)
    for ch in ("\\", "_", "*", "`", "[", "]"):
        text = text.replace(ch, f"\\{ch}")
    return text


def profile_val(profile: dict, key: str, default: str = "—") -> str:
    """Безопасно извлекает значение профиля и экранирует его для Markdown.

    Гарантирует что любой пользовательский ввод (эмодзи, спецсимволы,
    пустая строка, None) не сломает Telegram Markdown парсер.
    """
    raw = profile.get(key) if isinstance(profile, dict) else None
    # Нормализуем: None, пустая строка, только пробелы -> default
    if not raw or not str(raw).strip():
        raw = default
    return md_escape(str(raw).strip())


# ── keyboard builder ──────────────────────────────────────────────────────────
def kb(*rows) -> InlineKeyboardMarkup:
    result = []
    for row in rows:
        if not row:
            continue
        r = []
        for btn in row:
            if isinstance(btn, str):
                text, data = btn.split("|", 1)
                r.append(InlineKeyboardButton(text.strip(), callback_data=data.strip()))
            else:
                r.append(btn)
        if r:
            result.append(r)
    return InlineKeyboardMarkup(result)


# ── safe send ─────────────────────────────────────────────────────────────────
def _get_chat(update: Update):
    if update.message:
        return update.message
    if update.callback_query and update.callback_query.message:
        return update.callback_query.message
    return None


async def send(update: Update, text: str, **kwargs) -> None:
    msg = _get_chat(update)
    if not msg:
        return
    kwargs.setdefault("parse_mode", "Markdown")
    chunks = [text[i:i + 4000] for i in range(0, max(len(text), 1), 4000)]
    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(0.3)
        kw = kwargs if i == len(chunks) - 1 else {k: v for k, v in kwargs.items()
                                                    if k != "reply_markup"}
        await _send_once(msg, chunk, **kw)


async def _send_once(msg, text: str, **kwargs) -> None:
    for attempt in range(3):
        try:
            await msg.reply_text(text, **kwargs)
            return
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, NetworkError):
            await asyncio.sleep(2 ** attempt)
        except BadRequest as e:
            err = str(e).lower()
            if "parse" in err and "parse_mode" in kwargs:
                # Markdown сломан — отправляем без форматирования
                kw2 = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                try:
                    await msg.reply_text(text, **kw2)
                except Exception as e2:
                    logger.error(f"send fallback error: {e2}")
            elif "message is too long" in err:
                # Режем пополам и отправляем двумя сообщениями
                mid = len(text) // 2
                cut = text.rfind("\n", 0, mid) or mid
                kw2 = {k: v for k, v in kwargs.items() if k != "reply_markup"}
                try:
                    await msg.reply_text(text[:cut], **kw2)
                    await asyncio.sleep(0.3)
                    await msg.reply_text(text[cut:], **kwargs)
                except Exception as e2:
                    logger.error(f"send split error: {e2}")
            else:
                logger.error(f"send BadRequest ({type(msg).__name__}): {e}")
            return
        except Exception as e:
            logger.error(f"send error attempt {attempt+1}: {e}")
            return


async def edit(query, text: str, **kwargs) -> None:
    kwargs.setdefault("parse_mode", "Markdown")
    for attempt in range(3):
        try:
            await query.edit_message_text(text, **kwargs)
            return
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, NetworkError):
            await asyncio.sleep(2 ** attempt)
        except BadRequest as e:
            err = str(e).lower()
            if "not modified" in err:
                return   # текст не изменился — ок
            if "message to edit not found" in err or "message can't be edited" in err:
                # Сообщение удалено — отправляем новым
                try:
                    msg = query.message
                    if msg:
                        kw2 = dict(kwargs)
                        await msg.reply_text(text, **kw2)
                except Exception:
                    pass
                return
            if "parse" in err and "parse_mode" in kwargs:
                kw2 = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                try:
                    await query.edit_message_text(text, **kw2)
                except Exception:
                    pass
                return
            logger.error(f"edit BadRequest: {e}")
            return
        except Exception as e:
            logger.error(f"edit error: {e}")
            return
