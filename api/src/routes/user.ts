// src/routes/user.ts
import { FastifyInstance } from 'fastify'
import { z } from 'zod'
import { prisma } from '../db/prisma'
import { requireAuth, getUserState, invalidateUserState } from '../middleware/auth'
import type { AuthTokenPayload } from '../types'

const OnboardingSchema = z.object({
  niche:    z.string().min(1).max(300),
  audience: z.string().min(1).max(300),
  tone:     z.string().min(1).max(100),
  goals:    z.string().optional(),
  platform: z.string().optional(),
})

const UpdateProfileSchema = OnboardingSchema.partial()

const ModelSchema = z.object({
  model: z.enum(['claude', 'gpt4', 'grok']),
})

export async function userRoutes(app: FastifyInstance): Promise<void> {

  // ── GET /user/profile ─────────────────────────────────────────────────────
  app.get('/user/profile', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload

    const [user, voiceProfile, stats] = await Promise.all([
      prisma.user.findUnique({
        where: { id: userId },
        include: {
          creatorProfile: true,
          subscription:   true,
          trial:          true,
        },
      }),
      prisma.voiceProfile.findUnique({ where: { userId } }),
      getUserStats(userId),
    ])

    if (!user) return reply.code(404).send({ ok: false, error: 'Not found' })

    const state = await getUserState(userId)
    const totalSignals = voiceProfile?.totalSignals ?? 0

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
        creatorProfile: user.creatorProfile,
        subscription: user.subscription ? {
          tier:      user.subscription.tier,
          expiresAt: user.subscription.expiresAt.toISOString(),
          daysLeft:  Math.max(0, Math.ceil(
            (user.subscription.expiresAt.getTime() - Date.now()) / 86400000
          )),
          isActive: user.subscription.expiresAt > new Date(),
        } : null,
        voiceProgress: buildVoiceProgress(totalSignals),
        stats,
      },
    })
  })

  // ── POST /user/onboarding ─────────────────────────────────────────────────
  app.post('/user/onboarding', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const body = OnboardingSchema.parse(req.body)

    await prisma.$transaction([
      prisma.creatorProfile.upsert({
        where:  { userId },
        update: body,
        create: { userId, ...body },
      }),
      prisma.user.update({
        where: { id: userId },
        data:  { isOnboarded: true },
      }),
    ])

    await invalidateUserState(userId)

    return reply.send({ ok: true, data: { message: 'Profile saved' } })
  })

  // ── PATCH /user/profile ───────────────────────────────────────────────────
  app.patch('/user/profile', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const body = UpdateProfileSchema.parse(req.body)

    await prisma.creatorProfile.upsert({
      where:  { userId },
      update: body,
      create: { userId, ...body },
    })

    return reply.send({ ok: true, data: { message: 'Updated' } })
  })

  // ── PATCH /user/model ─────────────────────────────────────────────────────
  app.patch('/user/model', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const { model } = ModelSchema.parse(req.body)

    await prisma.user.update({
      where: { id: userId },
      data:  { preferredModel: model },
    })

    return reply.send({ ok: true, data: { model } })
  })

  // ── GET /user/progress ────────────────────────────────────────────────────
  app.get('/user/progress', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload

    const [voiceProfile, stats] = await Promise.all([
      prisma.voiceProfile.findUnique({ where: { userId } }),
      getUserStats(userId),
    ])

    return reply.send({
      ok: true,
      data: {
        voiceProgress: buildVoiceProgress(voiceProfile?.totalSignals ?? 0),
        stats,
      },
    })
  })
}

// ── Helpers ───────────────────────────────────────────────────────────────────

async function getUserStats(userId: number) {
  const [total, byTool] = await Promise.all([
    prisma.generation.count({ where: { userId } }),
    prisma.generation.groupBy({
      by: ['toolKey', 'toolName'],
      where: { userId },
      _count: { id: true },
      orderBy: { _count: { id: 'desc' } },
    }),
  ])

  return {
    totalGenerations: total,
    byTool: Object.fromEntries(
      byTool.map((r) => [r.toolKey, r._count.id])
    ),
    streakDays: 0,  // TODO: implement streak
  }
}

function buildVoiceProgress(totalSignals: number) {
  const levels = [
    { min: 0,  label: 'Начало пути',    emoji: '🌱' },
    { min: 3,  label: 'Учусь слышать',  emoji: '👂' },
    { min: 7,  label: 'Нахожу голос',   emoji: '🎙' },
    { min: 15, label: 'Звучу как я',    emoji: '✨' },
    { min: 25, label: 'Мой голос',      emoji: '🎯' },
    { min: 40, label: 'Экспертный голос', emoji: '🏆' },
  ]

  const thresholds = [0, 3, 7, 15, 25, 40]
  let level = 0
  for (let i = 0; i < thresholds.length; i++) {
    if (totalSignals >= thresholds[i]) level = i
  }

  const currentMin = thresholds[level]
  const nextMin    = thresholds[level + 1] ?? thresholds[level] + 10
  const percentage = Math.min(100, Math.round(
    ((totalSignals - currentMin) / (nextMin - currentMin)) * 100
  ))

  return {
    totalSignals,
    level,
    percentage,
    label:     levels[level].label,
    emoji:     levels[level].emoji,
    nextLevel: level < levels.length - 1 ? levels[level + 1].label : null,
  }
}
