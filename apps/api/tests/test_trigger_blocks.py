"""Trigger blocks on the canvas (Schedule, Email, Telegram Trigger) become the workflow's real triggers when the
pipeline is saved, and switch them off again when they are switched off or deleted."""

from tests.support import create_workflow


def graph(block_config=None, with_block=True):
    nodes = [
        {"id": "input", "type": "input", "config": {"name": "topic", "input_type": "text", "default": "x"}},
        {"id": "out", "type": "output", "config": {"name": "r", "value": "{{input.value}}"}},
    ]
    if with_block:
        nodes.insert(0, {"id": "when", "type": "schedule_trigger", "config": block_config or {}})
    return {"nodes": nodes, "edges": [{"source": "input", "target": "out"}], "variables": []}


async def triggers(client, user, wid):
    response = await client.get(f"/api/workflows/{wid}/triggers", headers=user.headers)
    assert response.status_code == 200, response.text
    return {t["type"]: t for t in response.json()["triggers"]} if "triggers" in response.json() else response.json()


def schedule(body):
    return body["schedule"] if "schedule" in body else next(t for t in body if t["type"] == "schedule")


async def test_a_schedule_block_creates_the_trigger_and_enabled_switches_it_on(client, user):
    wid = await create_workflow(client, user, graph({"enabled": True, "cron": "0 9 * * 1", "timezone": "Asia/Kolkata"}))
    trigger = schedule(await triggers(client, user, wid))
    assert trigger["enabled"] is True and trigger["config"]["cron"] == "0 9 * * 1" and trigger["config"]["timezone"] == "Asia/Kolkata"
    assert trigger["next_run_at"] is not None

    # Switching the block off (and saving) switches the trigger off.
    await client.put(f"/api/workflows/{wid}", json={"graph": graph({"enabled": False, "cron": "0 9 * * 1"})}, headers=user.headers)
    assert schedule(await triggers(client, user, wid))["enabled"] is False


async def test_a_block_is_off_by_default_so_nothing_runs_by_surprise(client, user):
    wid = await create_workflow(client, user, graph({}))
    assert schedule(await triggers(client, user, wid))["enabled"] is False


async def test_a_bad_cron_leaves_the_trigger_off_with_the_reason(client, user):
    wid = await create_workflow(client, user, graph({"enabled": True, "cron": "not a cron"}))
    trigger = schedule(await triggers(client, user, wid))
    assert trigger["enabled"] is False and trigger["last_error"]


async def test_deleting_the_block_switches_its_trigger_off(client, user):
    wid = await create_workflow(client, user, graph({"enabled": True, "cron": "0 9 * * *"}))
    assert schedule(await triggers(client, user, wid))["enabled"] is True
    await client.put(f"/api/workflows/{wid}", json={"graph": graph(with_block=False)}, headers=user.headers)
    assert schedule(await triggers(client, user, wid))["enabled"] is False


async def test_a_duplicate_does_not_start_firing_by_itself(client, user):
    wid = await create_workflow(client, user, graph({"enabled": True, "cron": "0 9 * * *"}))
    copy = (await client.post(f"/api/workflows/{wid}/duplicate", headers=user.headers)).json()
    block = next(n for n in copy["graph"]["nodes"] if n["type"] == "schedule_trigger")
    assert block["config"]["enabled"] is False
    assert schedule(await triggers(client, user, copy["id"]))["enabled"] is False


async def test_a_save_that_leaves_the_block_alone_keeps_what_the_triggers_panel_set(client, user):
    block = {"enabled": False, "cron": "0 9 * * *"}
    wid = await create_workflow(client, user, graph(block))
    await client.put(f"/api/workflows/{wid}/triggers/schedule", json={"enabled": True, "config": {"cron": "0 10 * * *"}}, headers=user.headers)
    await client.put(f"/api/workflows/{wid}", json={"graph": graph(block)}, headers=user.headers)  # an autosave
    trigger = schedule(await triggers(client, user, wid))
    assert trigger["enabled"] is True and trigger["config"]["cron"] == "0 10 * * *"


async def test_a_telegram_block_cannot_be_switched_on_before_the_pipeline_is_deployed(client, user):
    nodes = graph(with_block=False)
    nodes["nodes"].insert(0, {"id": "tg", "type": "telegram_trigger", "config": {"enabled": True, "allowed_chat_ids": "123"}})
    wid = await create_workflow(client, user, nodes)
    body = await triggers(client, user, wid)
    trigger = body["telegram"] if "telegram" in body else next(t for t in body if t["type"] == "telegram")
    assert trigger["enabled"] is False and "Deploy" in (trigger["last_error"] or "")
