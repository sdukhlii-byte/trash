// src/middleware/errorHandler.ts
import { FastifyError, FastifyRequest, FastifyReply } from 'fastify'
import { ZodError } from 'zod'
import { logger } from '../utils/logger'

export function errorHandler(
  error: FastifyError,
  req: FastifyRequest,
  reply: FastifyReply
): void {
  // Zod validation errors
  if (error instanceof ZodError) {
    reply.code(400).send({
      ok: false,
      error: 'Validation error',
      code: 'VALIDATION_ERROR',
      details: error.flatten().fieldErrors,
    })
    return
  }

  // Fastify validation errors
  if (error.validation) {
    reply.code(400).send({
      ok: false,
      error: 'Bad request',
      code: 'BAD_REQUEST',
      details: error.validation,
    })
    return
  }

  // Known HTTP errors
  if (error.statusCode && error.statusCode < 500) {
    reply.code(error.statusCode).send({
      ok: false,
      error: error.message,
      code: error.code ?? 'HTTP_ERROR',
    })
    return
  }

  // Unexpected errors — log and return 500
  logger.error({
    err: error,
    url: req.url,
    method: req.method,
    userId: (req.user as any)?.userId,
  }, 'Unhandled error')

  reply.code(500).send({
    ok: false,
    error: 'Internal server error',
    code: 'INTERNAL_ERROR',
  })
}
