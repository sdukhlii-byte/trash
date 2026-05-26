// src/types/index.ts

// ─────────────────────────────────────────────────────────────────────────────
// Auth
// ─────────────────────────────────────────────────────────────────────────────

export interface TelegramInitData {
  query_id?: string
  user: {
    id: number
    first_name: string
    last_name?: string
    username?: string
    language_code?: string
  }
  auth_date: number
  hash: string
}

export interface AuthTokenPayload {
  userId: number
  telegramId: number
  iat: number
  exp: number
}

// ─────────────────────────────────────────────────────────────────────────────
// User
// ─────────────────────────────────────────────────────────────────────────────

export type UserState = 'new' | 'onboarded' | 'trial' | 'subscribed' | 'expired'

export interface UserProfile {
  id: number
  telegramId: number
  username?: string
  firstName?: string
  state: UserState
  isOnboarded: boolean
  preferredModel: string
  creatorProfile?: CreatorProfile
  subscription?: SubscriptionInfo
  voiceProgress: VoiceProgress
  stats: UserStats
}

export interface CreatorProfile {
  niche?: string
  audience?: string
  tone?: string
  goals?: string
  platform?: string
}

export interface VoiceProgress {
  totalSignals: number
  level: number           // 0-5
  percentage: number      // 0-100
  label: string           // "Начинающий" / "Продвинутый" / ...
}

export interface UserStats {
  totalGenerations: number
  byTool: Record<string, number>
  streakDays: number
}

export interface SubscriptionInfo {
  tier: string
  expiresAt: string
  daysLeft: number
  isActive: boolean
}

// ─────────────────────────────────────────────────────────────────────────────
// Tools
// ─────────────────────────────────────────────────────────────────────────────

export interface ToolDefinition {
  key: string
  name: string
  emoji: string
  description: string
  inputSchema: ToolInputSchema
  allowedActions: string[]
  hasInterviewStep: boolean
  hasPickStep: boolean
  maxQuestions: number
  acceptsPhotos: boolean
  model: string
  photo?: string
}

export interface ToolInputSchema {
  topic?: { type: 'string'; required: boolean }
  description?: { type: 'string'; required: boolean }
  photos?: { type: 'array'; required: boolean }
}

// ─────────────────────────────────────────────────────────────────────────────
// Generation
// ─────────────────────────────────────────────────────────────────────────────

export interface GenerateRequest {
  toolKey: string
  input: GenerateInput
  sessionId?: string
}

export interface GenerateInput {
  topic?: string
  description?: string
  photos?: string[]     // base64
  interviewAnswers?: InterviewAnswer[]
  pickedVariant?: number
}

export interface InterviewAnswer {
  question: string
  answer: string
}

export interface GenerateResponse {
  generationId: number
  toolKey: string
  content: string
  keyboardVersion: number
  allowedActions: string[]
  completedActions: string[]
  isComplete: boolean
  nextStep?: 'interview' | 'pick' | 'done'
  interviewQuestion?: string
  variants?: string[]
}

// ─────────────────────────────────────────────────────────────────────────────
// Refinement
// ─────────────────────────────────────────────────────────────────────────────

export interface RefineRequest {
  generationId: number
  action: string
  metadata?: Record<string, string>  // e.g. { style: 'curiosity' }
}

export interface RefineResponse {
  generationId: number          // ID нового поколения
  parentId: number              // исходный generation
  content: string
  action: string
  keyboardVersion: number
  allowedActions: string[]
  completedActions: string[]
}

// ─────────────────────────────────────────────────────────────────────────────
// Materials
// ─────────────────────────────────────────────────────────────────────────────

export interface MaterialSaveRequest {
  generationId: number
  title?: string
  tags?: string[]
}

export interface MaterialListParams {
  toolKey?: string
  page?: number
  limit?: number
  search?: string
  favorites?: boolean
}

export interface MaterialItem {
  id: number
  toolKey: string
  toolName: string
  title: string
  content: string
  tags: string[]
  isFavorite: boolean
  generationId?: number
  createdAt: string
}

// ─────────────────────────────────────────────────────────────────────────────
// Voice Feedback
// ─────────────────────────────────────────────────────────────────────────────

export interface VoiceFeedbackRequest {
  generationId: number
  signal: 'approved' | 'rejected'
  note?: string
}

// ─────────────────────────────────────────────────────────────────────────────
// Subscription
// ─────────────────────────────────────────────────────────────────────────────

export type SubscriptionTier = '1m' | '3m' | '6m' | '12m'

export interface SubscriptionActivateRequest {
  tier: SubscriptionTier
  paymentId: string
  amountEur: number
}

// ─────────────────────────────────────────────────────────────────────────────
// API common
// ─────────────────────────────────────────────────────────────────────────────

export interface ApiResponse<T = unknown> {
  ok: boolean
  data?: T
  error?: string
  code?: string
}

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  limit: number
  hasMore: boolean
}
