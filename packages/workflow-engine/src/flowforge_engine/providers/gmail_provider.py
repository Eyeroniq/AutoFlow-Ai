from __future__ import annotations

from typing import Any


class GmailProvider:
    """Real Gmail sending needs per-user OAuth, which lands with the integrations phase."""

    name = "gmail"
    is_mock = False

    async def send_email(
        self, to: list[str], cc: list[str], subject: str, body: str
    ) -> dict[str, Any]:
        raise NotImplementedError("Gmail OAuth integration is not implemented yet")
