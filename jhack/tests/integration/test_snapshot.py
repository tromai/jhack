"""Live smoke test for `jhack scenario snapshot`, across both substrates.

Scope: smoke test only. We don't assert anything about the *content* of the
snapshot beyond "the command didn't fail and produced non-trivial output that
looks like the requested format".

Machine (LXD) and Canonical Kubernetes legs share the same assertions but
deploy different charms (a real Juju model can only ever be bootstrapped on
one provider at a time, so each leg gets its own `test_deploy_*`/
`test_snapshot_smoke_*` pair, selected via `-m machine` / `-m k8s`).

Note: snappass-test (the k8s smoke charm) only publishes a single base to
Charmhub, so the JHACK_TEST_BASE env var (used to vary the machine leg's charm
base) has no effect on the k8s leg; it's kept for CI matrix symmetry.

These tests talk to a real Juju controller/model (bootstrapped out-of-band,
e.g. via Concierge) and shell out to the actual `jhack` CLI, so they're opt-in:
they're skipped at collection time unless JHACK_RUN_INTEGRATION_TESTS is set,
so `pytest jhack/tests` (the unit test entrypoint) never tries to run them
against a real cloud. The `machine`/`k8s` markers below are registered in
pyproject.toml's [tool.pytest.ini_options].

SSH note (machine leg only): as of Juju 4, `juju ssh`/`juju scp` require the
client's SSH public key to be registered against the model with
`juju add-ssh-key` first -- Juju no longer does this automatically. `jhack
scenario snapshot` on machine units shells out to both (`get_metadata`,
`RemotePebbleClient`, `fetch_blob`/`fetch_file`), so without a registered key
every invocation fails with `JujuSSHError: SSH access failed (exit code
255)`. `_ensure_ssh_key` below generates a throwaway keypair (if one isn't
already present) and registers it via `juju.cli("add-ssh-key", ...)` before
the machine-leg tests run. The k8s leg is unaffected: `juju ssh --container`
against a k8s unit is proxied through the k8s API, not real SSH.
"""

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

if not os.environ.get("JHACK_RUN_INTEGRATION_TESTS"):
    pytest.skip(
        "set JHACK_RUN_INTEGRATION_TESTS=1 to run these live Juju integration tests",
        allow_module_level=True,
    )

jubilant = pytest.importorskip("jubilant")

logger = logging.getLogger(__name__)

MACHINE_APP_NAME = "ubuntu"
K8S_APP_NAME = "snappass-test"

# Charm base to deploy on the machine leg, e.g. "22.04", "24.04", "26.04".
# Defaults to the controller/model's default if unset.
BASE = os.environ.get("JHACK_TEST_BASE")


def _ensure_ssh_key(juju: jubilant.Juju):
    """Ensure the current user's SSH public key is registered with the model.

    Juju 4 requires `juju add-ssh-key` before `juju ssh`/`juju scp` will work;
    it's no longer done implicitly. Generates a throwaway keypair if the user
    (or CI runner) doesn't already have one.

    Unlike jubilant's own `tests/integration/test_machine.py` (which generates
    a per-test-module ephemeral keypair and passes it explicitly via
    `ssh_options=['-i', <path>]` to `juju.ssh`/`juju.scp`), we can't do that
    here: `jhack scenario snapshot` shells out to `juju ssh`/`juju scp`
    internally (`_juju_ssh`, `RemotePebbleClient`, `fetch_blob`/`fetch_file`)
    with no way to pass a custom identity file. So we must rely on OpenSSH's
    default identity discovery, which means writing (or reusing) a real
    default-location keypair at ~/.ssh/id_ed25519 -- this persists on the
    host/runner rather than being cleaned up per-test.

    Note: `juju add-ssh-key` exits 0 even if the key is already registered
    (it just logs "... already exist" to stderr and continues), so no
    duplicate-key error handling is needed here.
    """
    ssh_dir = Path.home() / ".ssh"
    pub_key_path = ssh_dir / "id_ed25519.pub"
    priv_key_path = ssh_dir / "id_ed25519"

    if not pub_key_path.exists():
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        logger.info("no SSH keypair found at %s; generating one", pub_key_path)
        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(priv_key_path),
            ],
            check=True,
            capture_output=True,
        )

    pub_key = pub_key_path.read_text().strip()
    juju.add_ssh_key(pub_key)


@pytest.mark.machine
@pytest.mark.juju_setup
def test_deploy_machine(juju: jubilant.Juju):
    """Deploy a plain ubuntu machine charm to snapshot against."""
    # Must happen before any `jhack scenario snapshot` call: Juju 4 requires
    # the client's SSH public key to be registered against the model before
    # `juju ssh`/`juju scp` will work (see module docstring).
    _ensure_ssh_key(juju)

    kwargs = {}
    if BASE:
        kwargs["base"] = f"ubuntu@{BASE}"
    juju.deploy(MACHINE_APP_NAME, **kwargs)
    juju.wait(jubilant.all_active, timeout=20 * 60)


@pytest.mark.k8s
@pytest.mark.juju_setup
def test_deploy_k8s(juju: jubilant.Juju):
    """Deploy snappass-test (a small k8s sidecar charm) to snapshot against."""
    juju.deploy(K8S_APP_NAME, trust=True)
    juju.wait(jubilant.all_active, timeout=20 * 60)


def _run_snapshot(juju: jubilant.Juju, unit: str, *extra_args: str) -> subprocess.CompletedProcess:
    cmd = ["jhack", "scenario", "snapshot", unit, "-m", juju.model, *extra_args]
    logger.info("running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def _any_unit(juju: jubilant.Juju, app_name: str) -> str:
    status = juju.status()
    return next(iter(status.apps[app_name].units))


def _assert_snapshot_smoke(juju: jubilant.Juju, app_name: str, format_: str):
    """Run `jhack scenario snapshot <unit> -f <format_>` and check it succeeded."""
    unit = _any_unit(juju, app_name)

    result = _run_snapshot(juju, unit, "-f", format_, "--devmode")

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


@pytest.mark.machine
@pytest.mark.parametrize("format_", ["state", "json", "pytest"])
def test_snapshot_smoke_machine(juju: jubilant.Juju, format_: str):
    _assert_snapshot_smoke(juju, MACHINE_APP_NAME, format_)


@pytest.mark.k8s
@pytest.mark.parametrize("format_", ["state", "json", "pytest"])
def test_snapshot_smoke_k8s(juju: jubilant.Juju, format_: str):
    _assert_snapshot_smoke(juju, K8S_APP_NAME, format_)
