// src/routes/subscription.ts
import { FastifyInstance } from 'fastify'
import { z } from 'zod'
import { requireAuth } from '../middleware/auth'
import { invalidateUserState } from '../middleware/auth'
import { prisma } from '../db/prisma'
import type { AuthTokenPayload } from '../types'

const TIER_DAYS: Record<string, number> = { '1m': 30, '3m': 90, '6m': 180, '12m': 365 }
const TRIAL_DAYS = 7

const ActivateSchema = z.object({
  tier:       z.enum(['1m', '3m', '6m', '12m']),
  paymentId:  z.string(),
  amountEur:  z.number().positive(),
})

export async function subscriptionRoutes(app: FastifyInstance): Promise<void> {

  // ── GET /subscription ─────────────────────────────────────────────────────
  app.get('/subscription', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload

    const [sub, trial] = await Promise.all([
      prisma.subscription.findUnique({ where: { userId } }),
      prisma.trial.findUnique({ where: { userId } }),
    ])

    const now = new Date()

    return reply.send({
      ok: true,
      data: {
        subscription: sub ? {
          tier:      sub.tier,
          expiresAt: sub.expiresAt.toISOString(),
          daysLeft:  Math.max(0, Math.ceil((sub.expiresAt.getTime() - now.getTime()) / 86400000)),
          isActive:  sub.expiresAt > now,
        } : null,
        trial: trial ? {
          expiresAt: trial.expiresAt.toISOString(),
          daysLeft:  Math.max(0, Math.ceil((trial.expiresAt.getTime() - now.getTime()) / 86400000)),
          isActive:  trial.expiresAt > now,
        } : null,
        hasUsedTrial: !!trial,
      },
    })
  })

  // ── POST /subscription/activate — called by payment webhook ──────────────
  app.post('/subscription/activate', {
    preHandler: async (req, reply) => {
      if (req.headers['x-bot-secret'] !== process.env.BOT_API_SECRET) {
        return reply.code(403).send({ ok: false, error: 'Forbidden' })
      }
    },
  }, async (req, reply) => {
    const body = ActivateSchema.extend({ userId: z.number() }).parse(req.body)
    const days = TIER_DAYS[body.tier]
    const now  = new Date()

    // Extend existing or create new
    const existing = await prisma.subscription.findUnique({ where: { userId: body.userId } })
    const startsAt = existing && existing.expiresAt > now ? existing.expiresAt : now
    const expiresAt = new Date(startsAt.getTime() + days * 86400000)

    await prisma.subscription.upsert({
      where:  { userId: body.userId },
      update: { tier: body.tier, expiresAt, paymentId: body.paymentId, amountEur: body.amountEur },
      create: {
        userId:    body.userId,
        tier:      body.tier,
        startsAt,
        expiresAt,
        paymentId: body.paymentId,
        amountEur: body.amountEur,
      },
    })

    await invalidateUserState(body.userId)

    // Pay out referral bonus if first payment
    try {
      const referral = await prisma.referral.findUnique({
        where: { receiverId: body.userId },
      })
      if (referral && !referral.paidOut) {
        // Add bonus days to referrer
        const giverSub = await prisma.subscription.findUnique({ where: { userId: referral.giverId } })
        if (giverSub) {
          const bonusExp = new Date(giverSub.expiresAt.getTime() + referral.bonusDays * 86400000)
          await prisma.subscription.update({
            where: { userId: referral.giverId },
            data:  { expiresAt: bonusExp },
          })
          await invalidateUserState(referral.giverId)
        }
        await prisma.referral.update({ where: { id: referral.id }, data: { paidOut: true } })
      }
    } catch {}

    return reply.send({ ok: true, data: { activated: true } })
  })

  // ── POST /subscription/trial ──────────────────────────────────────────────
  app.post('/subscription/trial', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload

    const existing = await prisma.trial.findUnique({ where: { userId } })
    if (existing) {
      return reply.code(409).send({ ok: false, error: 'Trial already used', code: 'TRIAL_USED' })
    }

    const now = new Date()
    await prisma.trial.create({
      data: {
        userId,
        startsAt:  now,
        expiresAt: new Date(now.getTime() + TRIAL_DAYS * 86400000),
      },
    })

    await invalidateUserState(userId)
    return reply.send({ ok: true, data: { trialDays: TRIAL_DAYS } })
  })

  // ── GET /subscription/payment-link ───────────────────────────────────────
  app.get('/subscription/payment-link', { preHandler: [requireAuth] }, async (_req, reply) => {
    const link = process.env.LAVA_LINK ?? ''
    if (!link) return reply.code(503).send({ ok: false, error: 'Payment not configured' })
    return reply.send({ ok: true, data: { url: link } })
  })
}
