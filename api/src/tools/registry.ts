// src/tools/registry.ts
// Единый реестр инструментов — зеркало Python registry.py
// Каждый инструмент определяет: input, allowed_actions, модель, интервью-шаги.

export interface ToolSpec {
  key:             string
  name:            string
  emoji:           string
  description:     string
  hasInterviewStep: boolean
  hasPickStep:     boolean
  maxQuestions:    number
  acceptsPhotos:   boolean
  model:           'claude' | 'gpt4' | 'grok'
  photo?:          string
  // Допустимые действия из ACTION_REGISTRY
  allowedActions:  string[]
  // Дефолтные allowed_actions при сохранении generation
  defaultAllowedActions: string[]
}

export const TOOLS: Record<string, ToolSpec> = {
  post: {
    key:             'post',
    name:            'Написать за меня',
    emoji:           '✍️',
    description:     'Пост в твоём голосе — не как нейросеть, а как ты',
    hasInterviewStep: true,
    hasPickStep:     false,
    maxQuestions:    5,
    acceptsPhotos:   false,
    model:           'claude',
    photo:           '1napis.png',
    allowedActions:  ['ag:softer','ag:bolder','ag:shorter','ag:detail','ag:cta','ag:hook','regen','refine'],
    defaultAllowedActions: ['ag:softer','ag:bolder','ag:shorter','ag:detail','ag:cta'],
  },

  reels_short: {
    key:             'reels_short',
    name:            'Хуки для рилса',
    emoji:           '🎬',
    description:     '14 хуков под твою тему — выбери лучший',
    hasInterviewStep: false,
    hasPickStep:     false,
    maxQuestions:    0,
    acceptsPhotos:   false,
    model:           'claude',
    photo:           '2huki.png',
    allowedActions:  ['rs:top5','rs:softer','rs:bolder','rs:style','rs:pick_desc','rs:regen','regen','refine'],
    defaultAllowedActions: ['rs:top5','rs:softer','rs:bolder','rs:style','rs:pick_desc'],
  },

  carousel: {
    key:             'carousel',
    name:            'Карусель',
    emoji:           '🎠',
    description:     'Карусель под твою аудиторию — формат и триггер подбирает сама',
    hasInterviewStep: true,
    hasPickStep:     false,
    maxQuestions:    2,
    acceptsPhotos:   false,
    model:           'claude',
    allowedActions:  ['car:headline','car:softer','car:bolder','car:shorten','car:add_slide','car:trigger','car:format','regen','refine'],
    defaultAllowedActions: ['car:headline','car:softer','car:bolder','car:shorten','car:add_slide','car:trigger','car:format'],
  },

  warmup: {
    key:             'warmup',
    name:            'Прогрев',
    emoji:           '🔥',
    description:     'Серия на 3 дня без давления и шаблонов',
    hasInterviewStep: true,
    hasPickStep:     false,
    maxQuestions:    5,
    acceptsPhotos:   false,
    model:           'claude',
    photo:           '5progrev.png',
    allowedActions:  ['ag:resonance','ag:add_proof','ag:softer','ag:cta','regen','refine'],
    defaultAllowedActions: ['ag:resonance','ag:add_proof','ag:softer','ag:cta'],
  },

  stories: {
    key:             'stories',
    name:            'Сторис',
    emoji:           '📸',
    description:     'Цепочка сторис которую досмотрят до конца',
    hasInterviewStep: true,
    hasPickStep:     true,
    maxQuestions:    5,
    acceptsPhotos:   false,
    model:           'claude',
    photo:           '4storis.png',
    allowedActions:  ['ag:softer','ag:bolder','ag:shorter','ag:mid_hook','regen','refine'],
    defaultAllowedActions: ['ag:softer','ag:bolder','ag:shorter','ag:mid_hook'],
  },

  talking_head: {
    key:             'talking_head',
    name:            'Talking Head',
    emoji:           '🎙',
    description:     'Сценарий монолога в кадре — в твоём голосе',
    hasInterviewStep: true,
    hasPickStep:     false,
    maxQuestions:    5,
    acceptsPhotos:   false,
    model:           'claude',
    photo:           'posti6.png',
    allowedActions:  ['ag:softer','ag:bolder','ag:shorter','ag:hook','regen','refine'],
    defaultAllowedActions: ['ag:softer','ag:bolder','ag:shorter','ag:hook'],
  },

  cartoon: {
    key:             'cartoon',
    name:            'Анимация',
    emoji:           '🎭',
    description:     'Сценарий анимационного рилса с вирусным крючком',
    hasInterviewStep: true,
    hasPickStep:     false,
    maxQuestions:    4,
    acceptsPhotos:   false,
    model:           'claude',
    photo:           '8animati.png',
    allowedActions:  ['ag:softer','ag:bolder','ag:shorter','ag:hook','regen','refine'],
    defaultAllowedActions: ['ag:softer','ag:bolder','ag:shorter','ag:hook'],
  },

  profile: {
    key:             'profile',
    name:            'Разбор профиля',
    emoji:           '🔍',
    description:     'Честный разбор аккаунта с конкретикой',
    hasInterviewStep: true,
    hasPickStep:     false,
    maxQuestions:    6,
    acceptsPhotos:   true,
    model:           'claude',
    photo:           '6analitika.png',
    allowedActions:  ['ag:deepen','ag:shorter','regen','refine'],
    defaultAllowedActions: ['ag:deepen','ag:shorter'],
  },

  competitor: {
    key:             'competitor',
    name:            'Разбор конкурента',
    emoji:           '🔎',
    description:     'Анализ конкурента — что взять, что обойти',
    hasInterviewStep: true,
    hasPickStep:     false,
    maxQuestions:    4,
    acceptsPhotos:   true,
    model:           'claude',
    photo:           'posti14.png',
    allowedActions:  ['ag:tactics','ag:positioning','ag:softer','ag:shorter','regen','refine'],
    defaultAllowedActions: ['ag:tactics','ag:positioning','ag:softer','ag:shorter'],
  },

  reels_adapt: {
    key:             'reels_adapt',
    name:            'Адаптация рилса',
    emoji:           '🔄',
    description:     'Вирусный рилс → переупаковка под твою нишу',
    hasInterviewStep: true,
    hasPickStep:     false,
    maxQuestions:    4,
    acceptsPhotos:   false,
    model:           'claude',
    photo:           'posti5.png',
    allowedActions:  ['ag:softer','ag:bolder','ag:shorter','regen','refine'],
    defaultAllowedActions: ['ag:softer','ag:bolder','ag:shorter'],
  },

  tg_plan: {
    key:             'tg_plan',
    name:            'Контент-план TG',
    emoji:           '📅',
    description:     'План канала на 7–14 дней с темами и хуками',
    hasInterviewStep: true,
    hasPickStep:     false,
    maxQuestions:    5,
    acceptsPhotos:   false,
    model:           'claude',
    photo:           'posti11.png',
    allowedActions:  ['ag:softer','ag:bolder','ag:shorter','ag:detail','ag:cta','regen','refine'],
    defaultAllowedActions: ['ag:softer','ag:bolder','ag:shorter','ag:detail'],
  },

  hashtags: {
    key:             'hashtags',
    name:            'Хэштеги и SEO',
    emoji:           '#️⃣',
    description:     'Подбор хэштегов под нишу и платформу',
    hasInterviewStep: true,
    hasPickStep:     false,
    maxQuestions:    2,
    acceptsPhotos:   false,
    model:           'claude',
    allowedActions:  ['regen','refine'],
    defaultAllowedActions: [],
  },
}

export function getTool(key: string): ToolSpec | undefined {
  return TOOLS[key]
}

export function getToolList(): ToolSpec[] {
  return Object.values(TOOLS)
}
