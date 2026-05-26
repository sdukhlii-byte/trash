// src/routes/refine.ts
import { FastifyInstance } from 'fastify'
import { z } from 'zod'
import { requireAuth, requireAccess } from '../middleware/auth'
import { refinementService } from '../services/refinement'
import type { AuthTokenPayload } from '../types'

const RefineSchema = z.object({
  generationId: z.number(),
  action:       z.string(),
  metadata:     z.record(z.string()).optional(),
})

export async function refineRoutes(app: FastifyInstance): Promise<void> {

  // ── POST /refine ──────────────────────────────────────────────────────────
  app.post('/refine', {
    preHandler: [requireAuth, requireAccess],
  }, async (req, reply) => {
    const { userId } = req.user as AuthTokenPayload
    const body = RefineSchema.parse(req.body)

    const result = await refinementService.refine(userId, body)
    return reply.send({ ok: true, data: result })
  })
}
