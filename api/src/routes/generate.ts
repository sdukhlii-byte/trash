// src/routes/generate.ts
import { FastifyInstance } from 'fastify'
import { z } from 'zod'
import { requireAuth, requireAccess } from '../middleware/auth'
import { generationService } from '../services/generation'
import { prisma } from '../db/prisma'
import type { AuthTokenPayload } from '../types'

const GenerateSchema = z.object({
  toolKey: z.string(),
  input: z.object({
    topic:       z.string().optional(),
    description: z.string().optional(),
    photos:      z.array(z.string()).optional(),
    interviewAnswers: z.array(z.object({
      question: z.string(),
      answer:   z.string(),
    })).optional(),
    pickedVariant: z.number().optional(),
  }),
})

export async function generateRoutes(app: FastifyInstance): Promise<void> {

  // ── POST /generate ────────────────────────────────────────────────────────
  app.post('/generate', {
    preHandler: [requireAuth, requireAccess],
  }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const body = GenerateSchema.parse(req.body)

    const result = await generationService.generate(userId, body)
    return reply.send({ ok: true, data: result })
  })

  // ── POST /generate/:id/regen ──────────────────────────────────────────────
  app.post('/generate/:id/regen', {
    preHandler: [requireAuth, requireAccess],
  }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const { id }     = req.params as { id: string }

    const result = await generationService.regenerate(userId, parseInt(id))
    return reply.send({ ok: true, data: result })
  })

  // ── GET /generate/:id ─────────────────────────────────────────────────────
  app.get('/generate/:id', {
    preHandler: [requireAuth],
  }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const { id }     = req.params as { id: string }

    const gen = await prisma.generation.findFirst({
      where: { id: parseInt(id), userId },
      include: { refinements: { orderBy: { createdAt: 'asc' } } },
    })

    if (!gen) return reply.code(404).send({ ok: false, error: 'Not found' })

    return reply.send({
      ok: true,
      data: {
        ...gen,
        completedActions: gen.completedActions as string[],
        allowedActions:   gen.allowedActions   as string[],
      },
    })
  })

  // ── GET /generate — list user's generations ───────────────────────────────
  app.get('/generate', {
    preHandler: [requireAuth],
  }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const { page = '0', limit = '20', toolKey } = req.query as any

    const where = { userId, ...(toolKey ? { toolKey } : {}) }
    const [items, total] = await Promise.all([
      prisma.generation.findMany({
        where,
        orderBy: { createdAt: 'desc' },
        skip:  parseInt(page) * parseInt(limit),
        take:  parseInt(limit),
        select: {
          id: true, toolKey: true, toolName: true,
          content: true, createdAt: true,
          completedActions: true, keyboardVersion: true,
        },
      }),
      prisma.generation.count({ where }),
    ])

    return reply.send({
      ok: true,
      data: { items, total, page: parseInt(page), limit: parseInt(limit), hasMore: total > (parseInt(page) + 1) * parseInt(limit) },
    })
  })
}
