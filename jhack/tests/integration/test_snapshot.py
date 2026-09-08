"""Live smoke test for `jhack scenario snapshot`, across both substrates.

Scope:
- Smoke tests verify the command exits 0 and produces non-trivial output in
  each supported format (state / json / pytest).
- Runnable-pytest tests go one step further for the ``pytest`` format: they
  take the generated test file, patch out the charm-import and any TODO
  placeholders (replacing them with a minimal stub), then actually execute
  ``pytest`` on it.  This proves that the generated test file is valid Python
  and that the ``State`` it contains can be round-tripped through the
  Scenario test harness.

Machine (LXD) and Canonical Kubernetes legs share the same assertions but
deploy different charms (a real Juju model can only ever be bootstrapped on
one provider at a time, so each leg gets its own `test_deploy_*`/
`test_snapshot_smoke_*`/`test_snapshot_pytest_runnable_*` trio, selected via
``-m machine`` / ``-m k8s``).

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
import re
import subprocess
import sys
import tempfile
import textwrap
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


# ---------------------------------------------------------------------------
# Helpers for the "runnable pytest output" tests
# ---------------------------------------------------------------------------

# Preamble injected at the top of every patched test file.  Provides a minimal
# stub charm so the generated ``from charm import ...`` line (and any
# ``CHARM_TYPE`` placeholder left by jhack) resolves to a real class.
_STUB_CHARM_SRC = textwrap.dedent(
    """\
    import sys, types

    # Build a synthetic ``charm`` module containing a minimal stub so that
    # ``from charm import <anything>`` works regardless of what name jhack
    # picked (or whether it left the TODO placeholder in place).
    _charm_mod = types.ModuleType("charm")

    import ops

    class _StubCharm(ops.CharmBase):
        \"\"\"Minimal stand-in for the real charm class.\"\"\"

    _charm_mod._StubCharm = _StubCharm
    sys.modules["charm"] = _charm_mod
    """
)


def _patch_pytest_output(raw: str, meta: dict) -> str:
    """Patch the raw ``jhack scenario snapshot -f pytest`` output so it can be
    executed by pytest without the real charm source tree, and augment it with
    round-trip assertions.

    All modifications are made here in the integration test; the template in
    snapshot.py is left untouched.  The transformations applied are:

    1. Strip the leading ``#``-comment header (jhack metadata).
    2. Replace the ``from charm import (...)`` block — however black formatted
       it — with a single clean import, and alias any discovered charm name.
    3. Replace the first-argument line of ``Context(...)`` with ``_StubCharm``,
       and inject ``meta=<real metadata dict>`` so Scenario doesn't need a
       charm root on disk.
    4. Replace the ``EVENT_NAME`` placeholder inside ``ctx.run(...)`` with
       ``ctx.on.install()``.
    5. Replace the ``# TODO: add assertions`` comment with concrete round-trip
       assertions that verify the snapshotted state survived the run unchanged
       for all read-only structural fields.
    """
    lines = raw.splitlines(keepends=True)

    # Drop the leading comment-only lines (jhack metadata header).
    code_lines = [ln for ln in lines if not ln.lstrip().startswith("#")]
    code = "".join(code_lines)

    # ------------------------------------------------------------------
    # 1. from charm import ...
    # black may format this as one line or as a parenthesised multi-liner.
    # Capture the first \w+ after "import" — that's the charm class name
    # (or "CHARM_TYPE" if jhack left the placeholder).
    # ------------------------------------------------------------------
    import_match = re.search(r"from charm import\s*\(?\s*(\w+)", code)
    original_charm_name = import_match.group(1) if import_match else None

    # Replace the whole import block (single- or multi-line) with one line.
    code = re.sub(
        r"from charm import\s*\(.*?\)[^\n]*|from charm import[^\n]*",
        "from charm import _StubCharm  # patched by integration test",
        code,
        count=1,
        flags=re.DOTALL,
    )

    # If jhack discovered a real name, alias it so Context(RealName, ...) resolves.
    if original_charm_name and original_charm_name not in ("_StubCharm", "CHARM_TYPE"):
        code = code.replace(
            "from charm import _StubCharm  # patched by integration test",
            "from charm import _StubCharm  # patched by integration test\n"
            f"{original_charm_name} = _StubCharm",
        )

    # ------------------------------------------------------------------
    # 2. Context(...) — replace charm-type arg and inject meta=.
    # Black puts the opening paren on its own line; the charm type is on
    # the next indented line.  Match those two lines and replace with
    # _StubCharm + meta kwarg.
    # ------------------------------------------------------------------
    charm_ref = (
        original_charm_name
        if original_charm_name not in (None, "CHARM_TYPE")
        else "_StubCharm"
    )
    meta_repr = repr(meta)
    def _context_replacement(m: re.Match) -> str:
        indent = m.group(2)
        return f"{m.group(1)}\n{indent}{charm_ref},\n{indent}meta={meta_repr},\n"

    code = re.sub(
        r"(ctx\s*=\s*Context\s*\()\n(\s*)[^\n]+\n",
        _context_replacement,
        code,
        count=1,
    )

    # ------------------------------------------------------------------
    # 3. EVENT_NAME placeholder inside ctx.run(...)
    # ------------------------------------------------------------------
    code = re.sub(
        r"(ctx\.run\(\s*)EVENT_NAME\s*,\s*#[^\n]*",
        r"\1ctx.on.install(),",
        code,
    )

    # ------------------------------------------------------------------
    # 4. Round-trip assertions replacing the TODO comment.
    # These verify that every structural field of the snapshotted State
    # is faithfully round-tripped through the Scenario harness.
    # ------------------------------------------------------------------
    assertions = textwrap.dedent(
        """\
        # Round-trip assertions: verify the snapshotted State survived the run
        # unchanged for all read-only structural fields.
        assert out.model.name == state.model.name
        assert out.model.uuid == state.model.uuid
        assert out.leader == state.leader
        assert out.config == state.config
        assert {r.endpoint for r in out.relations} == {r.endpoint for r in state.relations}
        assert {n.binding_name for n in out.networks} == {n.binding_name for n in state.networks}
        assert {c.name for c in out.containers} == {c.name for c in state.containers}
        assert out.opened_ports == state.opened_ports
        assert out.secrets == state.secrets
        assert out.storages == state.storages
        """
    )
    # Indent to match the function body (4 spaces).
    indented_assertions = textwrap.indent(assertions, "    ")
    code = code.replace(
        "    # TODO: add assertions\n",
        indented_assertions,
    )

    return _STUB_CHARM_SRC + "\n" + code


def _assert_snapshot_pytest_runnable(juju: jubilant.Juju, app_name: str):
    """Capture ``jhack scenario snapshot -f pytest``, patch the output so it
    is runnable without the real charm source, then execute pytest on it and
    assert it exits 0 (i.e. ``test_case`` passes).
    """
    unit = _any_unit(juju, app_name)

    result = _run_snapshot(juju, unit, "-f", "pytest", "--devmode")
    assert result.returncode == 0, (
        f"jhack scenario snapshot exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert result.stdout.strip(), f"no output produced (stderr:\n{result.stderr})"

    # Fetch the real charm metadata.yaml from the unit so the patcher can
    # inject it as ``meta=`` into Context(...), making the generated test
    # self-contained (no charm root needed on disk).
    # The remote path mirrors what jhack's get_metadata() uses internally.
    unit_slug = unit.replace("/", "-")
    meta_path = f"/var/lib/juju/agents/unit-{unit_slug}/charm/metadata.yaml"
    is_k8s = juju.status().model.type == "kubernetes"
    ssh_cmd = ["juju", "ssh", "-m", juju.model]
    if is_k8s:
        ssh_cmd += ["--container", "charm"]
    ssh_cmd += [unit, f"cat {meta_path}"]
    meta_result = subprocess.run(ssh_cmd, capture_output=True, text=True)
    import yaml
    charm_meta = yaml.safe_load(meta_result.stdout) or {}

    patched = _patch_pytest_output(result.stdout, meta=charm_meta)
    logger.debug("patched pytest file:\n%s", patched)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix="_snapshot_test_case.py",
        delete=False,
        prefix="jhack_snapshot_",
    ) as tmp:
        tmp.write(patched)
        tmp_path = tmp.name

    logger.info("running pytest on patched snapshot file: %s", tmp_path)
    pytest_result = subprocess.run(
        [sys.executable, "-m", "pytest", tmp_path, "-v", "--tb=short"],
        capture_output=True,
        text=True,
    )
    assert pytest_result.returncode == 0, (
        f"pytest on the patched snapshot output exited {pytest_result.returncode}.\n"
        f"--- patched file ({tmp_path}) ---\n{patched}\n"
        f"--- pytest stdout ---\n{pytest_result.stdout}\n"
        f"--- pytest stderr ---\n{pytest_result.stderr}"
    )


@pytest.mark.machine
def test_snapshot_pytest_runnable_machine(juju: jubilant.Juju):
    """Verify that the ``-f pytest`` output for the machine leg is a valid,
    executable pytest test (i.e. the generated ``test_case`` passes).
    """
    _assert_snapshot_pytest_runnable(juju, MACHINE_APP_NAME)


@pytest.mark.k8s
def test_snapshot_pytest_runnable_k8s(juju: jubilant.Juju):
    """Verify that the ``-f pytest`` output for the k8s leg is a valid,
    executable pytest test (i.e. the generated ``test_case`` passes).
    """
    _assert_snapshot_pytest_runnable(juju, K8S_APP_NAME)


# ---------------------------------------------------------------------------
# JSON output assertions
# ---------------------------------------------------------------------------


def _assert_snapshot_json(juju: jubilant.Juju, app_name: str):
    """Run ``jhack scenario snapshot -f json``, parse the output, and assert
    that the JSON document is structurally sound and internally consistent.

    These checks verify that jhack correctly serialises every field of the
    live unit's State to JSON -- i.e. that the serialiser hasn't silently
    dropped or mis-typed any field.
    """
    unit = _any_unit(juju, app_name)

    result = _run_snapshot(juju, unit, "-f", "json", "--devmode")
    assert result.returncode == 0, (
        f"jhack scenario snapshot -f json exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    payload = json.loads(result.stdout)
    assert payload, "JSON output was empty"

    # ------------------------------------------------------------------
    # Top-level keys: every field of State must be present.
    # ------------------------------------------------------------------
    expected_keys = {
        "config", "relations", "networks", "containers", "storages",
        "opened_ports", "leader", "model", "secrets", "resources",
        "planned_units", "deferred", "stored_states",
        "app_status", "unit_status", "workload_version",
    }
    assert expected_keys <= payload.keys(), (
        f"missing keys: {expected_keys - payload.keys()}"
    )

    # ------------------------------------------------------------------
    # Type checks: each field must deserialise to the right Python type.
    # ------------------------------------------------------------------
    assert isinstance(payload["config"], dict)
    assert isinstance(payload["relations"], list)
    assert isinstance(payload["networks"], list)
    assert isinstance(payload["containers"], list)
    assert isinstance(payload["storages"], list)
    assert isinstance(payload["opened_ports"], list)
    assert isinstance(payload["leader"], bool)
    assert isinstance(payload["model"], dict)
    assert isinstance(payload["secrets"], list)
    assert isinstance(payload["resources"], list)
    assert isinstance(payload["planned_units"], int)
    assert isinstance(payload["deferred"], list)
    assert isinstance(payload["stored_states"], list)
    assert isinstance(payload["app_status"], dict)
    assert isinstance(payload["unit_status"], dict)
    assert isinstance(payload["workload_version"], str)

    # ------------------------------------------------------------------
    # Model: name and uuid must be non-empty strings; type must match the
    # substrate we're testing against.
    # ------------------------------------------------------------------
    model = payload["model"]
    assert model.get("name"), "model.name is empty"
    assert model.get("uuid"), "model.uuid is empty"
    # Use the type from the JSON payload itself as the reference — jhack reads
    # it from ``juju status`` which returns "kubernetes" or "iaas".  We just
    # verify it is one of the two valid values and is non-empty.
    assert model.get("type") in ("kubernetes", "iaas"), (
        f"unexpected model.type: {model.get('type')!r}"
    )

    # ------------------------------------------------------------------
    # Status: both app_status and unit_status must have a non-empty name.
    # ------------------------------------------------------------------
    assert payload["app_status"].get("name"), "app_status.name is empty"
    assert payload["unit_status"].get("name"), "unit_status.name is empty"

    # ------------------------------------------------------------------
    # Networks: at least the juju-info binding must be present and well-formed.
    # ------------------------------------------------------------------
    assert payload["networks"], "networks list is empty"
    binding_names = {n["binding_name"] for n in payload["networks"]}
    assert "juju-info" in binding_names, (
        f"juju-info binding missing from networks: {binding_names}"
    )
    for net in payload["networks"]:
        assert "bind_addresses" in net
        assert "ingress_addresses" in net
        assert "egress_subnets" in net

    # ------------------------------------------------------------------
    # Containers (k8s only): each container must have a name and can_connect.
    # ------------------------------------------------------------------
    if payload["model"]["type"] == "kubernetes":
        assert payload["containers"], "containers list is empty on k8s"
        for c in payload["containers"]:
            assert c.get("name"), f"container missing name: {c}"
            assert "can_connect" in c

    # ------------------------------------------------------------------
    # Cross-check model name against Juju status.
    # ------------------------------------------------------------------
    assert payload["model"]["name"] == juju.model, (
        f"model name mismatch: JSON has {payload['model']['name']!r}, "
        f"juju model is {juju.model!r}"
    )


@pytest.mark.machine
def test_snapshot_json_machine(juju: jubilant.Juju):
    """Verify the ``-f json`` output for the machine leg is valid and
    structurally sound.
    """
    _assert_snapshot_json(juju, MACHINE_APP_NAME)


@pytest.mark.k8s
def test_snapshot_json_k8s(juju: jubilant.Juju):
    """Verify the ``-f json`` output for the k8s leg is valid and
    structurally sound.
    """
    _assert_snapshot_json(juju, K8S_APP_NAME)
