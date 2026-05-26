// src/routes/index.ts
import { FastifyInstance } from 'fastify'
import { authRoutes }         from './auth'
import { userRoutes }         from './user'
import { toolRoutes }         from './tools'
import { generateRoutes }     from './generate'
import { refineRoutes }       from './refine'
import { materialsRoutes }    from './materials'
import { subscriptionRoutes } from './subscription'
import { voiceRoutes }        from './voice'
import { webhookRoutes }      from './webhook'
import { adminRoutes }        from './admin'

export async function registerRoutes(app: FastifyInstance): Promise<void> {
  const v1 = { prefix: '/api/v1' }

  await app.register(authRoutes,         v1)
  await app.register(userRoutes,         v1)
  await app.register(toolRoutes,         v1)
  await app.register(generateRoutes,     v1)
  await app.register(refineRoutes,       v1)
  await app.register(materialsRoutes,    v1)
  await app.register(subscriptionRoutes, v1)
  await app.register(voiceRoutes,        v1)
  await app.register(webhookRoutes,      v1)
  await app.register(adminRoutes,        { prefix: '/api/admin' })
}
