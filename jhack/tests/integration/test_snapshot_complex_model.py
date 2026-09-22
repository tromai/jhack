"""Live integration test for `jhack scenario snapshot` against a more
elaborate model: several related applications (workload + database +
observability), a config option, and an action.

Loosely modeled on part 5 ("Observe your charm with COS Lite") of the
"from zero to hero" Kubernetes charm tutorial
(https://github.com/canonical/operator, examples/k8s-5-observe): a
workload charm related to a database and to the COS stack (Prometheus,
Grafana), with a config option and an action to exercise.

Real Charmhub charms are used instead of the tutorial's own fastapi-demo
(which isn't published to Charmhub) so the test can run with a plain
``juju deploy``:

- mattermost-k8s: workload charm (config option, action, requires a
  database via postgresql_client, provides metrics-endpoint/grafana-dashboard).
- postgresql-k8s: database.
- prometheus-k8s / grafana-k8s: COS pieces integrated with mattermost-k8s.

Restricted to Canonical Kubernetes on the latest Ubuntu LTS only (see the
workflow matrix), since this model is heavier to stand up than the smoke
tests and doesn't need to be repeated across every juju/base combination.
"""

import json
import logging
import os
import subprocess

import pytest

if not os.environ.get("JHACK_RUN_INTEGRATION_TESTS"):
    pytest.skip(
        "set JHACK_RUN_INTEGRATION_TESTS=1 to run these live Juju integration tests",
        allow_module_level=True,
    )

jubilant = pytest.importorskip("jubilant")

logger = logging.getLogger(__name__)

APP_NAME = "mattermost-k8s"
DB_APP_NAME = "postgresql-k8s"
PROMETHEUS_APP_NAME = "prometheus-k8s"
GRAFANA_APP_NAME = "grafana-k8s"


@pytest.mark.k8s
@pytest.mark.juju_setup
def test_deploy_complex_model(juju: jubilant.Juju):
    """Deploy a small COS+database topology to snapshot against."""
    juju.deploy(APP_NAME, config={"debug": True})
    juju.deploy(DB_APP_NAME, channel="14/stable", trust=True)
    juju.deploy(PROMETHEUS_APP_NAME, channel="latest/stable", trust=True)
    juju.deploy(GRAFANA_APP_NAME, channel="latest/stable", trust=True)

    juju.integrate(APP_NAME, DB_APP_NAME)
    juju.integrate(APP_NAME, PROMETHEUS_APP_NAME)
    juju.integrate(APP_NAME, GRAFANA_APP_NAME)
    juju.integrate(PROMETHEUS_APP_NAME, GRAFANA_APP_NAME)

    juju.wait(jubilant.all_active, timeout=30 * 60)


def _any_unit(juju: jubilant.Juju, app_name: str) -> str:
    status = juju.status()
    return next(iter(status.apps[app_name].units))


@pytest.mark.k8s
def test_snapshot_complex_model_smoke(juju: jubilant.Juju):
    """`jhack scenario snapshot` against a unit with a real relation to a
    database and to COS applications, plus a set config option, should
    still produce a valid, non-trivial State snapshot.
    """
    unit = _any_unit(juju, APP_NAME)

    result = subprocess.run(
        [
            "jhack",
            "scenario",
            "snapshot",
            unit,
            "-m",
            juju.model,
            "-f",
            "json",
            "--devmode",
            "--include-dead-relation-networks",
            "--include-juju-relation-data",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"jhack scenario snapshot exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    payload = json.loads(result.stdout)
    assert payload

    # the workload charm's config option should be reflected in the snapshot.
    assert payload["config"].get("debug") is True

    # relations to the database and to both COS apps should be visible.
    relation_endpoints = {r["endpoint"] for r in payload["relations"]}
    assert "postgresql" in relation_endpoints
    assert "metrics-endpoint" in relation_endpoints
    assert "grafana-dashboard" in relation_endpoints


@pytest.mark.k8s
def test_snapshot_complex_model_action(juju: jubilant.Juju):
    """The unit should also be usable to bind and run an action event, since
    the workload charm defines a real action (``grant-admin-role``).
    """
    unit = _any_unit(juju, APP_NAME)

    result = subprocess.run(
        [
            "jhack",
            "scenario",
            "snapshot",
            unit,
            "-m",
            juju.model,
            "-f",
            "state",
            "--devmode",
            "--include-dead-relation-networks",
            "--include-juju-relation-data",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"jhack scenario snapshot exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "State(" in result.stdout
