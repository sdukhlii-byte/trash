"""
flows/utm.py — отслеживание источника трафика.

Без этого невозможно понять:
- какой Telegram-канал даёт платящих пользователей
- окупается ли Facebook реклама
- какой креатив конвертирует лучше

Как работает:
  /start ref_XXXXX → уже есть (реферальная программа)
  /start utm_facebook_story_01 → новый формат для платного трафика

  Все параметры сохраняются в Redis при первом /start
  При активации триала и оплате — записывается источник
  Администратор видит статистику через /admin utm

Deep link форматы:
  t.me/CasaValeriaBot?start=utm_fb_expert_01    — Facebook реклама #1
  t.me/CasaValeriaBot?start=utm_tg_smm_channel  — посев в SMM-канале
  t.me/CasaValeriaBot?start=utm_organic          — органика
  t.me/CasaValeriaBot?start=ref_12345           — реферал (уже работает)
"""
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_UTM_KEY      = "__utm_source__"
_UTM_STAT_KEY = "utm_stats"


# ── Сохранение источника ──────────────────────────────────────────────────────

async def track_start(user_id: int, start_arg: str | None) -> dict:
    """
    Вызывается при /start.
    Парсит аргумент и сохраняет источник трафика.
    Возвращает parsed utm dict.
    """
    from db import kv_get, kv_set, get_redis

    if not start_arg or start_arg.startswith("ref_"):
        return {}

    # Парсим utm параметры
    utm = _parse_utm(start_arg)
    if not utm:
        return {}

    # Не перезаписываем если уже есть (пользователь перезашёл — источник тот же)
    existing = await kv_get(user_id, _UTM_KEY)
    if existing:
        return json.loads(existing)

    utm["first_seen"] = datetime.now(timezone.utc).isoformat()
    utm["user_id"]    = user_id
    await kv_set(user_id, _UTM_KEY, json.dumps(utm, ensure_ascii=False))

    # Глобальная статистика
    try:
        r   = await get_redis()
        key = f"utm:starts:{utm.get('source', 'unknown')}:{utm.get('campaign', 'none')}"
        await r.incr(key)
        await r.expire(key, 86400 * 90)  # 90 дней
    except Exception as e:
        logger.warning(f"utm global stat error: {e}")

    logger.info(f"UTM tracked: user={user_id} {utm}")
    return utm


def _parse_utm(arg: str) -> dict:
    """
    utm_fb_expert_01       → {source: fb, campaign: expert, creative: 01}
    utm_tg_smm_channel     → {source: tg, campaign: smm_channel}
    utm_organic            → {source: organic}
    """
    if not arg.startswith("utm_"):
        return {}
    parts  = arg[4:].split("_")
    source = parts[0] if parts else "unknown"
    campaign = parts[1] if len(parts) > 1 else "none"
    creative = parts[2] if len(parts) > 2 else "none"
    return {"source": source, "campaign": campaign, "creative": creative, "raw": arg}


async def get_utm(user_id: int) -> dict:
    from db import kv_get
    try:
        raw = await kv_get(user_id, _UTM_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


# ── Атрибуция событий ─────────────────────────────────────────────────────────

async def track_event(user_id: int, event: str) -> None:
    """
    event: "trial_activated" | "payment_completed" | "onboarding_done" | "churned"
    Записывает событие с привязкой к источнику трафика.
    """
    from db import get_redis
    utm = await get_utm(user_id)
    source   = utm.get("source", "unknown")
    campaign = utm.get("campaign", "none")
    try:
        r   = await get_redis()
        key = f"utm:{event}:{source}:{campaign}"
        await r.incr(key)
        await r.expire(key, 86400 * 90)
        logger.info(f"UTM event: {event} source={source} campaign={campaign} user={user_id}")
    except Exception as e:
        logger.warning(f"track_event error: {e}")


# ── Статистика для админа ─────────────────────────────────────────────────────

async def get_utm_report() -> str:
    """Генерирует текстовый отчёт по источникам трафика."""
    from db import get_redis
    try:
        r    = await get_redis()
        lines = ["📊 *UTM-статистика*\n"]

        # Собираем все ключи
        events   = ["starts", "onboarding_done", "trial_activated", "payment_completed"]
        sources  = {}

        for event in events:
            async for key in r.scan_iter(f"utm:{event}:*"):
                parts    = key.split(":")
                if len(parts) < 4:
                    continue
                source   = parts[2]
                campaign = parts[3]
                count    = int(await r.get(key) or 0)
                label    = f"{source}/{campaign}"
                if label not in sources:
                    sources[label] = {}
                sources[label][event] = count

        if not sources:
            return "📊 UTM-статистика пока пуста. Добавь параметры в ссылки рекламы."

        for label, stats in sorted(sources.items(), key=lambda x: x[1].get("starts", 0), reverse=True):
            starts   = stats.get("starts", 0)
            onb      = stats.get("onboarding_done", 0)
            trial    = stats.get("trial_activated", 0)
            paid     = stats.get("payment_completed", 0)
            cr_trial = f"{trial/starts*100:.0f}%" if starts else "—"
            cr_paid  = f"{paid/trial*100:.0f}%" if trial else "—"
            lines.append(
                f"*{label}*\n"
                f"  Стартов: {starts} → Онб: {onb} → Триал: {trial} → Оплат: {paid}\n"
                f"  CR в триал: {cr_trial} | CR триал→оплата: {cr_paid}\n"
            )

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"get_utm_report error: {e}")
        return f"Ошибка получения статистики: {e}"
