// src/server.ts
import 'dotenv/config'
import Fastify from 'fastify'
import fastifyCors from '@fastify/cors'
import fastifyHelmet from '@fastify/helmet'
import fastifyRateLimit from '@fastify/rate-limit'
import fastifyJwt from '@fastify/jwt'

import { prisma } from './db/prisma'
import { redis } from './db/redis'
import { registerRoutes } from './routes'
import { errorHandler } from './middleware/errorHandler'
import { logger } from './utils/logger'

const app = Fastify({
  logger: false,   // используем pino напрямую
  trustProxy: true,
})

async function bootstrap() {
  // ── Security ─────────────────────────────────────────────────────────────
  await app.register(fastifyHelmet, {
    contentSecurityPolicy: false,
  })

  await app.register(fastifyCors, {
    origin: process.env.ALLOWED_ORIGINS?.split(',') ?? ['http://localhost:3000'],
    credentials: true,
  })

  await app.register(fastifyRateLimit, {
    global: true,
    max: 120,
    timeWindow: '1 minute',
    keyGenerator: (req) => {
      // rate-limit по user_id из JWT, иначе по IP
      const token = req.headers.authorization?.split(' ')[1]
      if (token) {
        try {
          const payload = app.jwt.decode(token) as { userId?: number }
          if (payload?.userId) return `user:${payload.userId}`
        } catch {}
      }
      return req.ip
    },
  })

  // ── Auth ──────────────────────────────────────────────────────────────────
  await app.register(fastifyJwt, {
    secret: process.env.JWT_SECRET!,
    sign: { expiresIn: '30d' },
  })

  app.decorate('authenticate', async (request: import('fastify').FastifyRequest, reply: import('fastify').FastifyReply) => {
    try {
      await request.jwtVerify()
    } catch (err) {
      reply.send(err)
    }
  })

  // ── Routes ────────────────────────────────────────────────────────────────
  await registerRoutes(app)

  // ── Error handler ─────────────────────────────────────────────────────────
  app.setErrorHandler(errorHandler)

  // ── Health check ──────────────────────────────────────────────────────────
  app.get('/health', async () => ({
    ok: true,
    version: process.env.npm_package_version ?? '1.0.0',
    ts: new Date().toISOString(),
  }))

  // ── Start ─────────────────────────────────────────────────────────────────
  const port = parseInt(process.env.PORT ?? '4000')
  const host = process.env.HOST ?? '0.0.0.0'

  await app.listen({ port, host })
  logger.info(`🚀 Mira API running on ${host}:${port}`)

  // ── DB connectivity check ─────────────────────────────────────────────────
  await prisma.$connect()
  logger.info('✅ PostgreSQL connected')

  await redis.ping()
  logger.info('✅ Redis connected')
}

// Graceful shutdown
const shutdown = async (signal: string) => {
  logger.info(`${signal} received — shutting down`)
  await app.close()
  await prisma.$disconnect()
  await redis.quit()
  process.exit(0)
}

process.on('SIGTERM', () => shutdown('SIGTERM'))
process.on('SIGINT',  () => shutdown('SIGINT'))

bootstrap().catch((err) => {
  logger.error({ err }, 'Failed to start')
  process.exit(1)
})

export { app }
