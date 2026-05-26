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
      const token = req.headers.authorization?.split(' ')[1]

      if (token) {
        try {
          const payload = app.jwt.decode(token) as { userId?: number } | null
          if (payload?.userId) return `user:${payload.userId}`
        } catch {
          return req.ip
        }
      }

      return req.ip
    },
  })

  await app.register(fastifyJwt, {
    secret: process.env.JWT_SECRET!,
    sign: { expiresIn: '30d' },
  })

  await registerRoutes(app)

  app.setErrorHandler(errorHandler)

  app.get('/health', async () => ({
    ok: true,
    version: process.env.npm_package_version ?? '1.0.0',
    ts: new Date().toISOString(),
  }))

  const port = Number(process.env.PORT ?? 4000)
  const host = process.env.HOST ?? '0.0.0.0'

  await app.listen({ port, host })
  logger.info(`🚀 Mira API running on ${host}:${port}`)

  await prisma.$connect()
  logger.info('✅ PostgreSQL connected')

  await redis.ping()
  logger.info('✅ Redis connected')
}

const shutdown = async (signal: string) => {
  logger.info(`${signal} received — shutting down`)

  await app.close()
  await prisma.$disconnect()
  await redis.quit()

  process.exit(0)
}

process.on('SIGTERM', () => shutdown('SIGTERM'))
process.on('SIGINT', () => shutdown('SIGINT'))

bootstrap().catch((err) => {
  logger.error({ err }, 'Failed to start')
  process.exit(1)
})

export { app }
