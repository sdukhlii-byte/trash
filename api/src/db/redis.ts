// src/db/redis.ts
import Redis from 'ioredis'
import { logger } from '../utils/logger'

export const redis = new Redis(process.env.REDIS_URL!, {
  maxRetriesPerRequest: 3,
  enableReadyCheck: true,
  lazyConnect: true,
})

redis.on('error', (err) => logger.error({ err }, 'Redis error'))
redis.on('connect', () => logger.debug('Redis connected'))

// ── Typed helpers ─────────────────────────────────────────────────────────────

export const kv = {
  get: (key: string) => redis.get(key),

  set: (key: string, value: string, ttlSeconds?: number) =>
    ttlSeconds
      ? redis.set(key, value, 'EX', ttlSeconds)
      : redis.set(key, value),

  del: (...keys: string[]) => redis.del(...keys),

  getJson: async <T>(key: string): Promise<T | null> => {
    const raw = await redis.get(key)
    if (!raw) return null
    try { return JSON.parse(raw) as T } catch { return null }
  },

  setJson: (key: string, value: unknown, ttlSeconds?: number) =>
    kv.set(key, JSON.stringify(value), ttlSeconds),

  // User-scoped keys (mirrors Python bot pattern)
  userKey: (userId: number, suffix: string) => `user:${userId}:${suffix}`,
}

// ── Cache helpers ─────────────────────────────────────────────────────────────

export const cache = {
  userState: {
    key: (userId: number) => kv.userKey(userId, 'state'),
    get: (userId: number) => kv.get(cache.userState.key(userId)),
    set: (userId: number, state: string) =>
      kv.set(cache.userState.key(userId), state, 60),
    del: (userId: number) => kv.del(cache.userState.key(userId)),
  },

  session: {
    key: (userId: number) => kv.userKey(userId, 'session'),
    get: <T>(userId: number) => kv.getJson<T>(cache.session.key(userId)),
    set: (userId: number, data: unknown) =>
      kv.setJson(cache.session.key(userId), data, 3600),
    del: (userId: number) => kv.del(cache.session.key(userId)),
  },

  lock: {
    key: (userId: number, op: string) => kv.userKey(userId, `lock:${op}`),
    acquire: async (userId: number, op: string, ttl = 30): Promise<boolean> => {
      const res = await redis.set(
        cache.lock.key(userId, op), '1', 'EX', ttl, 'NX'
      )
      return res === 'OK'
    },
    release: (userId: number, op: string) =>
      kv.del(cache.lock.key(userId, op)),
  },
}
