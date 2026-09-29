from typing import Any

from fastapi import APIRouter
from flowforge_engine import GraphNode, default_registry
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser

router = APIRouter(prefix="/nodes", tags=["nodes"])

# Engine categories -> the editor's library groups. Unknown categories get their own group.
GROUPS = {
    "io": "General",
    "logic": "General",
    "ai": "LLM",
    "lists": "Lists",
    "sources": "Data sources",
    "integration": "Integrations",
    "documents": "Documents",
    "audio": "Audio",
}
GROUP_ORDER = ["General", "LLM", "Lists", "Data sources", "Integrations", "Documents", "Audio"]


class NodeTypeRead(BaseModel):
    type: str
    category: str = Field(description="Engine category: io, logic, ai, integration, ...")
    group: str = Field(description="Library group: General, LLM, Lists, Data sources, Integrations, Documents, Audio.")
    label: str
    description: str
    icon: str = Field(description="lucide icon name, e.g. 'sparkles'.")
    queue: str = Field(description="Celery queue whose workers run it: default, llm, ocr, or audio.")
    portable: bool = Field(description="Runs on whichever worker holds the run (never causes a queue hand-off).")
    interruptible: bool
    branches: list[str] = Field(description="Named output handles (Condition: true/false); empty = one output.")
    has_input: bool = Field(description="Whether the node takes incoming edges (Input nodes don't).")
    item_fields: list[str] = Field(
        default_factory=list, description="Per-item template fields, where {{item}} and {{index}} can be used."
    )
    produces_final_output: bool
    config_schema: dict[str, Any] = Field(description="JSON Schema of the node's config.")
    output_schema: dict[str, Any] | None
    output_keys: list[str] | None = Field(
        description="Top-level output keys for {{node.key}} references; null when they depend on the config "
        "(Input nodes expose `value` and their `name`)."
    )


def _describe(node_type: str) -> NodeTypeRead:
    definition = default_registry.get(node_type)
    assert definition is not None
    info = type(definition).describe()
    dynamic = node_type == "input"
    keys = None if dynamic else definition.output_keys(GraphNode(id="node", type=node_type))
    group = GROUPS.get(definition.category, definition.category.title())
    return NodeTypeRead(
        type=node_type,
        category=definition.category,
        group=group,
        label=info["label"],
        description=info["description"],
        icon=info["icon"],
        queue=definition.queue,
        portable=definition.portable,
        interruptible=definition.interruptible,
        branches=info["branches"],
        has_input=node_type != "input",
        item_fields=info["item_fields"],
        produces_final_output=definition.produces_final_output,
        config_schema=info["config_schema"],
        output_schema=info["output_schema"],
        output_keys=sorted(keys) if keys is not None else None,
    )


@router.get(
    "",
    response_model=list[NodeTypeRead],
    summary="Every node type the engine can run",
    description=(
        "The editor builds its node library and config forms from this list: each entry carries "
        "the node's JSON config schema (from the engine's Pydantic model), its output keys for "
        "`{{node.key}}` references, its handles, and the queue it runs on."
    ),
)
async def list_nodes(_: CurrentUser) -> list[NodeTypeRead]:
    nodes = [_describe(t) for t in default_registry.types()]
    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    return sorted(nodes, key=lambda n: (order.get(n.group, len(order)), n.label.lower()))
