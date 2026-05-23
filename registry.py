"""
registry.py — регистрация всех 9 агентов.
Импортируй этот модуль один раз при старте бота.
"""

from agents import AgentSpec, register
from config import (
    PROFILE_INTERVIEWER,   PROFILE_GENERATOR,
    STORIES_INTERVIEWER,   STORIES_GENERATOR,
    REELS_ADAPT_INTERVIEWER, REELS_ADAPT_GENERATOR,
    TELEGRAM_PLAN_INTERVIEWER, TELEGRAM_PLAN_GENERATOR,
    build_reels_short_headline_system, build_reels_short_desc_system,
    WARMUP_INTERVIEWER,    WARMUP_GENERATOR,
    build_carousel_system,
    CAROUSEL_INTERVIEWER, CAROUSEL_HEADLINE_SYSTEM,
    CAROUSEL_TREND_SYSTEM,
    CAROUSEL_FORMATS_4,   CAROUSEL_TRIGGERS_20,
    TALKING_HEAD_INTERVIEWER, TALKING_HEAD_GENERATOR,
    CARTOON_INTERVIEWER,  CARTOON_GENERATOR,
)


# ── 1. Анализ профиля ─────────────────────────────────────────────────────────
register(AgentSpec(
    key          = "profile",
    name         = "Анализ профиля",
    emoji        = "🔍",
    welcome      = (
        "🔍 *Анализ профиля*\n\n"
        "Опиши свой аккаунт свободно:\n\n"
        "• Ниша и что продаёшь\n"
        "• Кто твоя аудитория\n"
        "• Что сейчас выходит в контенте\n"
        "• Что не работает по ощущениям\n\n"
        "_Чем больше деталей — тем точнее разбор_"
    ),
    interviewer  = PROFILE_INTERVIEWER,
    generator    = PROFILE_GENERATOR,
    final_prompt = "Напиши полный разбор аккаунта строго по структуре. Используй профиль автора и все данные из интервью. Без вступлений — сразу по делу.",
    as_file      = True,
    file_prefix  = "Разбор_аккаунта",
    max_q        = 6,
    accept_photos= True,
    model        = "claude",
))


# ── 2. Сторисмау ──────────────────────────────────────────────────────────────
register(AgentSpec(
    key              = "stories",
    name             = "Сторисмау",
    emoji            = "📸",
    welcome          = (
        "📸 *Сторисмау*\n\n"
        "Расскажи историю или идею для сторис — в любом виде.\n\n"
        "_Что случилось, о чём хочешь рассказать, какую мысль донести_"
    ),
    interviewer      = STORIES_INTERVIEWER,
    generator        = STORIES_GENERATOR,
    # Stage 1: генерируем 10 вариантов захода
    final_prompt     = (
        "Напиши ровно 10 вариантов ЗАХОДА (первые 1-2 слайда) — по одному на каждый триггер.\n"
        "Формат: [N]. [Триггер]: «Первая фраза»\n"
        "После списка напиши: «Какой заход резонирует? Напиши номер — и я напишу полный сценарий.»"
    ),
    # Stage 2: пользователь выбрал → пишем полный сценарий
    final_prompt_pick = (
        "Напиши полный пошаговый сценарий сторис (6-12 слайдов) для выбранного захода.\n"
        "Формат: Слайд N: [текст] | [эмоция/действие] | [интерактив если уместно]\n"
        "Финальный слайд — обязательно с CTA."
    ),
    has_pick_step    = True,
    pick_prompt      = "Напиши номер понравившегося захода:",
    max_q            = 5,
    model            = "claude",
))


# ── 3. Рилс-адаптация ─────────────────────────────────────────────────────────
register(AgentSpec(
    key          = "reels_adapt",
    name         = "Рилс-адаптация",
    emoji        = "🔄",
    welcome      = (
        "🔄 *Рилс-адаптация*\n\n"
        "Опиши рилс, который хочешь адаптировать:\n\n"
        "• Что в нём происходит\n"
        "• Что именно зацепило\n"
        "• Под какую нишу / аудиторию адаптируем"
    ),
    interviewer  = REELS_ADAPT_INTERVIEWER,
    generator    = REELS_ADAPT_GENERATOR,
    final_prompt = "Напиши адаптированный сценарий рилса строго по структуре. Адаптируй под аудиторию и тон голоса из профиля автора.",
    max_q        = 4,
    model        = "claude",
))


