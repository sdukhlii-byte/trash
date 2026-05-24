import asyncio
import logging
import httpx

from config import (
    OPENROUTER_KEY, OPENROUTER_URL,
    OPENAI_KEY, WHISPER_URL,
    MODELS, DEFAULT_MODEL,
    LLM_RETRY, LLM_RETRY_DELAY, LLM_RETRY_DELAY_CAP, LLM_TIMEOUT, GEN_TIMEOUT,
)

logger = logging.getLogger(__name__)
SEM_FAST: asyncio.Semaphore | None = None   # short calls: classify, complete <1000 tok
SEM_HEAVY: asyncio.Semaphore | None = None  # long calls: generate_from_history, complete_long

# ── Shared httpx client (один TCP-пул на весь процесс) ───────────────────────
_HTTP: httpx.AsyncClient | None = None

def get_http() -> httpx.AsyncClient:
    """Возвращает общий клиент. Должен быть инициализирован через init_http()."""
    assert _HTTP is not None, "HTTP client not initialised — call init_http() first"
    return _HTTP

async def init_http() -> None:
    global _HTTP
    _HTTP = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=30,
            keepalive_expiry=30,
        ),
        timeout=httpx.Timeout(LLM_TIMEOUT),
    )
    logger.info("Shared httpx client initialised")

async def close_http() -> None:
    global _HTTP
    if _HTTP:
        await _HTTP.aclose()
        _HTTP = None
        logger.info("Shared httpx client closed")


def init_semaphore(size_fast: int, size_heavy: int) -> None:
    global SEM_FAST, SEM_HEAVY
    SEM_FAST  = asyncio.Semaphore(size_fast)
    SEM_HEAVY = asyncio.Semaphore(size_heavy)


# ── raw request ───────────────────────────────────────────────────────────────
async def _call(payload: dict, timeout: float = LLM_TIMEOUT, heavy: bool = False) -> str:
    sem = SEM_HEAVY if heavy else SEM_FAST
    assert sem, "Semaphore not initialised"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://t.me/pocket_marketer_bot",
        "X-Title":       "Pocket Marketer",
    }
    last = None
    for attempt in range(LLM_RETRY):
        try:
            async with sem:
                resp = await get_http().post(OPENROUTER_URL, json=payload, headers=headers,
                                              timeout=timeout)

            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After",
                               LLM_RETRY_DELAY * 2 ** attempt)),
                           LLM_RETRY_DELAY_CAP)
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 500:
                await asyncio.sleep(min(LLM_RETRY_DELAY * 2 ** attempt, LLM_RETRY_DELAY_CAP))
                continue

            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices")
            if not choices:
                raise ValueError(f"Empty choices: {data}")
            return choices[0]["message"]["content"]

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last = e
            await asyncio.sleep(min(LLM_RETRY_DELAY * 2 ** attempt, LLM_RETRY_DELAY_CAP))
        except Exception as e:
            last = e
            logger.error(f"LLM error attempt {attempt+1}: {e}")
            if attempt < LLM_RETRY - 1:
                await asyncio.sleep(min(LLM_RETRY_DELAY * 2 ** attempt, LLM_RETRY_DELAY_CAP))

    raise RuntimeError(f"LLM failed after {LLM_RETRY} attempts: {last}")


# ── public API ────────────────────────────────────────────────────────────────

async def chat(history: list, system: str | None = None,
               model_key: str = DEFAULT_MODEL) -> str:
    """Многоходовой диалог."""
    messages = ([{"role": "system", "content": system}] + history) if system else history
    return await _call({"model": MODELS[model_key], "messages": messages})


