"""
user_state.py — машина состояний пользователя.

Состояния:
  NEW        — не прошёл онбординг (нет профиля)
  ONBOARDED  — профиль есть, нет доступа, триал ещё не использован
  TRIAL      — активный пробный период
  SUBSCRIBED — активная оплаченная подписка
  EXPIRED    — был доступ (триал или подписка), истёк

Переходы:
  NEW → ONBOARDED (после онбординга)
  ONBOARDED → TRIAL (активировал триал)
  ONBOARDED → SUBSCRIBED (сразу оплатил)
  TRIAL → SUBSCRIBED (оплатил во время триала)
  TRIAL → EXPIRED (триал истёк без оплаты)
  SUBSCRIBED → EXPIRED (подписка истекла)
  EXPIRED → SUBSCRIBED (оплатил снова)
"""

from enum import Enum
from datetime import datetime, timezone

from db import is_onboarded, get_profile
from lava_payments import get_subscription, get_trial, has_used_trial


class UserState(Enum):
    NEW        = "new"
    ONBOARDED  = "onboarded"
    TRIAL      = "trial"
    SUBSCRIBED = "subscribed"
    EXPIRED    = "expired"


async def get_user_state(user_id: int) -> UserState:
    """
    Определяет текущее состояние пользователя.
    Единая точка правды — вызывается из всех точек входа.
    """
    now = datetime.now(timezone.utc)

    # 1. Проверяем онбординг
    if not await is_onboarded(user_id):
        p = await get_profile(user_id)
        if not p.get("niche"):
            return UserState.NEW

    # 2. Активная подписка
    sub = await get_subscription(user_id)
    if sub:
        return UserState.SUBSCRIBED

    # 3. Активный триал
    trial = await get_trial(user_id)
    if trial:
        return UserState.TRIAL

    # 4. Был ли вообще доступ (триал или подписка когда-либо)
    used_trial = await has_used_trial(user_id)
    if used_trial:
        return UserState.EXPIRED

    # Проверяем была ли подписка (в таблице subscriptions, статус cancelled/expired)
    from db import _get_pool
    pool = _get_pool()
    async with pool.acquire() as conn:
        had_sub = await conn.fetchrow(
            "SELECT 1 FROM subscriptions WHERE user_id=$1", user_id
        )
    if had_sub:
        return UserState.EXPIRED

    # 5. Онбординг пройден, но ни триала ни подписки не было
    return UserState.ONBOARDED


def has_access(state: UserState) -> bool:
    """Есть ли у пользователя доступ к функциям бота."""
    return state in (UserState.TRIAL, UserState.SUBSCRIBED)
