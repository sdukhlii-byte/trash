// src/services/promptBuilder.ts
// Единая точка сборки промптов — никакая бизнес-логика не живёт в роутах

import type { GenerateInput } from '../types'

interface CreatorProfile { niche?: string; audience?: string; tone?: string }
interface VoiceProfile   { metaProfile?: any; approvedSamples?: any; styleNotes?: any }

interface BuildPromptParams {
  toolKey:        string
  input:          GenerateInput
  creatorProfile?: CreatorProfile
  voiceProfile?:  VoiceProfile
  isRegen?:       boolean
  originalContent?: string
}

interface BuildRefinePromptParams {
  action:         string
  toolKey:        string
  currentContent: string
  metadata?:      Record<string, string>
  creatorProfile?: CreatorProfile
  voiceProfile?:  VoiceProfile
}

// ── Profile context builder ────────────────────────────────────────────────────

function buildProfileCtx(p?: CreatorProfile): string {
  if (!p) return ''
  const parts: string[] = []
  if (p.niche)    parts.push(`Ниша: ${p.niche}`)
  if (p.audience) parts.push(`Аудитория: ${p.audience}`)
  if (p.tone)     parts.push(`Тон: ${p.tone}`)
  return parts.length ? '\n\nПРОФИЛЬ АВТОРА:\n' + parts.join('\n') : ''
}

// ── Voice context builder ──────────────────────────────────────────────────────

function buildVoiceCtx(v?: VoiceProfile): string {
  if (!v) return ''
  const parts: string[] = []

  if (v.metaProfile?.summary) {
    parts.push(`Голос автора: ${v.metaProfile.summary}`)
  }
  if (Array.isArray(v.approvedSamples) && v.approvedSamples.length) {
    const samples = v.approvedSamples.slice(-3).join('\n---\n')
    parts.push(`Примеры голоса (одобренные):\n${samples}`)
  }
  if (Array.isArray(v.styleNotes) && v.styleNotes.length) {
    parts.push(`Заметки о стиле: ${v.styleNotes.slice(-3).join(', ')}`)
  }

  return parts.length ? '\n\nГОЛОС АВТОРА:\n' + parts.join('\n\n') : ''
}

// ── System prompts per tool ────────────────────────────────────────────────────
// Упрощённые версии — в production заменяются на полные из Python config.py
// через общую базу промптов (таблица prompts или env-файл)

const TOOL_SYSTEMS: Record<string, (profile: string, voice: string) => string> = {
  post: (p, v) => `Ты — профессиональный копирайтер. Пишешь в голосе автора — живо, без шаблонов.${p}${v}\n\nПиши один готовый пост. Только текст поста — без пояснений.`,

  reels_short: (p, v) => `Ты — эксперт по хукам для Instagram Reels. Создаёшь 14 хуков по разным психологическим триггерам.${p}${v}\n\nФормат: нумерованный список. Каждый хук — новая строка с меткой триггера в скобках.`,

  carousel: (p, v) => `Ты — эксперт по Instagram-каруселям. Создаёшь вовлекающие слайды.${p}${v}\n\nКаждый слайд: заголовок + 2-3 строки текста. Финальный слайд — CTA.`,

  warmup: (p, v) => `Ты — стратег прогревов. Пишешь серию из 3 дней: День 1 строит доверие (без давления), День 2 — трансформация, День 3 — оффер.${p}${v}`,

  stories: (p, v) => `Ты — сторисмейкер. Создаёшь цепочки сторис которые досматривают до конца.${p}${v}`,

  talking_head: (p, v) => `Ты — сценарист монологов для Reels. Пишешь разговорные сценарии с хронометражем.${p}${v}`,

  cartoon: (p, v) => `Ты — сценарист анимационных рилсов. Пишешь с неожиданным поворотом.${p}${v}`,

  profile: (p, v) => `Ты — эксперт по Instagram-стратегии. Делаешь честный разбор аккаунта.${p}${v}\n\nСтруктура: диагностика → топ-3 проблемы (🔴🟡🟢) → что делать сейчас.`,

  competitor: (p, v) => `Ты — стратег по конкурентному анализу.${p}${v}\n\nСтруктура: что делает конкурент → слабые места → стратегия переманивания → 3 шага на этой неделе.`,

  reels_adapt: (p, v) => `Ты — специалист по адаптации вирусного контента.${p}${v}`,

  tg_plan: (p, v) => `Ты — контент-стратег. Создаёшь планы Telegram-каналов с конкретными темами и хуками.${p}${v}`,

  hashtags: (p, v) => `Ты — SEO-специалист. Подбираешь хэштеги: горячие / целевые / нишевые.${p}${v}`,
}

// ── Refine prompts per action ──────────────────────────────────────────────────

