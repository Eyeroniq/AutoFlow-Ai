from pathlib import Path

import pytest

from flowforge_engine.providers import MockEmailProvider
from flowforge_engine.testing import live_tests_enabled

HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):
    if live_tests_enabled(config.option.markexpr):
        return
    skip = pytest.mark.skip(reason="live test: calls real APIs; run with `pytest -m live`")
    for item in items:
        if "live" in item.keywords and item.path.is_relative_to(HERE):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _clear_mock_outbox():
    MockEmailProvider.clear_outbox()
    yield
    MockEmailProvider.clear_outbox()


@pytest.fixture(autouse=True)
def _testing_mode(monkeypatch):
    # Unit tests use mocks: TESTING=true makes the provider factory hand out mocks even
    # when real keys are present in the environment. Live tests build their own settings.
    monkeypatch.setenv("TESTING", "true")