# ── 4. Змей-Телеграммыч ───────────────────────────────────────────────────────
register(AgentSpec(
    key          = "tg_plan",
    name         = "Змей-Телеграммыч",
    emoji        = "🐍",
    welcome      = (
        "🐍 *Змей-Телеграммыч*\n\n"
        "Расскажи о своём Telegram-канале:\n\n"
        "• О чём канал и для кого\n"
        "• Что продаёшь или продвигаешь\n"
        "• Какой период нужен (7 или 14 дней)\n"
        "• Какие форматы используешь сейчас"
    ),
    interviewer  = TELEGRAM_PLAN_INTERVIEWER,
    generator    = TELEGRAM_PLAN_GENERATOR,
    final_prompt = "Создай детальный контент-план строго по структуре. Каждая тема и хук — через призму аудитории из профиля. Конкретные формулировки, не шаблоны.",
    max_q        = 5,
    model        = "claude",
))


# ── 5. Рилс-коротышка ─────────────────────────────────────────────────────────
# Этот агент работает иначе: не интервью, а тема → 14 заголовков → выбор → описание.
# Реализован напрямую в handlers.py (специфичная логика), здесь только запись для меню.
register(AgentSpec(
    key          = "reels_short",
    name         = "Рилс-коротышка",
    emoji        = "🎬",
    welcome      = (
        "🎬 *Рилс-коротышка*\n\n"
        "Напиши тему рилса — одним словом или коротко.\n\n"
        "_Например: делегирование / выгорание / как поднять цену_"
    ),
    interviewer  = "",   # не используется — custom flow в handlers.py
    generator    = "",
    final_prompt = "",
    max_q        = 0,
    model        = "claude",
))


# ── 6. Сценарист прогрева ─────────────────────────────────────────────────────
register(AgentSpec(
    key          = "warmup",
    name         = "Сценарист прогрева",
    emoji        = "🔥",
    welcome      = (
        "🔥 *Сценарист прогрева*\n\n"
        "Расскажи о продукте:\n\n"
        "• Что продаёшь и по какой цене\n"
        "• Кто покупатель — боли и желания\n"
        "• Главные возражения\n"
        "• Есть ли дедлайн/акция"
    ),
    interviewer  = WARMUP_INTERVIEWER,
    generator    = WARMUP_GENERATOR,
    final_prompt = "Напиши детальный прогрев на 3 дня с конкретными текстами. Используй профиль автора: язык ЦА, их возражения, их боли. Не шаблон — живые тексты.",
    max_q        = 5,
    model        = "claude",
))


# ── 7. Каруселькин ────────────────────────────────────────────────────────────
# Специфичная логика (тренд → заголовки → формат → триггер → интервью → карусель)
# реализована в handlers.py. Здесь — запись для меню.
register(AgentSpec(
    key          = "carousel",
    name         = "Каруселькин",
    emoji        = "🎠",
    welcome      = (
        "🎠 *Каруселькин*\n\n"
        "Напиши тему карусели — одним предложением.\n\n"
        "_Например: «5 ошибок при запуске рекламы» или «как поднять цену без потери клиентов»_"
    ),
    interviewer  = "",   # custom flow
    generator    = "",
    final_prompt = "",
    max_q        = 0,
    model        = "claude",
))


