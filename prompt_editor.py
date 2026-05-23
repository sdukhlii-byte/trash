"""
prompt_editor.py

Редактор системных промптов — позволяет пользователю просматривать,
редактировать и сбрасывать любой из системных промптов бота.

Хранение: KV-таблица, ключ __prompt__{slug}__
Интеграция: вызывай get_prompt(user_id, slug, default) вместо прямого
            обращения к константам из config.py.
"""

from __future__ import annotations
import logging
from telegram import Update
from telegram.ext import ContextTypes
from db import kv_get, kv_set, kv_del, get_agent_session, save_agent_session, clear_agent_session
from utils import send, edit, kb

logger = logging.getLogger(__name__)

# ── Flow key ──────────────────────────────────────────────────────────────────
_PE_KEY = "prompt_editor_flow"


# ── Реестр всех редактируемых промптов ───────────────────────────────────────
# Формат: slug → (категория, название, описание)
PROMPT_REGISTRY: dict[str, tuple[str, str, str]] = {
    # Агенты — интервьюеры
    "profile_interviewer":        ("agents", "🔍 Анализ профиля — интервью",        "Вопросы для сбора инфо об аккаунте"),
    "profile_generator":          ("agents", "🔍 Анализ профиля — генератор",        "Финальный разбор аккаунта"),
    "stories_interviewer":        ("agents", "📸 Сторисмау — интервью",              "Вопросы для сценария сторис"),
    "stories_generator":          ("agents", "📸 Сторисмау — генератор",             "Генерация вариантов захода"),
    "reels_adapt_interviewer":    ("agents", "🔄 Рилс-адаптация — интервью",         "Вопросы для адаптации рилса"),
    "reels_adapt_generator":      ("agents", "🔄 Рилс-адаптация — генератор",        "Генерация адаптированного сценария"),
    "tg_plan_interviewer":        ("agents", "🐍 Змей-Телеграммыч — интервью",       "Вопросы для плана Telegram"),
    "tg_plan_generator":          ("agents", "🐍 Змей-Телеграммыч — генератор",      "Генерация плана Telegram-канала"),
    "warmup_interviewer":         ("agents", "🔥 Сценарист прогрева — интервью",     "Вопросы для прогрева/запуска"),
    "warmup_generator":           ("agents", "🔥 Сценарист прогрева — генератор",    "Генерация прогрева"),
    "talking_head_interviewer":   ("agents", "🎙 Разговорные рилс — интервью",       "Вопросы для монолога в кадре"),
    "talking_head_generator":     ("agents", "🎙 Разговорные рилс — генератор",      "Генерация сценария монолога"),
    "cartoon_interviewer":        ("agents", "🎭 Агент по мультикам — интервью",     "Вопросы для анимационного рилса"),
    "cartoon_generator":          ("agents", "🎭 Агент по мультикам — генератор",    "Генерация сценария мультика"),
    "competitor_interviewer":     ("agents", "🔎 Разбор конкурента — интервью",      "Вопросы для анализа конкурента"),
    "competitor_generator":       ("agents", "🔎 Разбор конкурента — генератор",     "Генерация разбора конкурента"),
    # Каруселькин
    "carousel_trend":             ("carousel", "🎠 Оценка темы",                     "Анализ потенциала темы карусели"),
    "carousel_headlines":         ("carousel", "🎠 Заголовки карусели",               "20 заголовков по триггерам"),
    "carousel_interviewer":       ("carousel", "🎠 Интервью карусели",                "Сбор внутрянки перед генерацией"),
    # Рилс-коротышка
    "reels_short_headlines":      ("reels",   "🎬 Рилс — заголовки",                 "14 заголовков по триггерам"),
    "reels_short_desc":           ("reels",   "🎬 Рилс — описание",                  "Описание + CTA + хэштеги"),
    # Утилиты
    "quick_ideas":                ("tools",   "💡 10 идей быстро",                   "Генерация 10 идей для постов"),
    "hashtags":                   ("tools",   "#️⃣ Хэштеги",                          "Подбор хэштегов для поста"),
    "refine":                     ("tools",   "✏️ Доработка текста",                  "Редактирование готового текста"),
    "regen":                      ("tools",   "🔄 Другой вариант",                    "Альтернативная версия текста"),
    "daily_digest":               ("tools",   "☀️ Дейли-режим",                       "Утреннее сообщение с идеей дня"),
    "planner":                    ("tools",   "📅 Планировщик — план недели",         "Генерация контент-плана"),
    "chat":                       ("tools",   "💬 Чат-режим",                         "Системный промпт чата"),
}

CATEGORIES = {
    "agents":   "🤖 Агенты",
    "carousel": "🎠 Каруселькин",
    "reels":    "🎬 Рилс-коротышка",
    "tools":    "🛠 Инструменты",
}


