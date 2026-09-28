import copy
import uuid

import pytest
from sqlalchemy import select

from app.models.workflow import WorkflowEdge, WorkflowNode, WorkflowVariable
from app.schemas.workflow import EXAMPLE_GRAPH


async def create_workflow(client, user, name="Test workflow", **extra):
    response = await client.post("/api/workflows", json={"name": name, **extra}, headers=user.headers)
    assert response.status_code == 201, response.text
    return response.json()


async def put_graph(client, user, workflow_id, graph):
    return await client.put(f"/api/workflows/{workflow_id}", json={"graph": graph}, headers=user.headers)


class TestCrud:
    async def test_lifecycle(self, client, user):
        created = await create_workflow(client, user, name="  Summarize  ", description="first")
        assert created["name"] == "Summarize"
        assert created["status"] == "draft"
        assert created["version"] == 1
        assert created["graph"] == {"nodes": [], "edges": [], "variables": []}
        wid = created["id"]

        listing = (await client.get("/api/workflows", headers=user.headers)).json()
        assert [w["id"] for w in listing] == [wid]
        assert "graph" not in listing[0]

        fetched = await client.get(f"/api/workflows/{wid}", headers=user.headers)
        assert fetched.status_code == 200 and fetched.json()["description"] == "first"

        renamed = await client.put(
            f"/api/workflows/{wid}", json={"name": "Renamed", "status": "active"}, headers=user.headers
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Renamed"
        assert renamed.json()["status"] == "active"
        assert renamed.json()["description"] == "first"  # untouched when omitted
        assert renamed.json()["version"] == 1  # only graph changes bump the version

        cleared = await client.put(f"/api/workflows/{wid}", json={"description": None}, headers=user.headers)
        assert cleared.json()["description"] is None

        with_graph = await put_graph(client, user, wid, EXAMPLE_GRAPH)
        assert with_graph.status_code == 200
        assert with_graph.json()["version"] == 2
        assert [n["id"] for n in with_graph.json()["graph"]["nodes"]] == ["input", "gemini", "gmail", "output"]

        assert (await client.delete(f"/api/workflows/{wid}", headers=user.headers)).status_code == 204
        assert (await client.get(f"/api/workflows/{wid}", headers=user.headers)).status_code == 404
        assert (await client.get("/api/workflows", headers=user.headers)).json() == []

    async def test_requires_authentication(self, client):
        assert (await client.get("/api/workflows")).status_code == 401
        assert (await client.post("/api/workflows", json={"name": "x"})).status_code == 401

    @pytest.mark.parametrize("name", ["", "   ", "x" * 256])
    async def test_rejects_bad_names(self, client, user, name):
        response = await client.post("/api/workflows", json={"name": name}, headers=user.headers)
        assert response.status_code == 422

    async def test_unknown_workflow_is_404(self, client, user):
        response = await client.get(f"/api/workflows/{uuid.uuid4()}", headers=user.headers)
        assert response.status_code == 404


class TestOwnership:
    async def test_other_users_cannot_see_or_touch_a_workflow(self, client, user_factory):
        owner, intruder = await user_factory(), await user_factory()
        wid = (await create_workflow(client, owner))["id"]
        await put_graph(client, owner, wid, EXAMPLE_GRAPH)

        h = intruder.headers
        assert (await client.get(f"/api/workflows/{wid}", headers=h)).status_code == 404
        assert (await client.put(f"/api/workflows/{wid}", json={"name": "pwned"}, headers=h)).status_code == 404
        assert (await client.delete(f"/api/workflows/{wid}", headers=h)).status_code == 404
        assert (await client.post(f"/api/workflows/{wid}/validate", headers=h)).status_code == 404
        assert (await client.post(f"/api/workflows/{wid}/run", headers=h)).status_code == 404
        assert (await client.get(f"/api/workflows/{wid}/executions", headers=h)).status_code == 404
        assert (await client.get("/api/workflows", headers=h)).json() == []

        still_there = await client.get(f"/api/workflows/{wid}", headers=owner.headers)
        assert still_there.json()["name"] == "Test workflow"


class TestGraphSync:
    async def rows(self, db_session, wid):
        wid = uuid.UUID(wid)
        nodes = {n.node_key: n for n in await db_session.scalars(select(WorkflowNode).where(WorkflowNode.workflow_id == wid))}
        edges = list(await db_session.scalars(select(WorkflowEdge).where(WorkflowEdge.workflow_id == wid)))
        variables = list(await db_session.scalars(select(WorkflowVariable).where(WorkflowVariable.workflow_id == wid)))
        return nodes, edges, variables

    async def test_put_graph_writes_nodes_edges_and_variables(self, client, user, db_session):
        wid = (await create_workflow(client, user))["id"]
        await put_graph(client, user, wid, EXAMPLE_GRAPH)

        nodes, edges, variables = await self.rows(db_session, wid)
        assert set(nodes) == {"input", "gemini", "gmail", "output"}
        assert nodes["gemini"].node_type == "gemini"
        assert nodes["gemini"].label == "Summarize"
        assert nodes["gmail"].position_x == 520
        assert nodes["gemini"].config_json["user_prompt"].startswith("Write a three-sentence summary")
        by_id = {n.id: key for key, n in nodes.items()}
        assert sorted((by_id[e.source_node_id], by_id[e.target_node_id]) for e in edges) == [
            ("gemini", "gmail"), ("gmail", "output"), ("input", "gemini"),
        ]
        assert [(v.key, v.value, v.var_type.value) for v in variables] == [("recipient", "demo@flowforge.ai", "workflow")]

    async def test_resave_keeps_surviving_node_rows(self, client, user, db_session):
        wid = (await create_workflow(client, user))["id"]
        await put_graph(client, user, wid, EXAMPLE_GRAPH)
        before, _, _ = await self.rows(db_session, wid)

        graph = copy.deepcopy(EXAMPLE_GRAPH)
        graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "gmail"]
        graph["nodes"].append({"id": "note", "type": "text", "config": {"text": "{{gemini.response}}"}})
        graph["nodes"][-2]["config"]["value"] = "{{gemini.response}}"  # output no longer mentions gmail
        graph["edges"] = [
            {"source": "input", "target": "gemini"},
            {"source": "gemini", "target": "note"},
            {"source": "note", "target": "output"},
        ]
        graph["variables"] = []
        assert (await put_graph(client, user, wid, graph)).status_code == 200

        after, edges, variables = await self.rows(db_session, wid)
        assert set(after) == {"input", "gemini", "output", "note"}
        for key in ("input", "gemini", "output"):
            assert after[key].id == before[key].id
        assert after["note"].label == "Text"  # defaults to the node type's label
        assert len(edges) == 3 and variables == []

    async def test_duplicate_node_ids_are_rejected(self, client, user):
        wid = (await create_workflow(client, user))["id"]
        graph = {"nodes": [{"id": "a", "type": "text", "config": {"text": "1"}}, {"id": "a", "type": "text", "config": {"text": "2"}}]}
        response = await put_graph(client, user, wid, graph)
        assert response.status_code == 422
        assert response.json()["detail"] == "Duplicate node id(s): a"

    @pytest.mark.parametrize("node_id", ["has space", "dot.ted", "", "x" * 101])
    async def test_invalid_node_ids_are_rejected(self, client, user, node_id):
        wid = (await create_workflow(client, user))["id"]
        response = await put_graph(client, user, wid, {"nodes": [{"id": node_id, "type": "text"}]})
        assert response.status_code == 422

    async def test_react_flow_handles_and_extra_fields_round_trip(self, client, user):
        wid = (await create_workflow(client, user))["id"]
        graph = {
            "nodes": [
                {"id": "c", "type": "condition", "config": {"left": 1, "operator": "equals", "right": 1}, "selected": True},
                {"id": "t", "type": "text", "config": {"text": "yes"}},
            ],
            "edges": [{"id": "e1", "source": "c", "target": "t", "sourceHandle": "true", "animated": True}],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        }
        saved = (await put_graph(client, user, wid, graph)).json()["graph"]
        assert saved["edges"][0]["source_handle"] == "true"
        assert saved["edges"][0]["animated"] is True
        assert saved["nodes"][0]["selected"] is True
        assert saved["viewport"] == {"x": 0, "y": 0, "zoom": 1}

    async def test_graph_with_semantic_errors_can_still_be_saved(self, client, user):
        wid = (await create_workflow(client, user))["id"]
        graph = {"nodes": [{"id": "x", "type": "teleport"}], "edges": [{"source": "x", "target": "ghost"}]}
        assert (await put_graph(client, user, wid, graph)).status_code == 200


