// src/middleware/auth.ts
import { FastifyRequest, FastifyReply } from 'fastify'
import crypto from 'crypto'
import { prisma } from '../db/prisma'
import { cache } from '../db/redis'
import { logger } from '../utils/logger'
import type { AuthTokenPayload } from '../types'

// ── Telegram initData verification ───────────────────────────────────────────

export function verifyTelegramInitData(initData: string): Record<string, string> | null {
  const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN

  if (!BOT_TOKEN) {
    logger.error('TELEGRAM_BOT_TOKEN is not set! Check environment variables on Render.')
    return null
  }

  try {
    const params = new URLSearchParams(initData)
    const hash = params.get('hash')
    if (!hash) {
      logger.warn('verifyTelegramInitData: no hash in initData')
      return null
    }

    params.delete('hash')

    const checkString = Array.from(params.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => `${k}=${v}`)
      .join('\n')

    const secretKey = crypto
      .createHmac('sha256', 'WebAppData')
      .update(BOT_TOKEN)
      .digest()

    const expectedHash = crypto
      .createHmac('sha256', secretKey)
      .update(checkString)
      .digest('hex')

    if (expectedHash !== hash) {
      logger.warn(
        { expected: expectedHash, got: hash },
        'verifyTelegramInitData: hash mismatch — BOT_TOKEN on Render likely wrong'
      )
      return null
    }

    // Skip auth_date check in non-production (helpful for testing)
    const authDate = parseInt(params.get('auth_date') ?? '0')
    const ageSeconds = Date.now() / 1000 - authDate
    if (process.env.NODE_ENV === 'production' && ageSeconds > 86400) {
      logger.warn({ ageSeconds }, 'verifyTelegramInitData: initData expired (>24h)')
      return null
    }

    const result: Record<string, string> = {}
    params.forEach((v, k) => { result[k] = v })
    return result

  } catch (err) {
    logger.error({ err }, 'verifyTelegramInitData: unexpected error')
    return null
  }
}

// ── JWT auth middleware ───────────────────────────────────────────────────────

export async function requireAuth(
  req: FastifyRequest,
  reply: FastifyReply
): Promise<void> {
  try {
    await req.jwtVerify()
    const payload = req.user as AuthTokenPayload

    prisma.user.update({
      where: { id: payload.userId },
      data: { lastActiveAt: new Date() },
    }).catch(() => {})

  } catch (err) {
    reply.code(401).send({ ok: false, error: 'Unauthorized', code: 'AUTH_REQUIRED' })
  }
}

// ── Optional auth ─────────────────────────────────────────────────────────────

export async function optionalAuth(
  req: FastifyRequest,
  _reply: FastifyReply
): Promise<void> {
  try {
    await req.jwtVerify()
  } catch {}
}

// ── Bot-to-API secret auth ────────────────────────────────────────────────────

export async function requireBotSecret(
  req: FastifyRequest,
  reply: FastifyReply
): Promise<void> {
  const secret = req.headers['x-bot-secret']
  if (secret !== process.env.BOT_API_SECRET) {
    reply.code(403).send({ ok: false, error: 'Forbidden', code: 'INVALID_SECRET' })
  }
}

// ── Subscription check ────────────────────────────────────────────────────────

export async function requireAccess(
  req: FastifyRequest,
  reply: FastifyReply
): Promise<void> {
  const payload = req.user as AuthTokenPayload
  const state = await getUserState(payload.userId)

  if (state !== 'trial' && state !== 'subscribed') {
    reply.code(402).send({
      ok: false,
      error: 'Subscription required',
      code: 'PAYMENT_REQUIRED',
      data: { state },
    })
  }
}

// ── User state helper (cached) ────────────────────────────────────────────────

export async function getUserState(userId: number): Promise<string> {
  const cached = await cache.userState.get(userId)
  if (cached) return cached

  const state = await computeUserState(userId)
  await cache.userState.set(userId, state)
  return state
}

async function computeUserState(userId: number): Promise<string> {
  const user = await prisma.user.findUnique({
    where: { id: userId },
    include: { subscription: true, trial: true },
  })

  if (!user) return 'new'
  if (!user.isOnboarded) return 'new'

  const now = new Date()

  if (user.subscription && user.subscription.expiresAt > now) return 'subscribed'
  if (user.trial && user.trial.expiresAt > now) return 'trial'
  if (user.subscription || user.trial) return 'expired'
  return 'onboarded'
}

export async function invalidateUserState(userId: number): Promise<void> {
  await cache.userState.del(userId)
}
