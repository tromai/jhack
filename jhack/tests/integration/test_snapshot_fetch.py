"""Live integration test for `jhack scenario snapshot --fetch`.

grafana-k8s is used here because, unlike the smoke-test charms in
test_snapshot.py, it exercises most of what ``--fetch`` (and the rest of
the State fields the snapshot command can gather) is meant to cover in one
single deployment: a workload container with a real Pebble plan, a
filesystem-backed storage mount, a peer relation, a config option, and an
action.

Restricted to Canonical Kubernetes on the latest Ubuntu LTS only (see the
workflow matrix): a single deployment is enough to exercise ``--fetch``,
so there's no need to repeat it across the whole juju/base matrix.
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

APP_NAME = "grafana-k8s"
CONTAINER_NAME = "grafana"
# grafana-k8s mounts its "database" storage at /var/lib/grafana; grafana.ini
# is always present there once the workload has started.
REMOTE_FILE = "/var/lib/grafana/grafana.ini"


@pytest.mark.k8s
@pytest.mark.juju_setup
def test_deploy_fetch_target(juju: jubilant.Juju):
    """Deploy grafana-k8s to snapshot/fetch against."""
    juju.deploy(APP_NAME, channel="2/stable", trust=True)
    juju.wait(jubilant.all_active, timeout=20 * 60)


def _any_unit(juju: jubilant.Juju) -> str:
    status = juju.status()
    return next(iter(status.apps[APP_NAME].units))


@pytest.mark.k8s
def test_snapshot_fetch_json_coverage(juju: jubilant.Juju):
    """A plain `-f json` snapshot should surface the container, its mount,
    the peer relation, and the config option in one shot.
    """
    unit = _any_unit(juju)

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

    containers = {c["name"]: c for c in payload["containers"]}
    assert CONTAINER_NAME in containers
    assert containers[CONTAINER_NAME]["can_connect"] is True

    relation_endpoints = {r["endpoint"] for r in payload["relations"]}
    assert "grafana" in relation_endpoints  # peer relation

    assert "allow_anonymous_access" in payload["config"]


@pytest.mark.k8s
def test_snapshot_fetch_file_from_container(juju: jubilant.Juju, tmp_path):
    """`--fetch` should download a real file out of the workload container's
    mounted storage.
    """
    unit = _any_unit(juju)

    fetch_spec = tmp_path / "fetch.json"
    fetch_spec.write_text(json.dumps({CONTAINER_NAME: [REMOTE_FILE]}))

    output_dir = tmp_path / "snapshot_storage"
    output_dir.mkdir()

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
            "--fetch",
            str(fetch_spec),
            "--output-dir",
            str(output_dir),
            "--devmode",
            "--include-dead-relation-networks",
            "--include-juju-relation-data",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"jhack scenario snapshot --fetch exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    payload = json.loads(result.stdout)
    containers = {c["name"]: c for c in payload["containers"]}
    mounts = containers[CONTAINER_NAME]["mounts"]
    assert mounts, "no mounts were fetched into the container's State"

    # the fetched file should exist on disk, under one of the mount locations.
    fetched_files = list(output_dir.rglob("grafana.ini"))
    assert fetched_files, (
        f"expected to find a fetched grafana.ini under {output_dir}, found nothing.\n"
        f"mounts: {mounts}"
    )
    assert fetched_files[0].stat().st_size > 0
