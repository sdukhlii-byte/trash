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
  logger: false,
  trustProxy: true,
})

async function bootstrap() {
  // ── Security ─────────────────────────────────────────────────────────────
  await app.register(fastifyHelmet, {
    contentSecurityPolicy: false,
  })

  // CORS: разрешаем Telegram Mini App и любой origin в dev-режиме
  const allowedOrigins = process.env.ALLOWED_ORIGINS?.split(',').map(o => o.trim()) ?? []

  await app.register(fastifyCors, {
    origin: (origin, cb) => {
      // Нет origin (curl, сервер-к-серверу) — разрешаем
      if (!origin) return cb(null, true)
      // Telegram не передаёт origin, но на случай если передаёт
      if (origin.startsWith('https://t.me') || origin.startsWith('https://web.telegram.org')) {
        return cb(null, true)
      }
      // Явно заданные разрешённые origins
      if (allowedOrigins.includes(origin)) return cb(null, true)
      // В dev-режиме разрешаем всё
      if (process.env.NODE_ENV !== 'production') return cb(null, true)

      cb(new Error(`CORS: origin ${origin} not allowed`), false)
    },
    credentials: true,
    methods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Content-Type', 'Authorization', 'x-bot-secret'],
  })

  await app.register(fastifyRateLimit, {
    global: true,
    max: 120,
    timeWindow: '1 minute',
    keyGenerator: (req) => {
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

  // ── Request logging hook ──────────────────────────────────────────────────
  app.addHook('onRequest', async (req) => {
    if (req.url === '/health') return
    logger.info({
      method: req.method,
      url: req.url,
      ip: req.ip,
      ua: req.headers['user-agent']?.slice(0, 80),
    }, `→ ${req.method} ${req.url}`)
  })

  app.addHook('onResponse', async (req, reply) => {
    if (req.url === '/health') return
    logger.info({
      method: req.method,
      url: req.url,
      statusCode: reply.statusCode,
    }, `← ${req.method} ${req.url} ${reply.statusCode}`)
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