# ── KV helpers ────────────────────────────────────────────────────────────────

def _kv_key(slug: str) -> str:
    return f"__prompt__{slug}__"


async def get_prompt(user_id: int, slug: str, default: str) -> str:
    """Возвращает кастомный промпт если есть, иначе default."""
    custom = await kv_get(user_id, _kv_key(slug))
    return custom if custom else default


async def set_prompt(user_id: int, slug: str, text: str) -> None:
    await kv_set(user_id, _kv_key(slug), text)


async def reset_prompt(user_id: int, slug: str) -> None:
    await kv_del(user_id, _kv_key(slug))


async def has_custom(user_id: int, slug: str) -> bool:
    v = await kv_get(user_id, _kv_key(slug))
    return bool(v)


# ── UI helpers ────────────────────────────────────────────────────────────────

def _cat_kb() -> object:
    """Клавиатура выбора категории."""
    rows = [[f"{label}|pe_cat_{slug}"] for slug, label in CATEGORIES.items()]
    rows.append(["← Меню|menu_main"])
    return kb(*rows)


def _prompts_kb(category: str) -> object:
    """Клавиатура со списком промптов категории."""
    rows = []
    for slug, (cat, name, _) in PROMPT_REGISTRY.items():
        if cat == category:
            rows.append([f"{name}|pe_view_{slug}"])
    rows.append([f"← Назад|pe_back_cats"])
    return kb(*rows)


def _prompt_action_kb(slug: str, is_custom: bool) -> object:
    """Кнопки действий для конкретного промпта."""
    rows = [
        [f"✏️ Редактировать|pe_edit_{slug}"],
    ]
    if is_custom:
        rows.append([f"🔄 Сбросить к дефолту|pe_reset_{slug}"])
    rows.append([f"← Список|pe_back_list_{slug}"])
    return kb(*rows)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def pe_menu(update: Update, user_id: int) -> None:
    """Главное меню редактора промптов."""
    await clear_agent_session(user_id, _PE_KEY)
    await send(update,
               "🛠 *Редактор промптов*\n\n"
               "Здесь можно изменить любой системный промпт бота.\n"
               "Изменения работают только для тебя — дефолты не затрагиваются.\n\n"
               "Выбери категорию:",
               parse_mode="Markdown",
               reply_markup=_cat_kb())


async def pe_show_category(update: Update, user_id: int, category: str, query=None) -> None:
    label = CATEGORIES.get(category, category)
    text = f"*{label}*\n\nВыбери промпт для просмотра или редактирования:"
    markup = _prompts_kb(category)
    if query:
        await edit(query, text, parse_mode="Markdown", reply_markup=markup)
    else:
        await send(update, text, parse_mode="Markdown", reply_markup=markup)


async def pe_view_prompt(update: Update, user_id: int, slug: str, query=None) -> None:
    """Показывает текущий промпт (кастомный или дефолтный)."""
    if slug not in PROMPT_REGISTRY:
        msg = "Промпт не найден."
        if query: await edit(query, msg)
        else: await send(update, msg)
        return

    _, name, desc = PROMPT_REGISTRY[slug]
    default = _get_default(slug)
    custom = await kv_get(user_id, _kv_key(slug))
    is_custom = bool(custom)
    current = custom if is_custom else default

    status = "✏️ *кастомный*" if is_custom else "📋 *стандартный*"
    preview = current[:800] + ("…" if len(current) > 800 else "")
    text = (f"*{name}*\n_{desc}_\n\nСтатус: {status}\n\n"
            f"```\n{preview}\n```")

    markup = _prompt_action_kb(slug, is_custom)
    if query:
        await edit(query, text, parse_mode="Markdown", reply_markup=markup)
    else:
        await send(update, text, parse_mode="Markdown", reply_markup=markup)


async def pe_start_edit(update: Update, user_id: int, slug: str, query=None) -> None:
    """Начинает flow редактирования — просит прислать новый текст промпта."""
    if slug not in PROMPT_REGISTRY:
        return
    _, name, _ = PROMPT_REGISTRY[slug]
    default = _get_default(slug)
    custom = await kv_get(user_id, _kv_key(slug))
    current = custom if custom else default

    await save_agent_session(user_id, _PE_KEY, {"step": "await_text", "slug": slug})

    hint = (f"✏️ *Редактируешь:* {name}\n\n"
            "Пришли новый текст промпта. Он полностью заменит текущий.\n\n"
            "💡 *Совет:* скопируй текущий промпт ниже, отредактируй и пришли обратно.\n\n"
            f"*Текущий промпт:*\n```\n{current[:1500]}\n```"
            + ("\n\n_...промпт обрезан для отображения, полный будет сохранён_" if len(current) > 1500 else ""))

    if query:
        await edit(query, hint, parse_mode="Markdown",
                   reply_markup=kb([f"❌ Отмена|pe_view_{slug}"]))
    else:
        await send(update, hint, parse_mode="Markdown",
                   reply_markup=kb([f"❌ Отмена|pe_view_{slug}"]))


