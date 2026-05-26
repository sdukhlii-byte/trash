// src/routes/tools.ts
import { FastifyInstance } from 'fastify'
import { optionalAuth } from '../middleware/auth'
import { getToolList, getTool } from '../tools/registry'

export async function toolRoutes(app: FastifyInstance): Promise<void> {

  // ── GET /tools ────────────────────────────────────────────────────────────
  app.get('/tools', { preHandler: [optionalAuth] }, async (_req, reply) => {
    return reply.send({ ok: true, data: getToolList() })
  })

  // ── GET /tools/:key ───────────────────────────────────────────────────────
  app.get('/tools/:key', async (req, reply) => {
    const { key } = req.params as { key: string }
    const tool = getTool(key)
    if (!tool) return reply.code(404).send({ ok: false, error: 'Tool not found' })
    return reply.send({ ok: true, data: tool })
  })
}
