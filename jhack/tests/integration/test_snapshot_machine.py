"""Live smoke test for `jhack scenario snapshot` against a real LXD (machine) unit.

Scope: smoke test only. We don't assert anything about the *content* of the
snapshot beyond "the command didn't fail and produced non-trivial output that
looks like the requested format". See jhack/tests/integration/README.md.
"""

import json
import logging
import os
import subprocess

import jubilant
import pytest

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.machine

APP_NAME = "ubuntu"
# Charm base to deploy, e.g. "22.04", "24.04", "26.04". Defaults to the
# controller/model's default if unset.
BASE = os.environ.get("JHACK_TEST_BASE")


@pytest.mark.juju_setup
def test_deploy(juju: jubilant.Juju):
    """Deploy a plain ubuntu machine charm to snapshot against."""
    kwargs = {}
    if BASE:
        kwargs["base"] = f"ubuntu@{BASE}"
    juju.deploy(APP_NAME, **kwargs)
    juju.wait(jubilant.all_active, timeout=20 * 60)


def _run_snapshot(juju: jubilant.Juju, unit: str, *extra_args: str) -> subprocess.CompletedProcess:
    cmd = ["jhack", "scenario", "snapshot", unit, "-m", juju.model, *extra_args]
    logger.info("running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def _any_unit(juju: jubilant.Juju) -> str:
    status = juju.status()
    return next(iter(status.apps[APP_NAME].units))


@pytest.mark.parametrize("format_", ["state", "json", "pytest"])
def test_snapshot_smoke(juju: jubilant.Juju, format_: str):
    """`jhack scenario snapshot <unit> -f <format_>` should succeed and produce output."""
    unit = _any_unit(juju)

    result = _run_snapshot(juju, unit, "-f", format_)

    assert result.returncode == 0, (
        f"jhack scenario snapshot exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert result.stdout.strip(), f"no output produced (stderr:\n{result.stderr})"

    if format_ == "json":
        # strict: must be valid, non-empty JSON.
        payload = json.loads(result.stdout)
        assert payload
    elif format_ == "state":
        assert "State(" in result.stdout
    elif format_ == "pytest":
        assert "def test_case" in result.stdout
