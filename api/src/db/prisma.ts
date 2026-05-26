// src/db/prisma.ts
import { PrismaClient } from '@prisma/client'
import { logger } from '../utils/logger'

declare global {
  // eslint-disable-next-line no-var
  var __prisma: PrismaClient | undefined
}

export const prisma =
  global.__prisma ??
  new PrismaClient({
    log: process.env.NODE_ENV === 'development'
      ? [{ emit: 'event', level: 'query' }]
      : [],
  })

if (process.env.NODE_ENV !== 'production') {
  global.__prisma = prisma
}

// Log slow queries in dev
if (process.env.NODE_ENV === 'development') {
  // @ts-ignore — prisma event typing
  prisma.$on('query', (e: any) => {
    if (e.duration > 100) {
      logger.warn(`Slow query (${e.duration}ms): ${e.query}`)
    }
  })
}
