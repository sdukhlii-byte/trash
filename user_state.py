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
from lava_payments import get_subscription, get_trial, has_used_trial

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
    Единая точка определения состояния.
    Сначала проверяем кэш — если промах, считаем из БД и кладём в кэш.
    """
    try:
        # 1. Быстрый путь: кэш
        cached = await kv_get(user_id, _STATE_KEY)
        if cached:
            try:
                return UserState(cached)
            except ValueError:
                pass  # невалидное значение — пересчитаем

        # 2. Медленный путь: расчёт из БД
        state = await _compute_user_state(user_id)

        # 3. Кэшируем (не кэшируем NEW — до онбординга состояние может меняться быстро)
        if state != UserState.NEW:
            await kv_set(user_id, _STATE_KEY, state.value, ttl=STATE_CACHE_TTL)

        return state
    except Exception as e:
        logger.error(f"get_user_state error uid={user_id}: {e}", exc_info=True)
        return UserState.NEW


async def invalidate_state_cache(user_id: int) -> None:
    """Сбросить кэш при любом событии меняющем состояние (оплата, триал, онбординг)."""
    await kv_del(user_id, _STATE_KEY)
    logger.debug(f"State cache invalidated for user {user_id}")


async def _compute_user_state(user_id: int) -> UserState:
    now = datetime.now(timezone.utc)

    # 1. Онбординг
    if not await is_onboarded(user_id):
        p = await get_profile(user_id)
        if not p.get("niche"):
            return UserState.NEW
        return UserState.ONBOARDED

    # 2. Активная подписка
    sub = await get_subscription(user_id)
    if sub:
        return UserState.SUBSCRIBED

    # 3. Активный триал
    trial = await get_trial(user_id)
    if trial:
        return UserState.TRIAL

    # 4. Был ли когда-либо доступ
    used_trial = await has_used_trial(user_id)
    if used_trial:
        return UserState.EXPIRED

    from db import _get_pool
    pool = _get_pool()
    async with pool.acquire() as conn:
        had_sub = await conn.fetchrow(
            "SELECT 1 FROM subscriptions WHERE user_id=$1", user_id
        )
    if had_sub:
        return UserState.EXPIRED

    return UserState.ONBOARDED


def has_access(state: UserState) -> bool:
    return state in (UserState.TRIAL, UserState.SUBSCRIBED)
