"""The API side of the privacy layer: per-workflow settings, the encrypted Redis vault, the
stash that keeps a masked step's real output for later queues, and the run's report.

Detection itself is flowforge_engine.privacy. Masking happens where steps are recorded
(app.services.runs.ExecutionRecorder): with the workflow's "mask sensitive data in stored
step inputs/outputs" on (the default), node_executions rows, live events, and the final
output hold "[REDACTED:<TYPE>]" instead of what was found. A run that continues on another
queue still needs the real outputs of earlier steps, so those are kept encrypted in Redis
(this module's OutputStash) until the run ends, never in Postgres.
"""

import uuid
from collections import Counter
from typing import Any

from flowforge_engine.privacy import PrivacyPolicy
from redis.asyncio import Redis

from app.core.config import settings
from app.core.crypto import get_cipher

# Redaction mappings (PII Redact) live this long.
VAULT_TTL_SECONDS = 3600


def policy_for(privacy_json: dict[str, Any] | None) -> PrivacyPolicy:
    return PrivacyPolicy.model_validate(privacy_json or {})


class RedisVault:
    """The engine's SecretVault for one execution: Fernet-encrypted values with a TTL, keyed
    to the execution so no other run can read them."""

    def __init__(self, redis: Redis, execution_id: uuid.UUID | str, ttl: int = VAULT_TTL_SECONDS):
        self._redis, self._execution_id, self._ttl = redis, str(execution_id), ttl

    def _key(self, ref: str) -> str:
        return f"flowforge:vault:{self._execution_id}:{ref}"

    async def put(self, data: dict[str, str]) -> str:
        ref = uuid.uuid4().hex
        await self._redis.set(self._key(ref), get_cipher().encrypt(dict(data)), ex=self._ttl)
        return ref

    async def get(self, ref: str) -> dict[str, str] | None:
        if not ref or not ref.isalnum():
            return None
        token = await self._redis.get(self._key(ref))
        return get_cipher().decrypt(token) if token else None


class OutputStash:
    """Real (unmasked) outputs of an execution's masked steps, encrypted, for the segments
    that run after a queue hand-off. Cleared when the run finishes; expires regardless."""

    def __init__(self, redis: Redis, execution_id: uuid.UUID | str):
        self._redis = redis
        self._key = f"flowforge:execution:{execution_id}:raw-outputs"

    async def put(self, node_key: str, output: dict[str, Any] | None) -> None:
        await self._redis.hset(self._key, node_key, get_cipher().encrypt({"output": output}))
        await self._redis.expire(self._key, int(settings.EXECUTION_TIME_LIMIT_SECONDS + 3600))

    async def all(self) -> dict[str, dict[str, Any] | None]:
        raw = await self._redis.hgetall(self._key)
        return {key: get_cipher().decrypt(value)["output"] for key, value in raw.items()}

    async def clear(self) -> None:
        await self._redis.delete(self._key)


def step_findings(privacy: dict[str, Any] | None) -> tuple[Counter[str], Counter[str]]:
    """(by type, by category) of one step: the larger of what its input and its output held
    (a step usually passes its input's values on, so adding them up would count twice)."""
    by_type: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    for part in ("input", "output"):
        section = (privacy or {}).get(part) or {}
        by_type |= Counter(section.get("by_type") or {})
        by_category |= Counter(section.get("by_category") or {})
    return by_type, by_category


def privacy_report(rows: list[Any], *, masked: bool) -> dict[str, Any]:
    """The run's Privacy Report from its node rows: counts and types per step, and what the
    guard did. The run's totals take, per type, the most any one step held: a value passed
    from step to step is counted once (two different values seen only in different steps
    count once too, so the total is a lower bound)."""
    by_type: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    nodes, guard_actions = [], Counter()
    for row in rows:
        privacy = row.privacy_json or {}
        step_type, step_category = step_findings(privacy)
        guard = privacy.get("guard")
        if guard and guard.get("action") not in (None, "off", "clean"):
            guard_actions[guard["action"]] += 1
        if not step_type and not (guard and guard.get("total")):
            continue
        by_type |= step_type
        by_category |= step_category
        nodes.append({
            "node_key": row.node_key, "label": row.node_label, "node_type": row.node_type,
            "total": sum(step_type.values()), "by_type": dict(step_type), "by_category": dict(step_category),
            "guard": guard,
        })
    return {
        "total": sum(by_type.values()), "by_type": dict(by_type), "by_category": dict(by_category),
        "guard_actions": dict(guard_actions), "masked": masked, "nodes": nodes,
    }
