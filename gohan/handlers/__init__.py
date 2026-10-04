"""Router assembly — order matters: the plain-text intake goes LAST so it
never swallows slash commands owned by later routers."""

from __future__ import annotations

from aiogram import Router

from . import actions, channels, common, compose


def build_router() -> Router:
    root = Router(name="gohan")
    root.include_router(common.router)
    root.include_router(compose.router)
    root.include_router(channels.router)
    root.include_router(actions.router)
    root.include_router(compose.intake_router)
    return root
