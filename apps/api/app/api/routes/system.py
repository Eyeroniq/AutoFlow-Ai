from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import CurrentUser
from app.core.config import settings
from app.core.redis import get_redis
from app.services import automations, demo
from app.services.discord_voice import WATCH_KEY

router = APIRouter(prefix="/system", tags=["system"])


class DemoUsage(BaseModel):
    runs: int
    runs_limit: int
    tokens: int
    tokens_limit: int


class SystemInfo(BaseModel):
    public_demo: bool
    # Whether the signed-in account is the owner (always true outside demo mode).
    is_owner: bool
    # Providers whose accounts are personal: in demo mode a visitor connects their own, never the owner's.
    own_account_providers: list[str]
    # Demo visitors: today's usage against the caps. None for the owner and outside demo mode.
    usage: DemoUsage | None = None


@router.get(
    "",
    response_model=SystemInfo,
    summary="Deployment mode and what this account may use",
    description="The editor uses this to explain, on a node, that a visitor must connect their own Gmail, Discord, Telegram, Notion, or Airtable.",
)
async def system_info(user: CurrentUser) -> SystemInfo:
    visitor = demo.is_visitor(user)
    return SystemInfo(
        public_demo=settings.PUBLIC_DEMO,
        is_owner=not visitor,
        own_account_providers=sorted(demo.PERSONAL_PROVIDERS) if visitor else [],
        usage=DemoUsage(**await demo.usage(user.id)) if visitor else None,
    )


class ServiceState(BaseModel):
    paused: bool
    # discord: the channel being watched right now (None while paused or when no pipeline has a Discord Voice Meeting block).
    watching: str | None = None


class AutomationsState(BaseModel):
    discord: ServiceState
    telegram: ServiceState


class AutomationUpdate(BaseModel):
    paused: bool


async def _state() -> AutomationsState:
    redis = get_redis()
    watch = await redis.get(WATCH_KEY)
    return AutomationsState(
        discord=ServiceState(paused=await automations.is_paused(redis, "discord"), watching=watch.decode() if isinstance(watch, bytes) else watch),
        telegram=ServiceState(paused=await automations.is_paused(redis, "telegram")),
    )


def _owner_only(user: CurrentUser) -> None:
    if demo.is_visitor(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="The Discord and Telegram services belong to the owner of this demo.")


@router.get(
    "/automations",
    response_model=AutomationsState,
    summary="Whether the Discord and Telegram services are paused",
    description="The always-on Discord monitor and Telegram bot keep running as containers; this is their pause switch.",
)
async def get_automations(user: CurrentUser) -> AutomationsState:
    _owner_only(user)
    return await _state()


@router.put(
    "/automations/{name}",
    response_model=AutomationsState,
    summary="Pause or resume the Discord monitor or the Telegram bot",
    description=(
        "Paused: the Telegram bot ignores messages (they are dropped, not replayed on resume) and the Discord monitor stops "
        "watching its channel and ends a recording in progress. Takes effect within about 10 seconds; the containers stay up."
    ),
)
async def put_automation(name: Literal["discord", "telegram"], body: AutomationUpdate, user: CurrentUser) -> AutomationsState:
    _owner_only(user)
    await automations.set_paused(get_redis(), name, body.paused)
    return await _state()
