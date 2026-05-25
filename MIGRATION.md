# Миграция: trash-main → fixed-bot

## Что изменилось

### Новая структура файлов

```
fixed-bot/
├── main.py                  ← был: точка входа с aiohttp внутри
├── config.py                ← расширен: удалён LLM_SEMAPHORE_SIZE, добавлены фабрики
├── llm.py                   ← Whisper без language=, temperature/pp контроль
├── db.py                    ← Redis singleton с Lock, build_profile_ctx перенесён
├── user_state.py            ← кэш состояния в Redis (TTL 60 сек)
├── security.py              ← NEW: _protect() вынесен сюда
├── utils.py                 ← bare except → TelegramError, safe_delete()
├── agents.py                ← без изменений (копируй из оригинала)
├── intent_router.py         ← без изменений
├── lava_payments.py         ← без изменений
├── retention.py             ← без изменений
├── prompt_editor.py         ← без изменений
├── registry.py              ← без изменений
│
├── ui/
│   ├── __init__.py
│   ├── menu.py              ← NEW: меню 2 уровня, no session clear on back
│   ├── paywall.py           ← NEW: лучшие тексты, expired с контекстом
│   └── cabinet.py           ← NEW: кабинет + upsell
│
├── flows/
│   ├── __init__.py
│   ├── onboarding.py        ← NEW: fix дубль return True, умные acks
│   ├── reels.py             ← NEW: Рилс-коротышка со статусами с характером
│   ├── carousel.py          ← NEW: Каруселькин с контекстными ошибками
│   └── misc.py              ← NEW: idei, refine, regen, planner, daily (jitter ±5min), style
│
├── handlers/
│   ├── __init__.py
│   ├── commands.py          ← NEW: /start /menu /clear /reset /subscribe /support
│   ├── messages.py          ← NEW: handle_text, handle_voice, handle_photo
│   └── callbacks.py         ← NEW: тонкий роутер callback_data
│
└── tests/
    ├── __init__.py
    └── test_state_machine.py ← NEW: 30 тестов
```

---

## Пошаговая миграция

### Шаг 1 — Скопировать неизменённые файлы

```bash
cp trash-main/agents.py         fixed-bot/
cp trash-main/intent_router.py  fixed-bot/
cp trash-main/lava_payments.py  fixed-bot/   # уже скопирован
cp trash-main/retention.py      fixed-bot/
cp trash-main/prompt_editor.py  fixed-bot/   # уже скопирован
cp trash-main/registry.py       fixed-bot/   # уже скопирован
```

### Шаг 2 — Обновить config.py

Оригинальный `config.py` содержит все промпты агентов (884 строки).
Новый `config.py` добавляет:
- `SEM_FAST_SIZE` / `SEM_HEAVY_SIZE` (было инлайн в main.py)
- `MIRA_INTRO`
- `build_reels_short_headline_system()`, `build_reels_short_desc_system()`
- `build_carousel_system()`
- `CAROUSEL_TREND_SYSTEM`, `CAROUSEL_HEADLINE_SYSTEM`
- Удалена `LLM_SEMAPHORE_SIZE` (мёртвая переменная)

**Вариант:** слей оба файла — возьми весь оригинальный config.py и добавь в начало константы из нового config.py.

### Шаг 3 — Добавить переменные окружения

```env
# Новые (опциональные)
SEM_FAST_SIZE=12
SEM_HEAVY_SIZE=4
WEBHOOK_SECRET=<случайная строка>
```

### Шаг 4 — Установить зависимости

```bash
pip install pytest pytest-asyncio --break-system-packages
```

### Шаг 5 — Запустить тесты

```bash
cd fixed-bot
pytest tests/ -v
```

Все 30 тестов должны пройти.

### Шаг 6 — Удалить изображения из репозитория

```bash
# Загрузить изображения в Telegram через бота-администратора
python scripts/upload_images.py

# После того как получены file_id — сохранить в Redis/config
# Затем удалить файлы из репозитория
git rm *.png
git commit -m "feat: serve images via Telegram file_id (removes 31MB from repo)"
```

