// src/routes/admin.ts
import { FastifyInstance } from 'fastify'
import { z } from 'zod'
import { prisma } from '../db/prisma'
import { invalidateUserState } from '../middleware/auth'

const requireAdmin = async (req: any, reply: any) => {
  if (req.headers['x-admin-secret'] !== process.env.ADMIN_SECRET) {
    return reply.code(403).send({ ok: false, error: 'Forbidden' })
  }
}

export async function adminRoutes(app: FastifyInstance): Promise<void> {

  app.addHook('preHandler', requireAdmin)

  // ── GET /api/admin/stats ──────────────────────────────────────────────────
  app.get('/stats', async (_req, reply) => {
    const [users, generations, materials, subs, trials] = await Promise.all([
      prisma.user.count(),
      prisma.generation.count(),
      prisma.material.count(),
      prisma.subscription.count({ where: { expiresAt: { gt: new Date() } } }),
      prisma.trial.count({ where: { expiresAt: { gt: new Date() } } }),
    ])

    return reply.send({
      ok: true,
      data: {
        users,
        generations,
        materials,
        activeSubscriptions: subs,
        activeTrials:        trials,
        totalAccess:         subs + trials,
      },
    })
  })

  // ── POST /api/admin/grant-access ──────────────────────────────────────────
  app.post('/grant-access', async (req, reply) => {
    const { telegramId, days } = z.object({
      telegramId: z.number(),
      days:       z.number().int().positive().max(365),
    }).parse(req.body)

    const user = await prisma.user.findUnique({
      where: { telegramId: BigInt(telegramId) },
    })
    if (!user) return reply.code(404).send({ ok: false, error: 'User not found' })

    const now       = new Date()
    const existing  = await prisma.subscription.findUnique({ where: { userId: user.id } })
    const startsAt  = existing && existing.expiresAt > now ? existing.expiresAt : now
    const expiresAt = new Date(startsAt.getTime() + days * 86400000)

    await prisma.subscription.upsert({
      where:  { userId: user.id },
      update: { expiresAt },
      create: { userId: user.id, tier: 'admin', startsAt, expiresAt },
    })

    await invalidateUserState(user.id)

    return reply.send({ ok: true, data: { userId: user.id, expiresAt } })
  })
}
