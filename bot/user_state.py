"""
user_state.py — машина состояний пользователя.

Состояния:
  NEW        — не прошёл онбординг
  ONBOARDED  — профиль есть, нет доступа, триал не использован
  TRIAL      — активный пробный период
  SUBSCRIBED — активная оплаченная подписка
  EXPIRED    — был доступ, истёк

Изменения v2:
- Кэш состояния в Redis (TTL 60 сек).
  Было: 5–6 DB/Redis запросов на каждый text/button.
  Стало: 1 Redis GET на горячем пути; полный расчёт только при промахе кэша.
- invalidate_state_cache() — вызывается при активации триала и оплате.
"""
from enum import Enum
from datetime import datetime, timezone
import json
import logging

from db import is_onboarded, get_profile, kv_get, kv_set, kv_del

logger = logging.getLogger(__name__)

STATE_CACHE_TTL = 60   # секунд
_STATE_KEY = "__user_state_cache__"


class UserState(Enum):
    NEW        = "new"
    ONBOARDED  = "onboarded"
    TRIAL      = "trial"
    SUBSCRIBED = "subscribed"
    EXPIRED    = "expired"


async def get_user_state(user_id: int) -> UserState:
    """
    Single source of truth for user state.
    Cache-first: Redis (TTL 60s) → compute from DB on miss.

    Fallback on infrastructure failure (v2):
    - If Redis unavailable on read → fall through to _compute
    - If Postgres unavailable in _compute:
        * If valid cached state existed before → return that (user had a known state)
        * Otherwise → return EXPIRED (safe: shows paywall, doesn't grant free access)
    - Never return NEW on infrastructure error (breaks onboarding for existing users)
    """
    last_known: UserState | None = None

    try:
        # 1. Fast path: cache
        try:
            cached = await kv_get(user_id, _STATE_KEY)
            if cached:
                try:
                    last_known = UserState(cached)
                    return last_known
                except ValueError:
                    pass  # invalid cached value — recompute
        except Exception:
            pass  # Redis unavailable — continue to _compute

        # 2. Slow path: compute from DB
        state = await _compute_user_state(user_id)

        # 3. Cache result (don't cache NEW — it changes fast)
        if state != UserState.NEW:
            try:
                await kv_set(user_id, _STATE_KEY, state.value, ttl=STATE_CACHE_TTL)
            except Exception:
                pass

        return state

    except Exception as e:
        logger.error(f"get_user_state error uid={user_id}: {e}", exc_info=True)
        # Safe fallback hierarchy:
        # - Known cached state → trust it (user existed before outage)
        # - No cache → EXPIRED: shows paywall, doesn't grant free access
        # Previously was SUBSCRIBED (grants free access during outage — wrong).
        if last_known is not None and last_known != UserState.NEW:
            return last_known
        return UserState.EXPIRED


async def invalidate_state_cache(user_id: int) -> None:
    """Сбросить кэш при любом событии меняющем состояние (оплата, триал, онбординг)."""
    await kv_del(user_id, _STATE_KEY)
    logger.debug(f"State cache invalidated for user {user_id}")


async def _compute_user_state(user_id: int) -> UserState:
    """
    Расчёт состояния пользователя.
    Сначала Redis (быстро, без Postgres), потом Postgres если нужно.
    При ошибке Postgres — не возвращаем NEW, а пробрасываем исключение
    чтобы get_user_state мог его залогировать и вернуть правильный fallback.
    """
    # 1. Онбординг — только Redis, без Postgres
    onboarded = await is_onboarded(user_id)
    if not onboarded:
        p = await get_profile(user_id)
        if not p.get("niche"):
            return UserState.NEW
        # Нишу записали, но onboarded=True ещё нет (edge case)
        return UserState.ONBOARDED

    # 2. Подписка + триал — один батч-запрос к Postgres
    try:
        from lava_payments import get_user_access_state
        access = await get_user_access_state(user_id)
    except Exception as pg_err:
        # Postgres недоступен — пробрасываем, get_user_state залогирует
        raise RuntimeError(f"Postgres unavailable in _compute_user_state: {pg_err}") from pg_err

    if access["has_active_sub"]:
        return UserState.SUBSCRIBED

    if access["has_active_trial"]:
        return UserState.TRIAL

    if access["ever_had_access"]:
        return UserState.EXPIRED

    return UserState.ONBOARDED


def has_access(state: UserState) -> bool:
    return state in (UserState.TRIAL, UserState.SUBSCRIBED)
