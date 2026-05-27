// src/routes/auth.ts
import { FastifyInstance } from 'fastify'
import { z } from 'zod'
import { prisma } from '../db/prisma'
import { verifyTelegramInitData } from '../middleware/auth'
import { getUserState } from '../middleware/auth'
import { logger } from '../utils/logger'

const TelegramAuthSchema = z.object({
  initData: z.string().min(1),
})

const BotAuthSchema = z.object({
  telegramId: z.number(),
  firstName:  z.string().optional(),
  lastName:   z.string().optional(),
  username:   z.string().optional(),
})

export async function authRoutes(app: FastifyInstance): Promise<void> {

  // ── POST /auth/telegram — Mini App auth via initData ─────────────────────
  app.post('/auth/telegram', async (req, reply) => {
    logger.info({ body: req.body }, 'POST /auth/telegram — received')

    let parsedBody: { initData: string }
    try {
      parsedBody = TelegramAuthSchema.parse(req.body)
    } catch (err) {
      logger.warn({ body: req.body, err }, 'POST /auth/telegram — invalid body')
      return reply.code(400).send({
        ok: false,
        error: 'Missing or invalid initData field',
        code: 'VALIDATION_ERROR',
      })
    }

    const { initData } = parsedBody

    const parsed = verifyTelegramInitData(initData)
    if (!parsed) {
      logger.warn({ initData: initData.slice(0, 80) }, 'POST /auth/telegram — invalid initData')
      return reply.code(401).send({
        ok: false,
        error: 'Invalid Telegram initData',
        code: 'INVALID_INIT_DATA',
      })
    }

    let tgUser: Record<string, any>
    try {
      tgUser = JSON.parse(parsed.user)
    } catch {
      logger.error({ parsed }, 'POST /auth/telegram — failed to parse user field')
      return reply.code(400).send({
        ok: false,
        error: 'Malformed user data in initData',
        code: 'MALFORMED_USER',
      })
    }

    logger.info({ telegramId: tgUser.id }, 'POST /auth/telegram — upserting user')

    // Upsert user
    let user: any
    try {
      user = await prisma.user.upsert({
        where: { telegramId: BigInt(tgUser.id) },
        update: {
          firstName:    tgUser.first_name,
          lastName:     tgUser.last_name,
          username:     tgUser.username,
          languageCode: tgUser.language_code,
          lastActiveAt: new Date(),
        },
        create: {
          telegramId:   BigInt(tgUser.id),
          firstName:    tgUser.first_name,
          lastName:     tgUser.last_name,
          username:     tgUser.username,
          languageCode: tgUser.language_code,
        },
        include: {
          creatorProfile: true,
          subscription:   true,
          trial:          true,
        },
      })
    } catch (dbErr: any) {
      logger.error({
        err: dbErr,
        code: dbErr?.code,
        meta: dbErr?.meta,
        telegramId: tgUser.id,
      }, 'POST /auth/telegram — DB upsert failed')
      return reply.code(500).send({
        ok: false,
        error: 'Database error',
        code: 'DB_ERROR',
        detail: dbErr?.message ?? 'unknown',
      })
    }

    const state = await getUserState(user.id)

    const token = await reply.jwtSign({
      userId:     user.id,
      telegramId: Number(user.telegramId),
    })

    logger.info({ userId: user.id, state }, 'POST /auth/telegram — success')

    return reply.send({
      ok: true,
      data: {
        token,
        user: {
          id:          user.id,
          telegramId:  Number(user.telegramId),
          firstName:   user.firstName,
          username:    user.username,
          state,
          isOnboarded: user.isOnboarded,
        },
      },
    })
  })

  // ── POST /auth/bot — Bot backend auth (used by Python bot to get JWT) ────
  // Secured by x-bot-secret header
  app.post('/auth/bot', {
    preHandler: async (req, reply) => {
      if (req.headers['x-bot-secret'] !== process.env.BOT_API_SECRET) {
        return reply.code(403).send({ ok: false, error: 'Forbidden' })
      }
    },
  }, async (req, reply) => {
    const body = BotAuthSchema.parse(req.body)

    const user = await prisma.user.upsert({
      where: { telegramId: BigInt(body.telegramId) },
      update: { lastActiveAt: new Date() },
      create: {
        telegramId: BigInt(body.telegramId),
        firstName:  body.firstName,
        lastName:   body.lastName,
        username:   body.username,
      },
    })

    const token = await reply.jwtSign({
      userId:     user.id,
      telegramId: body.telegramId,
    })

    return reply.send({ ok: true, data: { token, userId: user.id } })
  })

  // ── GET /auth/me — verify token and return current user ──────────────────
  app.get('/auth/me', {
    preHandler: [app.authenticate],
  }, async (req, reply) => {
    const { userId } = req.user as { userId: number }

    const user = await prisma.user.findUnique({
      where: { id: userId },
      include: { creatorProfile: true },
    })

    if (!user) return reply.code(404).send({ ok: false, error: 'User not found' })

    const state = await getUserState(userId)

    return reply.send({
      ok: true,
      data: {
        id:          user.id,
        telegramId:  Number(user.telegramId),
        firstName:   user.firstName,
        username:    user.username,
        state,
        isOnboarded: user.isOnboarded,
        preferredModel: user.preferredModel,
      },
    })
  })
}
