// src/routes/auth.ts
import { FastifyInstance } from 'fastify'
import { z } from 'zod'
import { prisma } from '../db/prisma'
import { verifyTelegramInitData } from '../middleware/auth'
import { getUserState } from '../middleware/auth'

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
    const { initData } = TelegramAuthSchema.parse(req.body)

    const parsed = verifyTelegramInitData(initData)
    if (!parsed) {
      return reply.code(401).send({
        ok: false,
        error: 'Invalid Telegram initData',
        code: 'INVALID_INIT_DATA',
      })
    }

    const tgUser = JSON.parse(parsed.user)

    // Upsert user
    const user = await prisma.user.upsert({
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

    const state = await getUserState(user.id)

    const token = await reply.jwtSign({
      userId:     user.id,
      telegramId: Number(user.telegramId),
    })

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
