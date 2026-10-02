"""The shared Redis rate limiter: the sliding window itself, that its state lives in Redis (not in
process memory), and that auth, workflow runs, and Telegram all go through it."""

import asyncio

import pytest

from app.core.config import settings
from app.core.rate_limit import Limit, RateLimiter, RateLimitExceeded, limiter
from app.core.redis import get_redis, new_redis
from tests.support import create_workflow
from tests.test_deployments import greeting_graph


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", True)
    limiter.reset()
    yield limiter
    limiter.reset()


def test_limits_parse_from_text():
    assert Limit.parse("10/minute") == Limit(10, 60)
    assert Limit.parse("240 / minute") == Limit(240, 60)
    assert Limit.parse("5/second") == Limit(5, 1)
    assert Limit.parse("100/hour") == Limit(100, 3600)
    assert Limit.parse("2 per 30 second") == Limit(2, 30)
    assert str(Limit(10, 60)) == "10/minute"
    for bad in ("", "ten/minute", "10/week", "10"):
        with pytest.raises(ValueError):
            Limit.parse(bad)


async def test_a_window_admits_the_limit_then_refuses_and_counts_refusals(on):
    results = [await on.hit("t", "a", "3/minute") for _ in range(5)]
    assert [r.allowed for r in results] == [True, True, True, False, False]
    assert [r.remaining for r in results[:3]] == [2, 1, 0]
    assert [r.refusals for r in results] == [0, 0, 0, 1, 2]  # the first refusal is identifiable
    assert 0 < results[3].retry_after <= 60


async def test_each_scope_and_key_has_its_own_budget(on):
    assert (await on.hit("t", "a", "1/minute")).allowed
    assert not (await on.hit("t", "a", "1/minute")).allowed
    assert (await on.hit("t", "b", "1/minute")).allowed  # another key
    assert (await on.hit("other", "a", "1/minute")).allowed  # another scope


async def test_the_window_slides_so_old_hits_stop_counting(on):
    assert (await on.hit("t", "a", "2/second")).allowed
    assert (await on.hit("t", "a", "2/second")).allowed
    assert not (await on.hit("t", "a", "2/second")).allowed
    await asyncio.sleep(1.1)
    assert (await on.hit("t", "a", "2/second")).allowed


async def test_refused_requests_are_not_counted_so_a_client_that_backs_off_recovers(on):
    await on.hit("t", "a", "1/second")
    for _ in range(20):
        assert not (await on.hit("t", "a", "1/second")).allowed
    await asyncio.sleep(1.1)
    assert (await on.hit("t", "a", "1/second")).allowed


async def test_concurrent_requests_cannot_both_take_the_last_slot(on):
    results = await asyncio.gather(*[on.hit("race", "a", "5/minute") for _ in range(25)])
    assert sum(r.allowed for r in results) == 5


async def test_the_counters_live_in_redis_not_in_the_process(on):
    """A second limiter object, on its own Redis connection (another API replica or a worker), sees
    the first one's hits."""
    other = RateLimiter()
    redis = new_redis()
    try:
        assert (await on.hit("shared", "a", "2/minute")).allowed
        assert (await other.hit("shared", "a", "2/minute", redis=redis)).allowed
        assert not (await on.hit("shared", "a", "2/minute")).allowed
        assert not (await other.hit("shared", "a", "2/minute", redis=redis)).allowed
    finally:
        await redis.aclose()
    keys = [k async for k in get_redis().scan_iter("flowforge:ratelimit:shared:*")]
    assert keys, "the window is stored under flowforge:ratelimit:"


async def test_check_raises_with_the_limit_and_when_to_retry(on):
    await on.check("c", "a", "1/minute")
    with pytest.raises(RateLimitExceeded) as caught:
        await on.check("c", "a", "1/minute")
    assert caught.value.detail == "1/minute" and caught.value.retry_after > 0


async def test_a_dead_redis_lets_requests_through_instead_of_taking_the_api_down(monkeypatch):
    broken = new_redis("redis://127.0.0.1:1/0")
    monkeypatch.setattr(limiter, "enabled", True)
    try:
        assert (await limiter.hit("t", "a", "1/minute", redis=broken)).allowed
        assert (await limiter.hit("t", "a", "1/minute", redis=broken)).allowed
    finally:
        await broken.aclose()


async def test_disabled_means_no_counting(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    assert all([(await limiter.hit("off", "a", "1/minute")).allowed for _ in range(5)])


# --- the endpoints ------------------------------------------------------------------------------


async def test_auth_endpoints_are_limited_per_client_address(client, on, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_RATE_LIMIT", "3/minute")
    body = {"email": "nobody@example.com", "password": "wrong-password"}
    codes = [(await client.post("/api/auth/login", json=body)).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    limited = await client.post("/api/auth/login", json=body)
    assert limited.status_code == 429 and limited.headers["retry-after"].isdigit()
    assert "Too many requests: limit is 3/minute" in limited.json()["detail"]


async def test_behind_a_proxy_callers_are_told_apart_by_forwarded_address(client, on, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_RATE_LIMIT", "2/minute")
    body = {"email": "nobody@example.com", "password": "wrong-password"}

    async def attempt(address):
        return (await client.post("/api/auth/login", json=body, headers={"X-Forwarded-For": address})).status_code

    # Not trusted (the default): every caller looks like the proxy, and shares one budget.
    assert [await attempt("1.1.1.1"), await attempt("2.2.2.2"), await attempt("3.3.3.3")] == [401, 401, 429]
    limiter.reset()
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
    assert [await attempt("1.1.1.1"), await attempt("1.1.1.1"), await attempt("1.1.1.1")] == [401, 401, 429]
    assert await attempt("2.2.2.2") == 401  # a different client has its own budget


async def test_starting_runs_is_limited_per_user(client, user, user_factory, task_queue, on, monkeypatch):
    monkeypatch.setattr(settings, "WORKFLOW_RUN_RATE_LIMIT", "2/minute")
    wid = await create_workflow(client, user, greeting_graph())

    async def run(who):
        return (await client.post(f"/api/workflows/{wid}/run", json={"inputs": {"name": "A"}}, headers=who.headers)).status_code

    assert [await run(user), await run(user), await run(user)] == [202, 202, 429]
    other = await user_factory()
    other_wid = await create_workflow(client, other, greeting_graph())
    assert (await client.post(f"/api/workflows/{other_wid}/run", json={"inputs": {"name": "A"}}, headers=other.headers)).status_code == 202


def test_no_in_memory_limiter_is_left():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    assert "slowapi" not in (root / "requirements.txt").read_text()
    offenders = [str(p) for p in (root / "app").rglob("*.py") if "slowapi" in p.read_text(encoding="utf-8")]
    assert offenders == []
