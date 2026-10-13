from __future__ import annotations

import pytest


from src.config.config_repository import MemoryConfigProvider, set_config_provider

# Some test modules inspect settings while pytest imports their module. Install
# an explicit memory provider before collection so those reads never reach Redis.
_collection_provider = MemoryConfigProvider()
_collection_provider.mutate(
    lambda doc: doc["business"].update({"startup_probe_enabled": False})
)
set_config_provider(_collection_provider)


@pytest.fixture(autouse=True)
def memory_config_provider():
    """Tests use an explicit in-memory provider and never contact Redis."""
    provider = MemoryConfigProvider()
    provider.mutate(
        lambda doc: doc["business"].update({"startup_probe_enabled": False})
    )
    set_config_provider(provider)
    yield provider
    set_config_provider(None)