async def complete(system: str, user: str,
                   model_key: str = DEFAULT_MODEL,
                   max_tokens: int = 2000) -> str:
    """Одиночный запрос system + user."""
    return await _call({
        "model":      MODELS[model_key],
        "max_tokens": max_tokens,
        "messages":   [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    })


async def complete_long(system: str, user: str,
                        model_key: str = DEFAULT_MODEL) -> str:
    """Длинная генерация (разборы, прогревы, контент-планы)."""
    return await _call({
        "model":      MODELS[model_key],
        "max_tokens": 4000,
        "messages":   [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }, timeout=GEN_TIMEOUT, heavy=True)


async def generate_from_history(system: str, history: list,
                                 final_prompt: str,
                                 model_key: str = DEFAULT_MODEL) -> str:
    """Генерация на основе накопленной истории интервью."""
    return await _call({
        "model":      MODELS[model_key],
        "max_tokens": 4000,
        "messages":   [
            {"role": "system", "content": system},
            *history,
            {"role": "user",   "content": final_prompt},
        ],
    }, timeout=GEN_TIMEOUT, heavy=True)


async def vision_complete(system: str, text_ctx: str,
                           images_b64: list[tuple[str, str]],
                           model_key: str = "gpt4") -> str:
    """GPT-4o vision — текст + изображения."""
    content: list = []
    if text_ctx:
        content.append({"type": "text", "text": text_ctx})
    for b64, mime in images_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
    content.append({
        "type": "text",
        "text": "Проанализируй скриншоты в контексте описания выше и учти их в разборе.",
    })
    return await _call({
        "model":      MODELS[model_key],
        "max_tokens": 4000,
        "messages":   [
            {"role": "system", "content": system},
            {"role": "user",   "content": content},
        ],
    }, timeout=GEN_TIMEOUT)


async def vision_chat(history: list, images_b64: list[tuple[str, str]],
                      caption: str = "",
                      system: str | None = None,
                      model_key: str = "gpt4") -> str:
    """GPT-4o vision — продолжение диалога с прикреплёнными фото.
    
    Фото добавляются как последнее user-сообщение после истории.
    caption — текст, который пользователь написал вместе с фото (может быть пустым).
    """
    content: list = []
    if caption:
        content.append({"type": "text", "text": caption})
    for b64, mime in images_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
    if not caption:
        content.append({
            "type": "text",
            "text": "Посмотри на этот скриншот и учти его в нашем разговоре.",
        })

    messages = list(history) + [{"role": "user", "content": content}]
    if system:
        messages = [{"role": "system", "content": system}] + messages

    return await _call({
        "model":      MODELS[model_key],
        "max_tokens": 2000,
        "messages":   messages,
    }, timeout=GEN_TIMEOUT)


async def vision_describe(images_b64: list[tuple[str, str]],
                           question: str = "",
                           model_key: str = "gpt4") -> str:
    """Универсальный анализ фото без контекста (для чат-режима)."""
    content: list = []
    for b64, mime in images_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
    prompt = question if question else (
        "Подробно опиши что на этом изображении. "
        "Если есть текст — прочитай его. "
        "Если это скриншот — проанализируй содержимое."
    )
    content.append({"type": "text", "text": prompt})

    return await _call({
        "model":      MODELS[model_key],
        "max_tokens": 1500,
        "messages":   [{"role": "user", "content": content}],
    }, timeout=GEN_TIMEOUT)


# ── Whisper voice transcription ───────────────────────────────────────────────
async def transcribe(ogg_bytes: bytes) -> str:
    """
    Отправляет OGG/аудио в OpenAI Whisper и возвращает текст.
    Если OPENAI_KEY не задан — поднимает RuntimeError.
    """
    if not OPENAI_KEY:
        raise RuntimeError("OPENAI_KEY не задан — расшифровка голоса недоступна")

    for attempt in range(LLM_RETRY):
        try:
            resp = await get_http().post(
                WHISPER_URL,
                headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                files={"file": ("voice.ogg", ogg_bytes, "audio/ogg")},
                data={"model": "whisper-1", "language": "ru"},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json().get("text", "").strip()
        except Exception as e:
            logger.error(f"Whisper error attempt {attempt+1}: {e}")
            if attempt < LLM_RETRY - 1:
                await asyncio.sleep(LLM_RETRY_DELAY * 2 ** attempt)

    raise RuntimeError("Whisper не ответил после нескольких попыток")
