"""
bot_context.py — глобальный singleton для доступа к PTB Application.

Используется в местах где нет доступа к ctx (например _finish_onboarding),
но нужно запланировать job_queue задание.

Использование:
    from bot_context import set_app, get_app
    set_app(ptb_app)       # в main.py сразу после build_app()
    app = get_app()        # в любом модуле
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

_app: "Application | None" = None


def set_app(app: "Application") -> None:
    global _app
    _app = app


def get_app() -> "Application | None":
    return _app
