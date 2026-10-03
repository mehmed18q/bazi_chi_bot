"""Aiogram handlers for private-chat setup, invitations, and gameplay."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType

from ..countdown import (
    CountdownScheduler,
    CountdownService,
)
from ..game import (
    GameService,
)
from . import challenges, countdowns, daily, gameplay, groups, menus, payments, tournaments
from .payments import PaymentAccessMiddleware
from .sponsors import SponsorMembershipMiddleware


def build_router(
    service: GameService,
    *,
    countdown_service: CountdownService | None = None,
    countdown_scheduler: CountdownScheduler | None = None,
    countdown_admin_ids: frozenset[int] = frozenset(),
    payment_reviewer_ids: frozenset[int] = frozenset({1767552952}),
    countdown_timezone: str = "Asia/Tehran",
) -> Router:
    router = Router(name="private_game")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    admin_ids = countdown_admin_ids | payment_reviewer_ids
    payment_middleware = PaymentAccessMiddleware(service, admin_ids)
    membership_middleware = SponsorMembershipMiddleware(service, admin_ids)
    router.message.middleware(payment_middleware)
    router.callback_query.middleware(payment_middleware)
    router.message.middleware(membership_middleware)
    router.callback_query.middleware(membership_middleware)
    # State-specific countdown input must be handled before free-form final responses.
    payments.register_handlers(
        router,
        service,
        admin_ids=admin_ids,
        reviewer_ids=payment_reviewer_ids,
    )
    menus.register_handlers(router, service, countdown_admin_ids=admin_ids)
    daily.register_handlers(router, service, countdown_service, countdown_scheduler)
    countdowns.register_handlers(
        router,
        service,
        countdown_service=countdown_service,
        countdown_scheduler=countdown_scheduler,
        countdown_admin_ids=admin_ids,
        countdown_timezone=countdown_timezone,
    )
    gameplay.register_handlers(router, service, admin_ids=admin_ids)
    tournaments.register_handlers(router, service)
    groups.register_private_handlers(router, service)
    challenges.register_handlers(router, service, admin_ids=admin_ids)
    return router