class TestValidateEndpoint:
    async def validate(self, client, user, graph):
        wid = (await create_workflow(client, user))["id"]
        assert (await put_graph(client, user, wid, graph)).status_code == 200
        response = await client.post(f"/api/workflows/{wid}/validate", headers=user.headers)
        assert response.status_code == 200
        return response.json()

    async def test_example_graph_is_valid(self, client, user):
        assert await self.validate(client, user, EXAMPLE_GRAPH) == {"valid": True, "errors": []}

    async def test_empty_graph_is_valid(self, client, user):
        assert (await self.validate(client, user, {}))["valid"] is True

    async def test_unknown_node_type(self, client, user):
        graph = copy.deepcopy(EXAMPLE_GRAPH)
        graph["nodes"][1]["type"] = "gpt-9000"
        result = await self.validate(client, user, graph)
        assert result["valid"] is False
        assert [(e["code"], e["node_id"]) for e in result["errors"]] == [("unknown_node_type", "gemini")]

    async def test_cycle(self, client, user):
        graph = copy.deepcopy(EXAMPLE_GRAPH)
        graph["edges"].append({"source": "output", "target": "input"})
        result = await self.validate(client, user, graph)
        [error] = result["errors"]
        assert error["code"] == "cycle"
        assert "input → gemini → gmail → output → input" in error["message"]

    async def test_missing_required_field(self, client, user):
        graph = copy.deepcopy(EXAMPLE_GRAPH)
        del graph["nodes"][2]["config"]["subject"]
        [error] = (await self.validate(client, user, graph))["errors"]
        assert (error["code"], error["node_id"], error["field"]) == ("missing_config", "gmail", "subject")

    async def test_unresolvable_variable_reference(self, client, user):
        graph = copy.deepcopy(EXAMPLE_GRAPH)
        graph["nodes"][2]["config"]["body"] = "{{gemini.summary}} for {{vars.manager}}"
        errors = (await self.validate(client, user, graph))["errors"]
        assert [(e["code"], e["field"]) for e in errors] == [
            ("unresolvable_reference", "body"),
            ("unresolvable_reference", "body"),
        ]
        assert "has no output 'summary'" in errors[0]["message"]
        assert "no workflow variable 'manager'" in errors[1]["message"]

    async def test_broken_edge(self, client, user):
        graph = copy.deepcopy(EXAMPLE_GRAPH)
        graph["edges"].append({"source": "gmail", "target": "slack"})
        [error] = (await self.validate(client, user, graph))["errors"]
        assert error["code"] == "broken_edge"
