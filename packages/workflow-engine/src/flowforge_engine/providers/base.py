from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    is_mock: bool

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str: ...

    async def embed(self, text: str) -> list[float]: ...


@runtime_checkable
class EmailProvider(Protocol):
    name: str
    is_mock: bool

    async def send_email(
        self, to: list[str], cc: list[str], subject: str, body: str
    ) -> dict[str, Any]: ...
