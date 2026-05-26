// src/routes/voice.ts
import { FastifyInstance } from 'fastify'
import { z } from 'zod'
import { requireAuth } from '../middleware/auth'
import { prisma } from '../db/prisma'
import type { AuthTokenPayload } from '../types'

const FeedbackSchema = z.object({
  generationId: z.number(),
  signal:       z.enum(['approved', 'rejected']),
  note:         z.string().optional(),
})

const NoteSchema = z.object({
  note: z.string().min(1).max(500),
})

const SamplesSchema = z.object({
  samples: z.array(z.string().min(10)).max(10),
})

export async function voiceRoutes(app: FastifyInstance): Promise<void> {

  // ── POST /voice/feedback ──────────────────────────────────────────────────
  app.post('/voice/feedback', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const body = FeedbackSchema.parse(req.body)

    const gen = await prisma.generation.findFirst({
      where: { id: body.generationId, userId },
    })
    if (!gen) return reply.code(404).send({ ok: false, error: 'Generation not found' })

    // Save feedback record
    await prisma.voiceFeedback.create({
      data: {
        userId,
        generationId: body.generationId,
        signal:       body.signal.toUpperCase() as any,
        note:         body.note,
      },
    })

    // Update voice profile
    const voiceProfile = await prisma.voiceProfile.findUnique({ where: { userId } })

    if (body.signal === 'approved') {
      const samples = (voiceProfile?.approvedSamples as string[]) ?? []
      const trimmed = [...samples, gen.content].slice(-30)  // keep last 30
      await prisma.voiceProfile.upsert({
        where:  { userId },
        update: {
          approvedSamples: trimmed,
          totalSignals:    { increment: 1 },
          lastLearnedAt:   new Date(),
        },
        create: {
          userId,
          approvedSamples: trimmed,
          totalSignals:    1,
          lastLearnedAt:   new Date(),
        },
      })
    } else {
      // Rejected — extract pattern from note if provided
      if (body.note) {
        const rejections = (voiceProfile?.rejectionPatterns as string[]) ?? []
        await prisma.voiceProfile.upsert({
          where:  { userId },
          update: { rejectionPatterns: [...rejections, body.note].slice(-20) },
          create: { userId, rejectionPatterns: [body.note] },
        })
      }
    }

    return reply.send({ ok: true, data: { recorded: true } })
  })

  // ── POST /voice/note ──────────────────────────────────────────────────────
  app.post('/voice/note', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const { note }   = NoteSchema.parse(req.body)

    const voiceProfile = await prisma.voiceProfile.findUnique({ where: { userId } })
    const notes        = (voiceProfile?.styleNotes as string[]) ?? []

    await prisma.voiceProfile.upsert({
      where:  { userId },
      update: { styleNotes: [...notes, note].slice(-20) },
      create: { userId, styleNotes: [note] },
    })

    return reply.send({ ok: true, data: { saved: true } })
  })

  // ── POST /voice/samples — bulk upload style examples ─────────────────────
  app.post('/voice/samples', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId }  = req.user as AuthTokenPayload
    const { samples } = SamplesSchema.parse(req.body)

    const voiceProfile = await prisma.voiceProfile.findUnique({ where: { userId } })
    const existing     = (voiceProfile?.approvedSamples as string[]) ?? []
    const merged       = [...existing, ...samples].slice(-30)

    await prisma.voiceProfile.upsert({
      where:  { userId },
      update: {
        approvedSamples: merged,
        totalSignals:    { increment: samples.length },
        lastLearnedAt:   new Date(),
      },
      create: {
        userId,
        approvedSamples: merged,
        totalSignals:    samples.length,
        lastLearnedAt:   new Date(),
      },
    })

    return reply.send({ ok: true, data: { added: samples.length } })
  })

  // ── GET /voice/profile ────────────────────────────────────────────────────
  app.get('/voice/profile', { preHandler: [requireAuth] }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload

    const vp = await prisma.voiceProfile.findUnique({ where: { userId } })

    return reply.send({
      ok: true,
      data: {
        totalSignals:      vp?.totalSignals ?? 0,
        approvedCount:     (vp?.approvedSamples as string[])?.length ?? 0,
        rejectionPatterns: (vp?.rejectionPatterns as string[]) ?? [],
        styleNotes:        (vp?.styleNotes as string[]) ?? [],
        hasMetaProfile:    !!vp?.metaProfile,
        lastLearnedAt:     vp?.lastLearnedAt?.toISOString() ?? null,
      },
    })
  })
}