async def pe_save_text(update: Update, user_id: int, text: str, s: dict) -> None:
    """Сохраняет новый текст промпта."""
    slug = s.get("slug", "")
    if not slug or slug not in PROMPT_REGISTRY:
        await clear_agent_session(user_id, _PE_KEY)
        await send(update, "Что-то пошло не так. Начни заново.",
                   reply_markup=kb(["🛠 Редактор промптов|pe_menu"]))
        return

    await set_prompt(user_id, slug, text)
    await clear_agent_session(user_id, _PE_KEY)

    _, name, _ = PROMPT_REGISTRY[slug]
    await send(update,
               f"✅ *Промпт сохранён!*\n\n*{name}*\n\n"
               f"Новый промпт ({len(text)} симв.) будет использоваться при следующей генерации.",
               parse_mode="Markdown",
               reply_markup=kb([f"👁 Посмотреть|pe_view_{slug}",
                                 "🛠 Ещё промпты|pe_menu",
                                 "← Меню|menu_main"]))


async def pe_reset(update: Update, user_id: int, slug: str, query=None) -> None:
    """Сбрасывает кастомный промпт к дефолтному."""
    await reset_prompt(user_id, slug)
    _, name, _ = PROMPT_REGISTRY[slug]
    text = f"🔄 *Промпт сброшен к стандартному*\n\n*{name}*"
    if query:
        await edit(query, text, parse_mode="Markdown",
                   reply_markup=kb([f"👁 Посмотреть|pe_view_{slug}",
                                     "🛠 Ещё промпты|pe_menu"]))
    else:
        await send(update, text, parse_mode="Markdown",
                   reply_markup=kb([f"👁 Посмотреть|pe_view_{slug}",
                                     "🛠 Ещё промпты|pe_menu"]))


# ── Default prompts lookup ────────────────────────────────────────────────────

def _get_default(slug: str) -> str:
    """Возвращает дефолтный промпт по slug из config.py."""
    import config as c
    _MAP = {
        "profile_interviewer":      c.PROFILE_INTERVIEWER,
        "profile_generator":        c.PROFILE_GENERATOR,
        "stories_interviewer":      c.STORIES_INTERVIEWER,
        "stories_generator":        c.STORIES_GENERATOR,
        "reels_adapt_interviewer":  c.REELS_ADAPT_INTERVIEWER,
        "reels_adapt_generator":    c.REELS_ADAPT_GENERATOR,
        "tg_plan_interviewer":      c.TELEGRAM_PLAN_INTERVIEWER,
        "tg_plan_generator":        c.TELEGRAM_PLAN_GENERATOR,
        "warmup_interviewer":       c.WARMUP_INTERVIEWER,
        "warmup_generator":         c.WARMUP_GENERATOR,
        "talking_head_interviewer": c.TALKING_HEAD_INTERVIEWER,
        "talking_head_generator":   c.TALKING_HEAD_GENERATOR,
        "cartoon_interviewer":      c.CARTOON_INTERVIEWER,
        "cartoon_generator":        c.CARTOON_GENERATOR,
        "competitor_interviewer":   c.COMPETITOR_INTERVIEWER,
        "competitor_generator":     c.COMPETITOR_GENERATOR,
        "carousel_trend":           c.CAROUSEL_TREND_SYSTEM,
        "carousel_headlines":       c.CAROUSEL_HEADLINE_SYSTEM,
        "carousel_interviewer":     c.CAROUSEL_INTERVIEWER,
        "reels_short_headlines":    c.build_reels_short_headline_system("{{ниша}}", "{{аудитория}}", "{{тон}}"),
        "reels_short_desc":         c.build_reels_short_desc_system("{{ниша}}", "{{аудитория}}", "{{тон}}"),
        "quick_ideas":              c.QUICK_IDEAS_SYSTEM,
        "hashtags":                 c.HASHTAG_SYSTEM,
        "refine":                   c.REFINE_SYSTEM,
        "regen":                    c.REGEN_SYSTEM,
        "daily_digest":             c.DAILY_DIGEST_SYSTEM,
        "planner":                  c.PLANNER_IDEAS_SYSTEM,
        "chat":                     c.CHAT_SYSTEM,
    }
    return _MAP.get(slug, "Промпт не найден.")


def get_category_for_slug(slug: str) -> str:
    """Возвращает категорию промпта по slug."""
    info = PROMPT_REGISTRY.get(slug)
    return info[0] if info else "tools"
