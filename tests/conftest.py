"""Shared pytest fixtures for spirrow-mindwire."""

from __future__ import annotations

import pytest

from spirrow_mindwire.obligations import ObligationsManifest, load_manifest


@pytest.fixture(scope="session")
def obligations() -> ObligationsManifest:
    """The in-repo loop-readable obligations manifest, loaded once per test session.

    Adapters take the manifest by constructor injection (no module-global path
    read), so nearly every test that spins up an ``ImplementerSdkAdapter`` or
    ``NaysayerSdkAdapter`` needs one. Cached at session scope because the
    manifest is immutable and loading it re-parses YAML.
    """
    return load_manifest()