Скрипт загрузки:
```python
# scripts/upload_images.py
import asyncio
from telegram import Bot

BOT_TOKEN = "..."
ADMIN_ID  = ...

async def upload():
    bot = Bot(BOT_TOKEN)
    images = ["posti4.png", "posti9.png", "posti8.png", ...]  # все используемые
    for fname in images:
        with open(fname, "rb") as f:
            msg = await bot.send_photo(chat_id=ADMIN_ID, photo=f)
            file_id = msg.photo[-1].file_id
            print(f'"{fname}": "{file_id}",')

asyncio.run(upload())
```

После получения file_id — создать `image_registry.py`:
```python
# image_registry.py
IMAGE_FILE_IDS = {
    "posti4.png": "AgACAgIAAxkBAAI...",
    "posti9.png": "AgACAgIAAxkBAAI...",
    # и т.д.
}
```

И заменить все `_send_photo(update, "posti4.png", ...)` на:
```python
from image_registry import IMAGE_FILE_IDS
await bot.send_photo(
    chat_id=update.effective_chat.id,
    photo=IMAGE_FILE_IDS.get("posti4.png", ...),
    caption=caption,
    reply_markup=reply_markup,
)
```

---

## Критические баги которые были исправлены

| # | Файл | Строка | Описание | Исправление |
|---|------|--------|----------|-------------|
| 1 | `handlers.py` | 293-294 | Дублирующийся `return True` (второй мёртвый) | Удалён второй `return True` в `flows/onboarding.py` |
| 2 | `llm.py` | 246 | `language="ru"` в Whisper — ломает нерусских | Убран параметр `language` |
| 3 | `db.py` | ~60 | Redis singleton без Lock — race condition | Добавлен `asyncio.Lock()` |
| 4 | `main.py` | webhook | Нет dedup update_id — повторные доставки | `_is_duplicate()` с Redis nx=True |
| 5 | `config.py` | 61 | `LLM_SEMAPHORE_SIZE = 15` — мёртвая переменная | Удалена |
| 6 | `handlers.py` | multiple | `except: pass` (26 мест) — глотает все ошибки | `except TelegramError: pass` |
| 7 | `handlers.py` | ~60 | Меню "← Меню" очищало агент-сессии | Очистка только при старте нового агента |
| 8 | `user_state.py` | all | 5-6 DB запросов на каждое сообщение | Redis-кэш 60 сек |
| 9 | `handlers.py` | 2575 | Монолит — всё в одном файле | Разбит на flows/ handlers/ ui/ |
| 10 | `handlers.py` | daily | Jitter ±30 сек → пики нагрузки | Jitter ±300 сек (5 мин) |

---

## Что ещё нужно сделать (не вошло в рефакторинг)

### Высокий приоритет
- [ ] Загрузить изображения в Telegram, убрать PNG из репозитория
- [ ] Добавить `presence_penalty` в все генерационные вызовы agents.py
- [ ] Инжектировать последние 2-3 результата в чат-режим Миры

### Средний приоритет
- [ ] Унифицировать голос Миры во всех агент-промптах (сейчас каждый агент звучит по-своему)
- [ ] Переименовать бота — убрать "АлоБОТ" везде, оставить только "Мира"
- [ ] Уведомлять пользователя когда сообщение обрезано до 4000 символов
- [ ] TTL для агент-сессий в Redis (сейчас хранятся вечно)

### Низкий приоритет  
- [ ] Перенести промпты из config.py в отдельную директорию `prompts/`
- [ ] Добавить `/admin` команду для просмотра статистики
- [ ] A/B тест текстов пейволла

---

## Запуск тестов

```bash
pip install pytest pytest-asyncio python-telegram-bot --break-system-packages
cd fixed-bot
pytest tests/test_state_machine.py -v --tb=short
```

Ожидаемый вывод:
```
tests/test_state_machine.py::TestUserState::test_new_user_no_niche PASSED
tests/test_state_machine.py::TestUserState::test_onboarded_no_access PASSED
tests/test_state_machine.py::TestUserState::test_trial_state PASSED
...
30 passed in X.Xs
```
