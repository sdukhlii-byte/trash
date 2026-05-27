// src/services/refinement.ts
// Refinement engine — каждая правка: валидация → prompt → LLM → новый generation

import { prisma } from '../db/prisma'
import { cache } from '../db/redis'
import { getTool } from '../tools/registry'
import { buildRefinePrompt } from './promptBuilder'
import { callLLM } from './llm'
import { ApiError } from './generation'
import type { RefineRequest, RefineResponse } from '../types'

// ── ACTION_REGISTRY — зеркало Python callback_context.py ─────────────────────

const ACTION_REGISTRY: Record<string, {
  repeatable: boolean
  marksCompleted: boolean
  resetsCompleted?: boolean
  allowedTools: string[] | null
}> = {
  'rs:softer':    { repeatable: true,  marksCompleted: false, allowedTools: ['reels_short'] },
  'rs:bolder':    { repeatable: true,  marksCompleted: false, allowedTools: ['reels_short'] },
  'rs:top5':      { repeatable: false, marksCompleted: true,  allowedTools: ['reels_short'] },
  'rs:style':     { repeatable: true,  marksCompleted: false, allowedTools: ['reels_short'] },
  'rs:regen':     { repeatable: true,  marksCompleted: false, resetsCompleted: true, allowedTools: ['reels_short'] },
  'rs:pick_desc': { repeatable: false, marksCompleted: true,  allowedTools: ['reels_short'] },
  'car:headline': { repeatable: true,  marksCompleted: false, allowedTools: ['carousel'] },
  'car:softer':   { repeatable: true,  marksCompleted: false, allowedTools: ['carousel'] },
  'car:bolder':   { repeatable: true,  marksCompleted: false, allowedTools: ['carousel'] },
  'car:shorten':  { repeatable: false, marksCompleted: true,  allowedTools: ['carousel'] },
  'car:add_slide':{ repeatable: false, marksCompleted: true,  allowedTools: ['carousel'] },
  'car:trigger':  { repeatable: true,  marksCompleted: false, allowedTools: ['carousel'] },
  'car:format':   { repeatable: true,  marksCompleted: false, allowedTools: ['carousel'] },
  'ag:softer':    { repeatable: true,  marksCompleted: false, allowedTools: null },
  'ag:bolder':    { repeatable: true,  marksCompleted: false, allowedTools: null },
  'ag:shorter':   { repeatable: true,  marksCompleted: false, allowedTools: null },
  'ag:detail':    { repeatable: true,  marksCompleted: false, allowedTools: null },
  'ag:cta':       { repeatable: true,  marksCompleted: false, allowedTools: null },
  'ag:resonance': { repeatable: true,  marksCompleted: false, allowedTools: ['warmup'] },
  'ag:add_proof': { repeatable: true,  marksCompleted: false, allowedTools: ['warmup'] },
  'ag:mid_hook':  { repeatable: true,  marksCompleted: false, allowedTools: ['stories'] },
  'ag:hook':      { repeatable: true,  marksCompleted: false, allowedTools: ['talking_head','cartoon','post'] },
  'ag:deepen':    { repeatable: true,  marksCompleted: false, allowedTools: ['profile'] },
  'ag:tactics':   { repeatable: true,  marksCompleted: false, allowedTools: ['competitor'] },
  'ag:positioning':{ repeatable: true, marksCompleted: false, allowedTools: ['competitor'] },
  'ag:save_moment':{ repeatable: true, marksCompleted: false, allowedTools: ['carousel'] },
  'regen':        { repeatable: true,  marksCompleted: false, allowedTools: null },
  'refine':       { repeatable: true,  marksCompleted: false, allowedTools: null },
}

export class RefinementService {

  async refine(userId: number, req: RefineRequest): Promise<RefineResponse> {

    // 1. Load generation — verify ownership
    const gen = await prisma.generation.findFirst({
      where: { id: req.generationId, userId },
    })
    if (!gen) throw new ApiError('Generation not found', 'NOT_FOUND', 404)

    // 2. Validate action against ACTION_REGISTRY
    const spec = ACTION_REGISTRY[req.action]
    if (!spec) throw new ApiError(`Unknown action: ${req.action}`, 'UNKNOWN_ACTION', 400)

    // 3. Check tool compatibility
    if (spec.allowedTools && !spec.allowedTools.includes(gen.toolKey)) {
      throw new ApiError(
        `Action ${req.action} not allowed for tool ${gen.toolKey}`,
        'ACTION_NOT_ALLOWED',
        400,
      )
    }

    // 4. Check if already completed (one-time actions)
    const completedActions = (gen.completedActions as string[]) ?? []
    if (spec.marksCompleted && !spec.repeatable && completedActions.includes(req.action)) {
      throw new ApiError('This action was already completed', 'ALREADY_COMPLETED', 409)
    }

    // 5. Acquire refinement lock
    const locked = await cache.lock.acquire(userId, `refine:${gen.id}`, 60)
    if (!locked) throw new ApiError('Refinement in progress', 'GENERATING', 409)

    try {
      // 6. Load context
      const [creatorProfile, voiceProfile, user] = await Promise.all([
        prisma.creatorProfile.findUnique({ where: { userId } }),
        prisma.voiceProfile.findUnique({ where: { userId } }),
        prisma.user.findUnique({ where: { id: userId }, select: { preferredModel: true } }),
      ])

      const tool = getTool(gen.toolKey)
      const modelKey = (user?.preferredModel ?? tool?.model ?? 'claude') as 'claude' | 'gpt4' | 'grok'

      // 7. Build refine prompt
      const { system, instruction } = await buildRefinePrompt({
        action:        req.action,
        toolKey:       gen.toolKey,
        currentContent: gen.content,
        metadata:      req.metadata,
        creatorProfile: creatorProfile as any,
        voiceProfile:  voiceProfile as any,
      })

      // 8. Call LLM
      const newContent = await callLLM({
        system,
        user:        instruction,
        modelKey,
        heavy:       true,
        temperature: 0.8,
      })

      // 9. Save refinement record
      await prisma.refinement.create({
        data: {
          generationId:  gen.id,
          userId,
          action:        req.action,
          instruction,
          inputContent:  gen.content,
          outputContent: newContent,
          toolKey:       gen.toolKey,
        },
      })

      // 10. Create new generation in chain
      const allowedActions = spec.resetsCompleted
        ? (gen.allowedActions as string[] ?? tool?.defaultAllowedActions ?? [])
        : (gen.allowedActions as string[] ?? tool?.defaultAllowedActions ?? [])

      const newCompletedActions = spec.resetsCompleted
        ? []
        : spec.marksCompleted
          ? [...completedActions, req.action]
          : completedActions

      const newGen = await prisma.generation.create({
        data: {
          userId,
          toolKey:          gen.toolKey,
          toolName:         gen.toolName,
          content:          newContent,
          sourceInput:      gen.sourceInput,
          parentId:         gen.id,
          allowedActions,
          completedActions: newCompletedActions,
          metadata:         { refinedFrom: gen.id, action: req.action },
        },
      })

      // 11. Track
      prisma.usageEvent.create({
        data: {
          userId,
          event:   'generation.refined',
          toolKey: gen.toolKey,
          meta:    { generationId: newGen.id, action: req.action },
        },
      }).catch(() => {})

      return {
        generationId:    newGen.id,
        parentId:        gen.id,
        content:         newContent,
        action:          req.action,
        keyboardVersion: newGen.keyboardVersion,
        allowedActions,
        completedActions: newCompletedActions,
      }

    } finally {
      await cache.lock.release(userId, `refine:${gen.id}`)
    }
  }
}

export const refinementService = new RefinementService()