# ── 8. Разговорные рилс ───────────────────────────────────────────────────────
register(AgentSpec(
    key          = "talking_head",
    name         = "Разговорные рилс",
    emoji        = "🎙",
    welcome      = (
        "🎙 *Разговорные рилс*\n\n"
        "Создадим сценарий монолога в кадре — под твою аудиторию и твой голос.\n\n"
        "Начни с темы: о чём хочешь снять рилс?\n"
        "_Можно одним словом или развёрнуто — дальше я вытащу нужное_"
    ),
    interviewer  = TALKING_HEAD_INTERVIEWER,
    generator    = TALKING_HEAD_GENERATOR,
    final_prompt = (
        "Напиши готовый сценарий разговорного рилса строго по структуре.\n"
        "ОБЯЗАТЕЛЬНО используй профиль автора: нишу, целевую аудиторию, тон голоса.\n"
        "Сценарий должен звучать как этот конкретный эксперт — не как шаблон.\n"
        "Включи все режиссёрские пометки и хронометраж блоков."
    ),
    max_q        = 5,
    model        = "claude",
))


# ── 9. Агент по мультикам ─────────────────────────────────────────────────────
register(AgentSpec(
    key          = "cartoon",
    name         = "Агент по мультикам",
    emoji        = "🎭",
    welcome      = (
        "🎭 *Агент по мультикам*\n\n"
        "Расскажи идею вирусного мультика:\n\n"
        "_Ситуация, персонажи, неожиданный поворот, тон (абсурд / сатира / милота)_"
    ),
    interviewer  = CARTOON_INTERVIEWER,
    generator    = CARTOON_GENERATOR,
    final_prompt = "Напиши полный сценарий мультика строго по структуре. Персонажи и ситуации — узнаваемые для аудитории из профиля. + 3 идеи для продолжения.",
    max_q        = 4,
    model        = "claude",
))


# ── 10. Постовик ──────────────────────────────────────────────────────────────
from config import POST_INTERVIEWER, POST_GENERATOR

register(AgentSpec(
    key          = "post",
    name         = "Постовик",
    emoji        = "✍️",
    welcome      = (
        "✍️ *Постовик*\n\n"
        "Напишем живой текстовый пост для Instagram или Telegram.\n\n"
        "_Расскажи тему или идею — что хочешь донести до аудитории?_"
    ),
    interviewer  = POST_INTERVIEWER,
    generator    = POST_GENERATOR,
    final_prompt = "Напиши готовый текстовый пост в тоне голоса из профиля автора. Первая строка — под боль/желание ЦА. Только текст — без пояснений.",
    max_q        = 3,
    model        = "claude",
))


# ── 11. Хэштеги и SEO ─────────────────────────────────────────────────────────
from config import HASHTAG_SYSTEM

register(AgentSpec(
    key          = "hashtags",
    name         = "Хэштеги и SEO",
    emoji        = "#️⃣",
    welcome      = (
        "#️⃣ *Хэштеги и SEO*\n\n"
        "Напиши тему поста или вставь готовый текст — подберу хэштеги.\n\n"
        "_Укажи платформу если важно (Instagram / Telegram / Reels)_"
    ),
    interviewer  = (
        "Ты — SEO-специалист. Уточняешь детали для подбора хэштегов.\n"
        "Задай ОДИН уточняющий вопрос если нужно (платформа, дополнительные темы).\n"
        "Если темы достаточно — скажи ровно: «Генерирую хэштеги.»"
    ),
    generator    = HASHTAG_SYSTEM,
    final_prompt = "Подбери хэштеги строго по структуре: горячие / целевые / нишевые + итоговый список.",
    max_q        = 2,
    model        = "claude",
))


# ── 12. Разбор конкурента ─────────────────────────────────────────────────────
from config import COMPETITOR_INTERVIEWER, COMPETITOR_GENERATOR

register(AgentSpec(
    key          = "competitor",
    name         = "Разбор конкурента",
    emoji        = "🔎",
    welcome      = (
        "🔎 *Разбор конкурента*\n\n"
        "Разберём аккаунт конкурента — что взять, что обойти.\n\n"
        "_Напиши имя/ссылку аккаунта или опиши его_"
    ),
    interviewer  = COMPETITOR_INTERVIEWER,
    generator    = COMPETITOR_GENERATOR,
    final_prompt = "Сделай полный разбор конкурента строго по структуре. Всё — через призму ниши и аудитории автора. Конкретные рекомендации, не общие слова.",
    accept_photos= True,
    max_q        = 4,
    model        = "claude",
))
