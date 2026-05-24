"""
registry.py — регистрация всех агентов.
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


# ── 1. Разбор профиля ────────────────────────────────────────────────────────
register(AgentSpec(
    key          = "profile",
    name         = "Разбор профиля",
    emoji        = "🔍",
    welcome      = (
        "🔍 *Разбор профиля*\n\n"
        "Посмотрю на твой аккаунт честно — без лести, с конкретикой.\n\n"
        "Опиши свой профиль:\n"
        "• Ниша и что продаёшь\n"
        "• Кто твоя аудитория\n"
        "• Что сейчас выходит в контенте\n"
        "• Что, по ощущениям, не работает\n\n"
        "_Чем подробнее — тем точнее разбор_"
    ),
    interviewer  = PROFILE_INTERVIEWER,
    generator    = PROFILE_GENERATOR,
    final_prompt = "Напиши полный разбор аккаунта строго по структуре. Используй профиль автора и все данные из интервью. Без вступлений — сразу по делу.",
    as_file      = True,
    file_prefix  = "Разбор_аккаунта",
    max_q        = 6,
    accept_photos= True,
    model        = "claude",
    photo        = "posti13.png",
))


# ── 2. Сторис ────────────────────────────────────────────────────────────────
register(AgentSpec(
    key              = "stories",
    name             = "Сторис",
    emoji            = "📸",
    welcome          = (
        "📸 *Сторис*\n\n"
        "Напишем цепочку, которую досмотрят до конца.\n\n"
        "Расскажи — что хочешь донести или что произошло?\n"
        "_Идея, история, инсайт — в любом виде_"
    ),
    interviewer      = STORIES_INTERVIEWER,
    generator        = STORIES_GENERATOR,
    final_prompt     = (
        "Напиши ровно 10 вариантов ЗАХОДА (первые 1-2 слайда) — по одному на каждый триггер.\n"
        "Формат: [N]. [Триггер]: «Первая фраза»\n"
        "После списка напиши: «Какой заход резонирует? Напиши номер — и я напишу полный сценарий.»"
    ),
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


# ── 3. Адаптация рилса ───────────────────────────────────────────────────────
register(AgentSpec(
    key          = "reels_adapt",
    name         = "Адаптация рилса",
    emoji        = "🔄",
    welcome      = (
        "🔄 *Адаптация рилса*\n\n"
        "Берём чужой вирусный рилс — и переупаковываем под твою нишу и голос.\n\n"
        "Опиши рилс, который зацепил:\n"
        "• Что в нём происходит\n"
        "• Что именно сработало\n"
        "• Под какую аудиторию адаптируем"
    ),
    interviewer  = REELS_ADAPT_INTERVIEWER,
    generator    = REELS_ADAPT_GENERATOR,
    final_prompt = "Напиши адаптированный сценарий рилса строго по структуре. Адаптируй под аудиторию и тон голоса из профиля автора.",
    max_q        = 4,
    model        = "claude",
    photo        = "posti5.png",
))


# ── 4. Контент-план TG ───────────────────────────────────────────────────────
register(AgentSpec(
    key          = "tg_plan",
    name         = "Контент-план TG",
    emoji        = "📅",
    welcome      = (
        "📅 *Контент-план TG*\n\n"
        "Составим план канала на 7 или 14 дней — с темами, форматами и хуками.\n\n"
        "Расскажи о канале:\n"
        "• О чём и для кого\n"
        "• Что продвигаешь или продаёшь\n"
        "• Период — 7 или 14 дней\n"
        "• Какие форматы используешь сейчас"
    ),
    interviewer  = TELEGRAM_PLAN_INTERVIEWER,
    generator    = TELEGRAM_PLAN_GENERATOR,
    final_prompt = "Создай детальный контент-план строго по структуре. Каждая тема и хук — через призму аудитории из профиля. Конкретные формулировки, не шаблоны.",
    max_q        = 5,
    model        = "claude",
    photo        = "posti11.png",
))


# ── 5. Хуки для рилса ────────────────────────────────────────────────────────
# Custom flow в handlers.py
register(AgentSpec(
    key          = "reels_short",
    name         = "Хуки для рилса",
    emoji        = "🎬",
    welcome      = (
        "🎬 *Хуки для рилса*\n\n"
        "Напиши тему — дам 14 заголовков по разным триггерам.\n\n"
        "_Например: делегирование / выгорание / как поднять цену_"
    ),
    interviewer  = "",
    generator    = "",
    final_prompt = "",
    max_q        = 0,
    model        = "claude",
))


# ── 6. Прогрев ───────────────────────────────────────────────────────────────
register(AgentSpec(
    key          = "warmup",
    name         = "Прогрев",
    emoji        = "🔥",
    welcome      = (
        "🔥 *Прогрев*\n\n"
        "Напишем серию, которая ведёт к покупке — без давления и шаблонов.\n\n"
        "Расскажи о продукте:\n"
        "• Что продаёшь и по какой цене\n"
        "• Кто покупает — их боли и желания\n"
        "• Главные возражения\n"
        "• Есть ли дедлайн или акция"
    ),
    interviewer  = WARMUP_INTERVIEWER,
    generator    = WARMUP_GENERATOR,
    final_prompt = "Напиши детальный прогрев на 3 дня с конкретными текстами. Используй профиль автора: язык ЦА, их возражения, их боли. Не шаблон — живые тексты.",
    max_q        = 5,
    model        = "claude",
))


# ── 7. Карусель ──────────────────────────────────────────────────────────────
# Custom flow в handlers.py
register(AgentSpec(
    key          = "carousel",
    name         = "Карусель",
    emoji        = "🎠",
    welcome      = (
        "🎠 *Карусель*\n\n"
        "Напиши тему — подберём формат, триггер и соберём карусель под твою аудиторию.\n\n"
        "_Например: «5 ошибок при запуске рекламы» или «как поднять цену без потери клиентов»_"
    ),
    interviewer  = "",
    generator    = "",
    final_prompt = "",
    max_q        = 0,
    model        = "claude",
))


# ── 8. Talking Head ──────────────────────────────────────────────────────────
register(AgentSpec(
    key          = "talking_head",
    name         = "Talking Head",
    emoji        = "🎙",
    welcome      = (
        "🎙 *Talking Head*\n\n"
        "Напишем сценарий монолога в кадре — в твоём голосе, под твою аудиторию.\n\n"
        "О чём хочешь снять рилс?\n"
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
    photo        = "posti6.png",
))


# ── 9. Анимация ──────────────────────────────────────────────────────────────
register(AgentSpec(
    key          = "cartoon",
    name         = "Анимация",
    emoji        = "🎭",
    welcome      = (
        "🎭 *Анимация*\n\n"
        "Напишем сценарий анимационного рилса — с персонажами, поворотом и виральным крючком.\n\n"
        "Расскажи идею:\n"
        "_Ситуация, персонажи, неожиданный поворот, тон — абсурд, сатира или что-то своё_"
    ),
    interviewer  = CARTOON_INTERVIEWER,
    generator    = CARTOON_GENERATOR,
    final_prompt = "Напиши полный сценарий мультика строго по структуре. Персонажи и ситуации — узнаваемые для аудитории из профиля. + 3 идеи для продолжения.",
    max_q        = 4,
    model        = "claude",
))


# ── 10. Написать за меня ─────────────────────────────────────────────────────
from config import POST_INTERVIEWER, POST_GENERATOR

register(AgentSpec(
    key          = "post",
    name         = "Написать за меня",
    emoji        = "✍️",
    welcome      = (
        "✍️ *Написать за меня*\n\n"
        "Напишу пост в твоём голосе — не как нейросеть, а как ты.\n\n"
        "Расскажи тему или идею — что хочешь донести до своей аудитории?\n"
        "_Можно вброситься черновиком, тезисом или просто мыслью_"
    ),
    interviewer  = POST_INTERVIEWER,
    generator    = POST_GENERATOR,
    final_prompt = "Напиши готовый текстовый пост в тоне голоса из профиля автора. Первая строка — под боль/желание ЦА. Только текст — без пояснений.",
    max_q        = 3,
    model        = "claude",
))


# ── 11. Хэштеги и SEO ────────────────────────────────────────────────────────
from config import HASHTAG_SYSTEM

register(AgentSpec(
    key          = "hashtags",
    name         = "Хэштеги и SEO",
    emoji        = "#️⃣",
    welcome      = (
        "#️⃣ *Хэштеги и SEO*\n\n"
        "Подберу хэштеги под твою нишу и платформу.\n\n"
        "Напиши тему поста или вставь готовый текст.\n"
        "_Укажи платформу если важно: Instagram, Reels, Telegram_"
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


# ── 12. Разбор конкурента ────────────────────────────────────────────────────
from config import COMPETITOR_INTERVIEWER, COMPETITOR_GENERATOR

register(AgentSpec(
    key          = "competitor",
    name         = "Разбор конкурента",
    emoji        = "🔎",
    welcome      = (
        "🔎 *Разбор конкурента*\n\n"
        "Посмотрим, что делают другие — и как обойти или взять лучшее.\n\n"
        "Напиши имя аккаунта, ссылку или опиши его.\n"
        "_Можно прикрепить скриншот профиля_"
    ),
    interviewer  = COMPETITOR_INTERVIEWER,
    generator    = COMPETITOR_GENERATOR,
    final_prompt = "Сделай полный разбор конкурента строго по структуре. Всё — через призму ниши и аудитории автора. Конкретные рекомендации, не общие слова.",
    accept_photos= True,
    max_q        = 4,
    model        = "claude",
    photo        = "posti14.png",
))
