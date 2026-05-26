// src/services/generation.ts
// Централизованный generation pipeline — 10 этапов

import { prisma } from '../db/prisma'
import { cache } from '../db/redis'
import { getTool } from '../tools/registry'
import { buildPrompt } from './promptBuilder'
import { callLLM } from './llm'
import { logger } from '../utils/logger'
import type { GenerateRequest, GenerateResponse } from '../types'

export class GenerationService {

  // ── Main pipeline ──────────────────────────────────────────────────────────
  async generate(userId: number, req: GenerateRequest): Promise<GenerateResponse> {

    // 1. Validate input
    const tool = getTool(req.toolKey)
    if (!tool) throw new ApiError('Tool not found', 'TOOL_NOT_FOUND', 404)

    // 2. Acquire generation lock (race condition protection)
    const locked = await cache.lock.acquire(userId, 'generation', 120)
    if (!locked) throw new ApiError('Generation already in progress', 'GENERATING', 409)

    try {
      // 3. Load user memory (creator profile + voice profile)
      const [creatorProfile, voiceProfile] = await Promise.all([
        prisma.creatorProfile.findUnique({ where: { userId } }),
        prisma.voiceProfile.findUnique({ where: { userId } }),
      ])

      // 4. Load preferred model
      const user = await prisma.user.findUnique({
        where: { id: userId },
        select: { preferredModel: true },
      })
      const modelKey = (user?.preferredModel ?? tool.model) as 'claude' | 'gpt4' | 'grok'

      // 5. Build structured prompt
      const { system, userPrompt } = await buildPrompt({
        toolKey:       req.toolKey,
        input:         req.input,
        creatorProfile: creatorProfile,
        voiceProfile:  voiceProfile,
      })

      // 6. Update session status
      await this.updateSessionStatus(userId, req.toolKey, 'GENERATING')

      // 7. Call LLM
      let content: string
      try {
        content = await callLLM({
          system,
          user:     userPrompt,
          modelKey,
          heavy:    true,
          temperature: 0.85,
        })
      } catch (err) {
        await this.updateSessionStatus(userId, req.toolKey, 'ERROR')
        throw err
      }

      // 8. Save generation to DB
      const generation = await prisma.generation.create({
        data: {
          userId,
          toolKey:     req.toolKey,
          toolName:    tool.name,
          content,
          sourceInput: req.input.topic ?? req.input.description ?? '',
          allowedActions: tool.defaultAllowedActions,
          metadata: {
            model:       modelKey,
            inputLength: userPrompt.length,
          },
        },
      })

      // 9. Track usage event
      prisma.usageEvent.create({
        data: {
          userId,
          event:   'generation.created',
          toolKey: req.toolKey,
          meta:    { generationId: generation.id },
        },
      }).catch(() => {})

      // 10. Build response with allowed actions
      await this.updateSessionStatus(userId, req.toolKey, 'DONE', generation.id)

      return {
        generationId:    generation.id,
        toolKey:         req.toolKey,
        content,
        keyboardVersion: 1,
        allowedActions:  tool.defaultAllowedActions,
        completedActions: [],
        isComplete:      true,
        nextStep:        'done',
      }

    } finally {
      await cache.lock.release(userId, 'generation')
    }
  }

  // ── Regenerate (PHASE 9 safe: uses sourceInput) ────────────────────────────
  async regenerate(userId: number, generationId: number): Promise<GenerateResponse> {
    const original = await prisma.generation.findFirst({
      where: { id: generationId, userId },
    })
    if (!original) throw new ApiError('Generation not found', 'NOT_FOUND', 404)

    const tool = getTool(original.toolKey)
    if (!tool) throw new ApiError('Tool not found', 'TOOL_NOT_FOUND', 404)

    const locked = await cache.lock.acquire(userId, 'generation', 120)
    if (!locked) throw new ApiError('Already generating', 'GENERATING', 409)

    try {
      const [creatorProfile, voiceProfile, user] = await Promise.all([
        prisma.creatorProfile.findUnique({ where: { userId } }),
        prisma.voiceProfile.findUnique({ where: { userId } }),
        prisma.user.findUnique({ where: { id: userId }, select: { preferredModel: true } }),
      ])

      // PHASE 9 FIX: use sourceInput, not content
      const sourceInput = original.sourceInput ?? original.content
      const modelKey = (user?.preferredModel ?? tool.model) as 'claude' | 'gpt4' | 'grok'

      const { system, userPrompt } = await buildPrompt({
        toolKey:       original.toolKey,
        input:         { topic: sourceInput },
        creatorProfile: creatorProfile,
        voiceProfile:  voiceProfile,
        isRegen:       true,
        originalContent: sourceInput,
      })

      const content = await callLLM({
        system,
        user:        userPrompt,
        modelKey,
        heavy:       true,
        temperature: 0.9,
        presencePenalty: 0.4,
      })

      const newGen = await prisma.generation.create({
        data: {
          userId,
          toolKey:      original.toolKey,
          toolName:     original.toolName,
          content,
          sourceInput,
          parentId:     generationId,
          allowedActions: original.allowedActions ?? tool.defaultAllowedActions,
        },
      })

      prisma.usageEvent.create({
        data: {
          userId,
          event:   'generation.regenerated',
          toolKey: original.toolKey,
          meta:    { generationId: newGen.id, parentId: generationId },
        },
      }).catch(() => {})

      return {
        generationId:    newGen.id,
        toolKey:         original.toolKey,
        content,
        keyboardVersion: 1,
        allowedActions:  (original.allowedActions as string[]) ?? tool.defaultAllowedActions,
        completedActions: [],
        isComplete:      true,
        nextStep:        'done',
      }
    } finally {
      await cache.lock.release(userId, 'generation')
    }
  }

  // ── Helpers ────────────────────────────────────────────────────────────────

  private async updateSessionStatus(
    userId: number,
    toolKey: string,
    status: 'GENERATING' | 'DONE' | 'ERROR' | 'IDLE',
    resultId?: number,
  ) {
    await prisma.userSession.upsert({
      where:  { userId },
      update: {
        currentTool:     toolKey,
        generationStatus: status as any,
        activeResultId:  resultId ?? null,
      },
      create: {
        userId,
        currentTool:     toolKey,
        generationStatus: status as any,
        activeResultId:  resultId ?? null,
      },
    })
  }
}

export const generationService = new GenerationService()

// ── Simple error class ─────────────────────────────────────────────────────────
export class ApiError extends Error {
  constructor(
    message: string,
    public code: string,
    public statusCode: number = 400,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}
