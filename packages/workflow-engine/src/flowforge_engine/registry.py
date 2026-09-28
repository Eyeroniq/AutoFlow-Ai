from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from flowforge_engine.models import GraphNode, NodeContext, NodeResult


class NodeConfig(BaseModel):
    """Base for node config schemas. Unknown keys are rejected so typos surface in validation."""

    model_config = ConfigDict(extra="forbid")


ConfigT = TypeVar("ConfigT", bound=BaseModel)


class NodeDefinition(ABC, Generic[ConfigT]):
    """A node type. Instances are stateless; one is shared per registry."""

    type: ClassVar[str]
    category: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]
    icon: ClassVar[str]
    config_schema: ClassVar[type[BaseModel]]
    # Optional: declares the output keys so validation can catch {{node.typo}}.
    output_schema: ClassVar[type[BaseModel] | None] = None
    # Branching nodes name their outgoing handles; only the edge matching the
    # output's "branch" value stays active.
    branches: ClassVar[tuple[str, ...]] = ()
    # Output nodes contribute {name: value} to the execution's final output.
    produces_final_output: ClassVar[bool] = False

    @abstractmethod
    async def execute(self, context: NodeContext, config: ConfigT) -> NodeResult: ...

    def required_providers(self, node: GraphNode) -> list[tuple[str, str]]:
        """(provider, config field) pairs whose credentials this node needs to run.

        Validation reports "Authentication missing" for any that aren't configured.
        """
        return []

    def output_keys(self, node: GraphNode) -> set[str] | None:
        """Top-level output keys this node will produce, or None if they can't be known."""
        if self.output_schema is None:
            return None
        return set(self.output_schema.model_fields)

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "type": cls.type,
            "category": cls.category,
            "label": cls.label,
            "description": cls.description,
            "icon": cls.icon,
            "config_schema": cls.config_schema.model_json_schema(),
            "output_schema": cls.output_schema.model_json_schema() if cls.output_schema else None,
            "branches": list(cls.branches),
        }


N = TypeVar("N", bound=type[NodeDefinition[Any]])

_REQUIRED_ATTRS = ("category", "label", "description", "icon", "config_schema")


class NodeRegistry:
    def __init__(self) -> None:
        self._nodes: dict[str, NodeDefinition[Any]] = {}

    def register(self, node_type: str) -> Callable[[N], N]:
        def decorator(cls: N) -> N:
            self.add(node_type, cls)
            return cls

        return decorator

    def add(self, node_type: str, cls: type[NodeDefinition[Any]]) -> None:
        if node_type in self._nodes:
            raise ValueError(f"Node type '{node_type}' is already registered")
        missing = [attr for attr in _REQUIRED_ATTRS if not hasattr(cls, attr)]
        if missing:
            raise TypeError(f"{cls.__name__} is missing required attributes: {', '.join(missing)}")
        cls.type = node_type
        self._nodes[node_type] = cls()

    def get(self, node_type: str) -> NodeDefinition[Any] | None:
        return self._nodes.get(node_type)

    def __contains__(self, node_type: object) -> bool:
        return node_type in self._nodes

    def types(self) -> list[str]:
        return sorted(self._nodes)

    def definitions(self) -> list[NodeDefinition[Any]]:
        return [self._nodes[t] for t in self.types()]

    def describe(self) -> list[dict[str, Any]]:
        """Every node type with its JSON config schema — for editor introspection."""
        return [type(d).describe() for d in self.definitions()]


default_registry = NodeRegistry()


def register_node(node_type: str, *, registry: NodeRegistry | None = None) -> Callable[[N], N]:
    return (registry or default_registry).register(node_type)


def get_node_definition(node_type: str) -> NodeDefinition[Any] | None:
    return default_registry.get(node_type)


def list_node_definitions() -> list[dict[str, Any]]:
    return default_registry.describe()
