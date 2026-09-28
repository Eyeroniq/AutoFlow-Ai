"""Fix 2: deleting a WorkflowNode keeps its NodeExecution history, readable via snapshots."""

import copy
import uuid

from sqlalchemy import delete, select

from app.models.execution import NodeExecution
from app.models.workflow import WorkflowNode
from app.schemas.workflow import EXAMPLE_GRAPH


async def run_example(client, user):
    wid = (await client.post("/api/workflows", json={"name": "History"}, headers=user.headers)).json()["id"]
    await client.put(f"/api/workflows/{wid}", json={"graph": EXAMPLE_GRAPH}, headers=user.headers)
    execution = (await client.post(f"/api/workflows/{wid}/run", headers=user.headers)).json()
    assert execution["status"] == "success"
    return wid, execution


async def test_removing_a_node_from_the_graph_keeps_its_history(client, user):
    wid, execution = await run_example(client, user)
    gmail_before = next(n for n in execution["node_executions"] if n["node_key"] == "gmail")
    assert gmail_before["node_id"] is not None

    graph = copy.deepcopy(EXAMPLE_GRAPH)
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "gmail"]
    graph["nodes"][-1]["config"]["value"] = "{{gemini.response}}"
    graph["edges"] = [{"source": "input", "target": "gemini"}, {"source": "gemini", "target": "output"}]
    assert (await client.put(f"/api/workflows/{wid}", json={"graph": graph}, headers=user.headers)).status_code == 200

    history = (await client.get(f"/api/executions/{execution['id']}", headers=user.headers)).json()
    by_key = {n["node_key"]: n for n in history["node_executions"]}

    assert set(by_key) == {"input", "gemini", "gmail", "output"}
    gmail = by_key["gmail"]
    assert gmail["node_id"] is None
    assert (gmail["node_type"], gmail["node_label"]) == ("gmail", "Email summary")
    assert gmail["output"] == gmail_before["output"]
    assert gmail["status"] == "success"
    # Nodes that survived the save are still linked to their rows.
    for key in ("input", "gemini", "output"):
        assert by_key[key]["node_id"] == next(
            n["node_id"] for n in execution["node_executions"] if n["node_key"] == key
        )


async def test_deleting_a_node_row_sets_node_id_null(client, user, db_session):
    wid, execution = await run_example(client, user)

    await db_session.execute(
        delete(WorkflowNode).where(WorkflowNode.workflow_id == uuid.UUID(wid), WorkflowNode.node_key == "gemini")
    )
    await db_session.flush()

    rows = {
        r.node_key: r
        for r in await db_session.scalars(
            select(NodeExecution)
            .where(NodeExecution.execution_id == uuid.UUID(execution["id"]))
            .execution_options(populate_existing=True)
        )
    }
    assert len(rows) == 4
    assert rows["gemini"].node_id is None
    assert (rows["gemini"].node_type, rows["gemini"].node_label) == ("gemini", "Summarize")
    assert rows["gemini"].output_json["response"].startswith("[MOCK RESPONSE to:")
    assert rows["input"].node_id is not None
