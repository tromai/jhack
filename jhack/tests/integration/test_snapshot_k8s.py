"""Live smoke test for `jhack scenario snapshot` against a real Canonical Kubernetes unit.

Scope: smoke test only, mirrors test_snapshot_machine.py. See
jhack/tests/integration/README.md.

Note: snappass-test only publishes a single base (ubuntu@20.04) to Charmhub, so
the JHACK_TEST_BASE env var (used to vary the machine smoke test's charm base)
has no k8s equivalent here and is intentionally ignored. The k8s leg of the CI
matrix still runs once per nominal "base" entry for structural symmetry with
the machine leg, even though the deployed charm is identical each time.
"""

import json
import logging
import subprocess

import jubilant
import pytest

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.k8s

APP_NAME = "snappass-test"


@pytest.mark.juju_setup
def test_deploy(juju: jubilant.Juju):
    """Deploy snappass-test (a small k8s sidecar charm) to snapshot against."""
    juju.deploy(APP_NAME, trust=True)
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
        payload = json.loads(result.stdout)
        assert payload
    elif format_ == "state":
        assert "State(" in result.stdout
    elif format_ == "pytest":
        assert "def test_case" in result.stdout
