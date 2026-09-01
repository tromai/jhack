"""Shared fixtures for the `jhack scenario snapshot` live integration smoke tests.

These tests talk to a real Juju controller/model (bootstrapped out-of-band, e.g.
via Concierge) and shell out to the actual `jhack` CLI, so they are excluded from
the regular unit test run (see jhack/tests/integration/README.md).
"""

import os
import shutil
import sys

import pytest

collect_ignore_glob = []

# Skip this whole package unless explicitly requested, so `pytest jhack/tests`
# (the unit test entrypoint) never tries to run these against a real cloud.
if not os.environ.get("JHACK_RUN_INTEGRATION_TESTS"):
    collect_ignore_glob = ["*"]


def pytest_configure(config):
    config.addinivalue_line("markers", "machine: test targets a machine (lxd) substrate.")
    config.addinivalue_line("markers", "k8s: test targets a Canonical Kubernetes substrate.")


@pytest.fixture(scope="session")
def jhack_bin() -> str:
    """Path to the jhack executable under test."""
    exe = shutil.which("jhack")
    if not exe:
        pytest.fail("jhack is not on PATH; install it (e.g. `pip install .`) before running.")
    return exe


@pytest.fixture(scope="session")
def python_bin() -> str:
    """Fall back to invoking jhack as `python -m jhack.main` if useful for debugging."""
    return sys.executable