const REFINE_INSTRUCTIONS: Record<string, string | ((meta?: Record<string, string>) => string)> = {
  'rs:softer':  'Перепиши хуки в более мягком и поддерживающем тоне. Убери давление, сохрани суть. Нумерованный список с метками механизмов.',
  'rs:bolder':  'Сделай хуки провокационнее: прямые утверждения, вызов, неудобная правда. Нумерованный список.',
  'rs:top5':    'Выбери 5 самых сильных хуков для тестирования. Объясни в одном предложении почему каждый работает. Укажи порядок тестирования.',
  'rs:style':   (meta) => {
    const styleMap: Record<string, string> = {
      curiosity: 'Перепиши хуки через триггер ЛЮБОПЫТСТВО: недосказанность, интрига, "а что если".',
      pain:      'Перепиши хуки через триггер БОЛЬ: конкретная проблема, страх, ошибка аудитории.',
      social:    'Перепиши хуки через СОЦИАЛЬНОЕ ДОКАЗАТЕЛЬСТВО: истории других людей.',
    }
    return styleMap[meta?.style ?? 'curiosity'] ?? styleMap.curiosity
  },
  'car:headline':  'Перепиши только заголовок (первый слайд). Добавь конкретику, цифру или интригу. Верни полную карусель с новым заголовком.',
  'car:softer':    'Смягчи тон карусели: теплее, меньше давления. Сохрани структуру.',
  'car:bolder':    'Сделай карусель острее: провокационнее заголовки слайдов. Сохрани структуру.',
  'car:shorten':   'Сократи: убери слабые слайды, оставь 5-7 сильных. Каждый — ударный.',
  'car:add_slide': 'Добавь один новый слайд — он должен быть самым сильным. Верни полную карусель.',
  'car:trigger':   (meta) => {
    const trigMap: Record<string, string> = {
      fear:      'Перепиши заголовки слайдов через триггер СТРАХ: риск, последствия бездействия.',
      curiosity: 'Перепиши через ЛЮБОПЫТСТВО: недосказанность, "что если", интрига.',
      social:    'Перепиши через СОЦИАЛЬНОЕ ДОКАЗАТЕЛЬСТВО: истории, "она сделала".',
      transform: 'Перепиши через ТРАНСФОРМАЦИЮ: до/после, изменение.',
    }
    return trigMap[meta?.trigger ?? 'curiosity'] ?? trigMap.curiosity
  },
  'car:format':    (meta) => ({
    instruction: `Переформатируй в ${meta?.format === 'instruction' ? 'формат ИНСТРУКЦИЯ/ШАГИ: каждый слайд = один чёткий шаг с действием' : 'формат СПИСОК: каждый слайд = один пункт (ошибка/идея/факт)'}. Сохрани тему.`,
  } as any).instruction ?? 'Переформатируй карусель.',
  'ag:softer':     'Перепиши в более мягком и поддерживающем тоне. Убери давление. Сохрани структуру.',
  'ag:bolder':     'Сделай текст смелее и провокационнее: прямые утверждения, без воды. Сохрани структуру.',
  'ag:shorter':    'Сократи на 30%: убери воду, оставь только главное. Сохрани ключевые идеи.',
  'ag:detail':     'Добавь одну конкретную деталь, историю или цифру — в самом сильном месте текста.',
  'ag:cta':        'Усиль призыв к действию в конце: сделай его конкретным и неотразимым.',
  'ag:resonance':  'Усиль эмоциональный резонанс серии — без усиления давления. Добавь больше узнавания, близости, "это про меня".',
  'ag:add_proof':  'Добавь конкретное социальное доказательство: кейс клиента, цифра, до/после.',
  'ag:mid_hook':   'Перепиши слайды 5-7 (опасная зона). Добавь интригующий вопрос или неожиданный поворот. Остальные слайды не трогай.',
  'ag:hook':       'Перепиши только хук (0-3 сек). Другая боль или заблуждение. Остальное не трогай.',
  'ag:deepen':     'Углуби один раздел — выбери тот, который звучит наиболее обобщённо. Добавь конкретику. Остальные разделы не трогай.',
  'ag:tactics':    'Перепиши раздел "Стратегия переманивания": конкретные темы постов, форматы, дни публикации.',
  'ag:positioning':'Добавь "Дифференцирующую фразу": одно предложение для шапки профиля. Формат: "В отличие от [конкурент], я [отличие] — потому что [причина]."',
  'ag:save_moment':'Найди или добавь главный save-момент карусели: чеклист, мини-инструкция или shareable цитата.',
  'regen':         'Напиши другой вариант того же контента — другой угол, другой стиль, та же тема.',
  'refine':        'Улучши текст сохраняя голос автора.',
}

// ── Public functions ───────────────────────────────────────────────────────────

export async function buildPrompt(params: BuildPromptParams): Promise<{
  system: string
  userPrompt: string
}> {
  const { toolKey, input, creatorProfile, voiceProfile, isRegen, originalContent } = params

  const profileCtx = buildProfileCtx(creatorProfile)
  const voiceCtx   = buildVoiceCtx(voiceProfile)

  const systemFn = TOOL_SYSTEMS[toolKey]
  const system   = systemFn ? systemFn(profileCtx, voiceCtx) : `Ты — AI-ассистент.${profileCtx}${voiceCtx}`

  let userPrompt: string
  if (isRegen && originalContent) {
    userPrompt = `Напиши другой вариант. Оригинал:\n${originalContent}`
  } else {
    userPrompt = input.topic ?? input.description ?? 'Генерируй.'
  }

  return { system, userPrompt }
}

export async function buildRefinePrompt(params: BuildRefinePromptParams): Promise<{
  system: string
  instruction: string
}> {
  const { action, toolKey, currentContent, metadata, creatorProfile, voiceProfile } = params

  const profileCtx = buildProfileCtx(creatorProfile)
  const voiceCtx   = buildVoiceCtx(voiceProfile)

  const system = [
    `Ты — стратегический редактор контента.`,
    `Инструмент: ${toolKey}.`,
    profileCtx,
    voiceCtx,
    `\nТЕКУЩИЙ МАТЕРИАЛ:\n${currentContent}`,
  ].join('')

  const instrRaw = REFINE_INSTRUCTIONS[action]
  const instruction = typeof instrRaw === 'function'
    ? instrRaw(metadata)
    : instrRaw ?? 'Улучши текст.'

  return { system, instruction }
}
