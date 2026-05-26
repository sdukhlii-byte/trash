// src/routes/materials.ts
import { FastifyInstance } from 'fastify'
import { z } from 'zod'
import { requireAuth } from '../middleware/auth'
import { prisma } from '../db/prisma'
import type { AuthTokenPayload } from '../types'

const SaveSchema = z.object({
  generationId: z.number(),
  title:        z.string().optional(),
  tags:         z.array(z.string()).optional(),
})

const UpdateSchema = z.object({
  title:      z.string().optional(),
  tags:       z.array(z.string()).optional(),
  isFavorite: z.boolean().optional(),
})

export async function materialsRoutes(app: FastifyInstance): Promise<void> {

  // ── POST /materials/save ──────────────────────────────────────────────────
  app.post('/materials/save', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId }       = req.user as AuthTokenPayload
    const { generationId, title, tags } = SaveSchema.parse(req.body)

    const gen = await prisma.generation.findFirst({
      where: { id: generationId, userId },
    })
    if (!gen) return reply.code(404).send({ ok: false, error: 'Generation not found' })

    // Idempotent: if already saved — return existing
    const existing = await prisma.material.findFirst({
      where: { generationId, userId },
    })
    if (existing) return reply.send({ ok: true, data: existing })

    const material = await prisma.material.create({
      data: {
        userId,
        generationId,
        toolKey: gen.toolKey,
        title:   title ?? gen.toolName,
        content: gen.content,
        tags:    tags ?? [],
      },
    })

    return reply.code(201).send({ ok: true, data: material })
  })

  // ── GET /materials ────────────────────────────────────────────────────────
  app.get('/materials', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const {
      toolKey,
      page      = '0',
      limit     = '20',
      search,
      favorites,
    } = req.query as any

    const where: any = { userId }
    if (toolKey)          where.toolKey    = toolKey
    if (favorites === 'true') where.isFavorite = true
    if (search) {
      where.OR = [
        { title:   { contains: search, mode: 'insensitive' } },
        { content: { contains: search, mode: 'insensitive' } },
      ]
    }

    const [items, total] = await Promise.all([
      prisma.material.findMany({
        where,
        orderBy: { createdAt: 'desc' },
        skip:    parseInt(page) * parseInt(limit),
        take:    parseInt(limit),
      }),
      prisma.material.count({ where }),
    ])

    return reply.send({
      ok: true,
      data: {
        items,
        total,
        page:    parseInt(page),
        limit:   parseInt(limit),
        hasMore: total > (parseInt(page) + 1) * parseInt(limit),
      },
    })
  })

  // ── GET /materials/:id ────────────────────────────────────────────────────
  app.get('/materials/:id', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const { id }     = req.params as { id: string }

    const material = await prisma.material.findFirst({
      where: { id: parseInt(id), userId },
      include: { generation: { include: { refinements: true } } },
    })

    if (!material) return reply.code(404).send({ ok: false, error: 'Not found' })
    return reply.send({ ok: true, data: material })
  })

  // ── PATCH /materials/:id ──────────────────────────────────────────────────
  app.patch('/materials/:id', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const { id }     = req.params as { id: string }
    const body       = UpdateSchema.parse(req.body)

    const material = await prisma.material.findFirst({
      where: { id: parseInt(id), userId },
    })
    if (!material) return reply.code(404).send({ ok: false, error: 'Not found' })

    const updated = await prisma.material.update({
      where: { id: parseInt(id) },
      data:  body,
    })

    return reply.send({ ok: true, data: updated })
  })

  // ── DELETE /materials/:id ─────────────────────────────────────────────────
  app.delete('/materials/:id', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const { id }     = req.params as { id: string }

    const material = await prisma.material.findFirst({
      where: { id: parseInt(id), userId },
    })
    if (!material) return reply.code(404).send({ ok: false, error: 'Not found' })

    await prisma.material.delete({ where: { id: parseInt(id) } })
    return reply.send({ ok: true, data: { deleted: true } })
  })
}
