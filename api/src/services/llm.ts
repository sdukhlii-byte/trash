// src/services/llm.ts
// Единственная точка доступа к LLM — зеркало Python llm.py

import axios from 'axios'
import { logger } from '../utils/logger'

const OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
const MODELS = {
  claude: 'anthropic/claude-sonnet-4-5',
  gpt4:   'openai/gpt-4o',
  grok:   'x-ai/grok-3-mini-beta',
}

const LLM_RETRY       = 3
const LLM_TIMEOUT     = 30_000   // ms
const GEN_TIMEOUT     = 180_000  // ms for heavy calls

interface LLMCallParams {
  system:          string
  user:            string
  modelKey?:       'claude' | 'gpt4' | 'grok'
  maxTokens?:      number
  temperature?:    number
  presencePenalty?: number
  heavy?:          boolean
}

export async function callLLM(params: LLMCallParams): Promise<string> {
  const {
    system,
    user,
    modelKey       = 'claude',
    maxTokens      = params.heavy ? 4000 : 2000,
    temperature    = params.heavy ? 0.85  : 0.4,
    presencePenalty,
    heavy          = false,
  } = params

  const payload: Record<string, unknown> = {
    model:      MODELS[modelKey],
    max_tokens: maxTokens,
    temperature,
    messages: [
      { role: 'system', content: system },
      { role: 'user',   content: user   },
    ],
  }
  if (presencePenalty) payload.presence_penalty = presencePenalty

  const timeout = heavy ? GEN_TIMEOUT : LLM_TIMEOUT
  let lastErr: unknown

  for (let attempt = 0; attempt < LLM_RETRY; attempt++) {
    try {
      const resp = await axios.post(OPENROUTER_URL, payload, {
        headers: {
          Authorization: `Bearer ${process.env.OPENROUTER_KEY}`,
          'Content-Type': 'application/json',
          'HTTP-Referer': 'https://t.me/pocket_marketer_bot',
          'X-Title':      'Mira API',
        },
        timeout,
      })

      const content = resp.data?.choices?.[0]?.message?.content
      if (!content) throw new Error('Empty LLM response')
      return content as string

    } catch (err: any) {
      lastErr = err

      if (err.response?.status === 429) {
        const wait = Math.min(
          parseInt(err.response.headers['retry-after'] ?? '2') * 1000,
          30_000,
        )
        await sleep(wait)
        continue
      }

      if (err.response?.status >= 500) {
        await sleep(Math.min(1000 * 2 ** attempt, 10_000))
        continue
      }

      if (axios.isAxiosError(err) && (err.code === 'ECONNABORTED' || err.code === 'ETIMEDOUT')) {
        logger.warn(`LLM timeout attempt ${attempt + 1}`)
        await sleep(Math.min(1000 * 2 ** attempt, 10_000))
        continue
      }

      throw err
    }
  }

  throw new Error(`LLM failed after ${LLM_RETRY} attempts: ${lastErr}`)
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms))
}
