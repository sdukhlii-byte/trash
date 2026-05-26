// src/routes/webhook.ts
// Lava payment webhook — зеркало lava_payments.py логики

import { FastifyInstance } from 'fastify'
import crypto from 'crypto'
import { prisma } from '../db/prisma'
import { invalidateUserState } from '../middleware/auth'
import { logger } from '../utils/logger'

const TIER_THRESHOLDS: [number, string][] = [
  [240, '6m'],
  [120, '3m'],
  [32,  '1m'],
]
const TIER_DAYS: Record<string, number> = { '1m': 30, '3m': 90, '6m': 180, '12m': 365 }

function detectTier(amountEur: number): string {
  for (const [threshold, tier] of TIER_THRESHOLDS) {
    if (amountEur >= threshold) return tier
  }
  return '1m'
}

export async function webhookRoutes(app: FastifyInstance): Promise<void> {

  // ── POST /webhook/lava ────────────────────────────────────────────────────
  app.post('/webhook/lava', async (req, reply) => {
    try {
      const body = req.body as any

      // Verify Lava signature
      const secret   = process.env.LAVA_SECRET_KEY ?? ''
      const received = body.signature ?? ''
      const payload  = JSON.stringify({ ...body, signature: undefined })
      const expected = crypto.createHmac('sha256', secret).update(payload).digest('hex')

      if (secret && received !== expected) {
        logger.warn('[webhook/lava] invalid signature')
        return reply.code(400).send({ ok: false, error: 'Invalid signature' })
      }

      const status = body.status
      if (status !== 'success') {
        return reply.send({ ok: true, data: { ignored: true, status } })
      }

      const orderId   = body.order_id as string   // format: "uid_{telegramId}"
      const amountEur = parseFloat(body.amount ?? '0')

      if (!orderId?.startsWith('uid_')) {
        logger.warn('[webhook/lava] unknown order format:', orderId)
        return reply.send({ ok: true })
      }

      const telegramId = parseInt(orderId.replace('uid_', ''))
      const user       = await prisma.user.findUnique({
        where: { telegramId: BigInt(telegramId) },
      })

      if (!user) {
        logger.error(`[webhook/lava] user not found for telegramId=${telegramId}`)
        return reply.send({ ok: true })
      }

      const tier   = detectTier(amountEur)
      const days   = TIER_DAYS[tier]
      const now    = new Date()
      const existing = await prisma.subscription.findUnique({ where: { userId: user.id } })
      const startsAt  = existing && existing.expiresAt > now ? existing.expiresAt : now
      const expiresAt = new Date(startsAt.getTime() + days * 86400000)

      await prisma.subscription.upsert({
        where:  { userId: user.id },
        update: { tier, expiresAt, paymentId: body.payment_id, amountEur },
        create: {
          userId: user.id,
          tier,
          startsAt,
          expiresAt,
          paymentId: body.payment_id,
          amountEur,
        },
      })

      await invalidateUserState(user.id)

      logger.info(`[webhook/lava] activated tier=${tier} for user=${user.id} (tg=${telegramId})`)

      return reply.send({ ok: true })

    } catch (err) {
      logger.error('[webhook/lava] error:', err)
      return reply.code(500).send({ ok: false, error: 'Internal error' })
    }
  })
}
