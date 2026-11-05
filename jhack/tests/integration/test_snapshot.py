"""Live integration tests for `jhack scenario snapshot`, across both substrates.

What is deployed (one Juju model per module, shared by every test below):

* machine leg: ``ubuntu`` plus ``postgresql`` (``16/stable``, base 24.04 only: the charm
  does not publish a 26.04 build, so on a 26.04 matrix leg postgresql is skipped and
  only ``ubuntu`` is exercised);
* k8s leg: ``snappass-test`` plus ``postgresql-k8s`` (``16/stable``).

Every test runs once per way of invoking jhack (see ``conftest.py``): the Python
module installed in the test environment, and -- when JHACK_TEST_SNAP is set -- the
jhack snap. The model is therefore set up once and exercised through both.

Scope:
- Smoke tests verify the command exits 0 and produces non-trivial output in
  each supported format (state / json / pytest).
- Runnable-pytest tests go one step further for the ``pytest`` format: they
  take the generated test file, patch out the charm-import and any TODO
  placeholders (replacing them with a minimal stub), then actually execute
  ``pytest`` on it.  This proves that the generated test file is valid Python
  and that the ``State`` it contains can be round-tripped through the
  Scenario test harness.
- JSON tests check the structure of the serialised State.
- ``--fetch`` / ``--output-dir`` tests (k8s, postgresql-k8s) are the live regression
  tests for canonical/jhack#252: text and binary files are fetched intact from a
  container mount, ``Mount.location`` / ``Mount.source`` have the right meaning, the
  output directory defaults to the cwd and is created on demand, and a failed fetch is
  reported without crashing.

Machine (LXD) and Canonical Kubernetes legs are selected via ``-m machine`` / ``-m k8s``
(a real Juju model can only ever be bootstrapped on one provider at a time).

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

import base64
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from .conftest import JhackRunner

if not os.environ.get("JHACK_RUN_INTEGRATION_TESTS"):
    pytest.skip(
        "set JHACK_RUN_INTEGRATION_TESTS=1 to run these live Juju integration tests",
        allow_module_level=True,
    )

jubilant = pytest.importorskip("jubilant")

logger = logging.getLogger(__name__)

# Charm base to deploy on the machine leg, e.g. "22.04", "24.04", "26.04".
# Defaults to the controller/model's default if unset.
BASE = os.environ.get("JHACK_TEST_BASE")

# postgresql (machine) only publishes builds for these bases (charmhub, stable risk).
POSTGRESQL_MACHINE_CHANNELS = {"22.04": "14/stable", "24.04": "16/stable"}
# When JHACK_TEST_BASE is unset, we deploy postgresql on the oldest base it supports
# that is also the usual default for the Juju versions in the CI matrix.
POSTGRESQL_MACHINE_BASE = BASE or "24.04"
POSTGRESQL_MACHINE_SUPPORTED = POSTGRESQL_MACHINE_BASE in POSTGRESQL_MACHINE_CHANNELS
POSTGRESQL_K8S_CHANNEL = "16/stable"


@dataclass(frozen=True)
class Target:
    """An application to take snapshots of."""

    app: str
    substrate: str  # "machine" | "k8s"

    def __str__(self) -> str:
        return self.app


UBUNTU = Target("ubuntu", "machine")
POSTGRESQL = Target("postgresql", "machine")
SNAPPASS = Target("snappass-test", "k8s")
POSTGRESQL_K8S = Target("postgresql-k8s", "k8s")

_NO_PG_MACHINE = pytest.mark.skipif(
    not POSTGRESQL_MACHINE_SUPPORTED,
    reason=f"postgresql (machine) has no build for base {POSTGRESQL_MACHINE_BASE}",
)

# Every test that takes a ``target`` runs on each of these; ``-m machine`` / ``-m k8s``
# picks the leg.
TARGETS = [
    pytest.param(UBUNTU, id="ubuntu", marks=pytest.mark.machine),
    pytest.param(POSTGRESQL, id="postgresql", marks=[pytest.mark.machine, _NO_PG_MACHINE]),
    pytest.param(SNAPPASS, id="snappass-test", marks=pytest.mark.k8s),
    pytest.param(POSTGRESQL_K8S, id="postgresql-k8s", marks=pytest.mark.k8s),
]


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
    """Deploy ubuntu, and postgresql where the charm supports the matrix base."""
    # Must happen before any `jhack scenario snapshot` call: Juju 4 requires
    # the client's SSH public key to be registered against the model before
    # `juju ssh`/`juju scp` will work (see module docstring).
    _ensure_ssh_key(juju)

    kwargs = {}
    if BASE:
        kwargs["base"] = f"ubuntu@{BASE}"
    juju.deploy(UBUNTU.app, **kwargs)

    if POSTGRESQL_MACHINE_SUPPORTED:
        juju.deploy(
            POSTGRESQL.app,
            channel=POSTGRESQL_MACHINE_CHANNELS[POSTGRESQL_MACHINE_BASE],
            base=f"ubuntu@{POSTGRESQL_MACHINE_BASE}",
        )
    else:
        logger.warning("skipping postgresql: no build for base %s", POSTGRESQL_MACHINE_BASE)

    juju.wait(jubilant.all_active, timeout=40 * 60)


@pytest.mark.k8s
@pytest.mark.juju_setup
def test_deploy_k8s(juju: jubilant.Juju):
    """Deploy snappass-test (a small sidecar charm) and postgresql-k8s."""
    juju.deploy(SNAPPASS.app, trust=True)
    juju.deploy(POSTGRESQL_K8S.app, channel=POSTGRESQL_K8S_CHANNEL, trust=True)
    juju.wait(jubilant.all_active, timeout=40 * 60)


def _any_unit(juju: jubilant.Juju, target: Target) -> str:
    status = juju.status()
    return next(iter(status.apps[target.app].units))


def _run_snapshot(
    jhack: JhackRunner, juju: jubilant.Juju, unit: str, *extra_args: str
) -> subprocess.CompletedProcess:
    return jhack.snapshot(unit, juju.model, *extra_args)


_FULL_STATE_FLAGS = ("--include-dead-relation-networks", "--include-juju-relation-data")


def _assert_snapshot_smoke(jhack: JhackRunner, juju: jubilant.Juju, target: Target, format_: str):
    """Run `jhack scenario snapshot <unit> -f <format_>` and check it succeeded."""
    unit = _any_unit(juju, target)

    result = _run_snapshot(jhack, juju, unit, "-f", format_, *_FULL_STATE_FLAGS)

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


@pytest.mark.parametrize("format_", ["state", "json", "pytest"])
@pytest.mark.parametrize("target", TARGETS)
def test_snapshot_smoke(jhack: JhackRunner, juju: jubilant.Juju, target: Target, format_: str):
    _assert_snapshot_smoke(jhack, juju, target, format_)


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
        original_charm_name if original_charm_name not in (None, "CHARM_TYPE") else "_StubCharm"
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


def _read_charm_metadata(juju: jubilant.Juju, unit: str) -> dict:
    """Read the unit's charm metadata, as jhack's get_metadata() does."""
    import yaml

    unit_slug = unit.replace("/", "-")
    charm_dir = f"/var/lib/juju/agents/unit-{unit_slug}/charm"
    last_error = None
    for name in ("metadata.yaml", "charmcraft.yaml"):
        try:
            return yaml.safe_load(juju.ssh(unit, f"cat {charm_dir}/{name}")) or {}
        except jubilant.CLIError as e:
            last_error = e
    raise AssertionError(f"could not read the charm metadata of {unit}: {last_error}")


def _assert_snapshot_pytest_runnable(jhack: JhackRunner, juju: jubilant.Juju, target: Target):
    """Capture ``jhack scenario snapshot -f pytest``, patch the output so it
    is runnable without the real charm source, then execute pytest on it and
    assert it exits 0 (i.e. ``test_case`` passes).
    """
    unit = _any_unit(juju, target)

    result = _run_snapshot(jhack, juju, unit, "-f", "pytest", *_FULL_STATE_FLAGS)
    assert result.returncode == 0, (
        f"jhack scenario snapshot exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert result.stdout.strip(), f"no output produced (stderr:\n{result.stderr})"

    # Fetch the real charm metadata.yaml from the unit so the patcher can
    # inject it as ``meta=`` into Context(...), making the generated test
    # self-contained (no charm root needed on disk).
    charm_meta = _read_charm_metadata(juju, unit)

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


@pytest.mark.parametrize("target", TARGETS)
def test_snapshot_pytest_runnable(jhack: JhackRunner, juju: jubilant.Juju, target: Target):
    """Verify that the ``-f pytest`` output is a valid, executable pytest test
    (i.e. the generated ``test_case`` passes).
    """
    _assert_snapshot_pytest_runnable(jhack, juju, target)


# ---------------------------------------------------------------------------
# JSON output assertions
# ---------------------------------------------------------------------------


def _assert_snapshot_json(jhack: JhackRunner, juju: jubilant.Juju, target: Target):
    """Run ``jhack scenario snapshot -f json``, parse the output, and assert
    that the JSON document is structurally sound and internally consistent.

    These checks verify that jhack correctly serialises every field of the
    live unit's State to JSON -- i.e. that the serialiser hasn't silently
    dropped or mis-typed any field.
    """
    unit = _any_unit(juju, target)

    result = _run_snapshot(jhack, juju, unit, "-f", "json", *_FULL_STATE_FLAGS)
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
        "config",
        "relations",
        "networks",
        "containers",
        "storages",
        "opened_ports",
        "leader",
        "model",
        "secrets",
        "resources",
        "planned_units",
        "deferred",
        "stored_states",
        "app_status",
        "unit_status",
        "workload_version",
    }
    assert expected_keys <= payload.keys(), f"missing keys: {expected_keys - payload.keys()}"

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
    # Juju returns different type strings depending on the provider:
    # "kubernetes", "iaas", "lxd", etc.  Just assert it's a non-empty string.
    assert model.get("type"), "model.type is empty"

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
        f"model name mismatch: JSON has {payload['model']['name']!r}, juju model is {juju.model!r}"
    )


@pytest.mark.parametrize("target", TARGETS)
def test_snapshot_json(jhack: JhackRunner, juju: jubilant.Juju, target: Target):
    """Verify the ``-f json`` output is valid and structurally sound."""
    _assert_snapshot_json(jhack, juju, target)


# ---------------------------------------------------------------------------
# --fetch / --output-dir: live regression tests for canonical/jhack#252
#
# postgresql-k8s is used because its workload container ("postgresql") has a
# storage mount, which is what `--fetch` can download files from. (Machine charms
# have no containers, so there is nothing to fetch there.)
# ---------------------------------------------------------------------------

PROBE_DIRNAME = ".jhack-snapshot-probe"
PROBE_FILES: dict[str, bytes] = {
    "probe.txt": b"jhack snapshot fetch probe\n",
    # All 256 byte values: not valid UTF-8, so it breaks any text-mode transfer.
    "probe.bin": bytes(range(256)),
}


@dataclass(frozen=True)
class FetchProbe:
    """Files written into a container mount of the unit, to fetch back."""

    unit: str
    container: str
    mount_location: str  # where the mount lives in the container, e.g. /var/lib/x/data
    files: dict[str, bytes]

    @property
    def remote_dir(self) -> str:
        return f"{self.mount_location}/{PROBE_DIRNAME}"

    def remote_path(self, name: str) -> str:
        return f"{self.remote_dir}/{name}"

    def relative_path(self, name: str) -> Path:
        """Where the file sits inside the (local copy of the) mount."""
        return Path(self.remote_path(name)).relative_to(self.mount_location)

    def write_spec(self, directory: Path, extra_paths: tuple[str, ...] = ()) -> Path:
        """Write a --fetch spec for the probe files (plus any extra remote paths)."""
        spec = {self.container: [*(self.remote_path(n) for n in self.files), *extra_paths]}
        path = directory / "fetch.json"
        path.write_text(json.dumps(spec))
        return path


def _find_mounted_container(juju: jubilant.Juju, unit: str) -> tuple[str, str]:
    """Return (container, mount location) of the first container with a storage mount."""
    meta = _read_charm_metadata(juju, unit)
    for container, spec in (meta.get("containers") or {}).items():
        for mount in (spec or {}).get("mounts") or ():
            if mount.get("storage") and mount.get("location"):
                return container, mount["location"].rstrip("/")
    raise AssertionError(f"{unit} has no container with a storage mount: {meta}")


@pytest.fixture(scope="module")
def fetch_probe(juju: jubilant.Juju):
    """Write the probe files into a mounted container of postgresql-k8s (once per module)."""
    unit = _any_unit(juju, POSTGRESQL_K8S)
    container, location = _find_mounted_container(juju, unit)
    probe = FetchProbe(unit, container, location, PROBE_FILES)

    script = f"mkdir -p {shlex.quote(probe.remote_dir)}"
    for name, data in PROBE_FILES.items():
        target = shlex.quote(probe.remote_path(name))
        script += f" && printf %s {base64.b64encode(data).decode()} | base64 -d > {target}"
    juju.ssh(unit, script, container=container)

    yield probe

    try:
        juju.ssh(unit, f"rm -rf {shlex.quote(probe.remote_dir)}", container=container)
    except jubilant.CLIError:
        logger.warning("could not remove the probe files from %s", unit, exc_info=True)


def _assert_ok(result: subprocess.CompletedProcess, what: str = "jhack scenario snapshot"):
    assert result.returncode == 0, (
        f"{what} exited {result.returncode}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The crashes fixed by #252 surface as these, even if the exit code were swallowed.
    for bad in ("Traceback", "AttributeError", "UnicodeDecodeError", "TypeError"):
        assert bad not in result.stderr, f"{bad} in stderr:\n{result.stderr}"


def _fetched_files(output_dir: Path, probe: FetchProbe, name: str) -> list[Path]:
    # jhack stores each mount in <output-dir>/<random tmpdir>/<path relative to the mount>.
    return list(output_dir.glob(f"*/{probe.relative_path(name)}"))


def _assert_probe_fetched(output_dir: Path, probe: FetchProbe):
    for name, expected in probe.files.items():
        matches = _fetched_files(output_dir, probe, name)
        assert len(matches) == 1, f"{name}: expected one fetched copy, got {matches}"
        assert matches[0].read_bytes() == expected, f"{name}: content differs from the original"


@pytest.mark.k8s
def test_snapshot_fetch_text_and_binary_files(
    jhack: JhackRunner, juju: jubilant.Juju, fetch_probe: FetchProbe, workdir: Path
):
    """`--fetch` downloads text *and* binary files byte for byte.

    Regressions: `--fetch` paths were left as `str` (AttributeError: 'str' object has no
    attribute 'parts'), and files were fetched through `juju ssh ... cat` in text mode, which
    corrupted (or crashed on) non-UTF-8 content.
    """
    spec = fetch_probe.write_spec(workdir)
    output_dir = workdir / "out"

    result = jhack.snapshot(
        fetch_probe.unit,
        juju.model,
        "-f",
        "json",
        "--fetch",
        str(spec),
        "--output-dir",
        str(output_dir),
    )
    _assert_ok(result)
    _assert_probe_fetched(output_dir, fetch_probe)


@pytest.mark.k8s
def test_snapshot_fetch_mount_layout_in_state(
    jhack: JhackRunner, juju: jubilant.Juju, fetch_probe: FetchProbe, workdir: Path
):
    """`Mount.location` is the mount's path in the container; `Mount.source` is a local
    directory mirroring it.

    Regression: the two were swapped, and the local copy repeated the whole remote path
    (``<tmpdir>/var/lib/.../probe.txt``) instead of the path relative to the mount.
    """
    spec = fetch_probe.write_spec(workdir)
    output_dir = workdir / "out"

    result = jhack.snapshot(
        fetch_probe.unit,
        juju.model,
        "-f",
        "json",
        "--fetch",
        str(spec),
        "--output-dir",
        str(output_dir),
    )
    _assert_ok(result)

    payload = json.loads(result.stdout)
    container = next(c for c in payload["containers"] if c["name"] == fetch_probe.container)
    mounts = container["mounts"]
    assert len(mounts) == 1, f"expected exactly the mount we fetched from, got {mounts}"
    mount = next(iter(mounts.values()))

    assert mount["location"] == fetch_probe.mount_location
    source = Path(mount["source"])
    assert source.is_dir()
    assert source.parent == output_dir
    for name, expected in fetch_probe.files.items():
        assert (source / fetch_probe.relative_path(name)).read_bytes() == expected
    # the mount path must not be repeated inside the local copy.
    first_component = Path(fetch_probe.mount_location).parts[1]  # e.g. "var"
    assert not (source / first_component).exists()


@pytest.mark.k8s
def test_snapshot_fetch_with_state_format(
    jhack: JhackRunner, juju: jubilant.Juju, fetch_probe: FetchProbe, workdir: Path
):
    """The default (`state`) output format can represent the fetched mounts too."""
    spec = fetch_probe.write_spec(workdir)
    output_dir = workdir / "out"

    result = jhack.snapshot(
        fetch_probe.unit,
        juju.model,
        "-f",
        "state",
        "--fetch",
        str(spec),
        "--output-dir",
        str(output_dir),
    )
    _assert_ok(result)
    assert "Mount(" in result.stdout
    assert fetch_probe.mount_location in result.stdout
    _assert_probe_fetched(output_dir, fetch_probe)


@pytest.mark.k8s
def test_snapshot_fetch_creates_missing_output_dir(
    jhack: JhackRunner, juju: jubilant.Juju, fetch_probe: FetchProbe, workdir: Path
):
    """`--output-dir` is created (with parents) when it doesn't exist."""
    spec = fetch_probe.write_spec(workdir)
    output_dir = workdir / "does" / "not" / "exist" / "yet"
    assert not output_dir.exists()

    result = jhack.snapshot(
        fetch_probe.unit,
        juju.model,
        "-f",
        "json",
        "--fetch",
        str(spec),
        "--output-dir",
        str(output_dir),
    )
    _assert_ok(result)
    _assert_probe_fetched(output_dir, fetch_probe)


@pytest.mark.k8s
def test_snapshot_fetch_default_output_dir_is_under_cwd(
    jhack: JhackRunner, juju: jubilant.Juju, fetch_probe: FetchProbe, workdir: Path
):
    """Without `--output-dir`, fetched files go to ``./snapshot_storage``.

    Regression: the default was ``<cwd's parent>/snapshot_storage``.
    """
    spec = fetch_probe.write_spec(workdir)
    cwd = workdir / "cwd"
    cwd.mkdir()
    parent_storage = cwd.parent / "snapshot_storage"
    before = set(parent_storage.iterdir()) if parent_storage.exists() else set()

    result = jhack.snapshot(
        fetch_probe.unit,
        juju.model,
        "-f",
        "json",
        "--fetch",
        str(spec),
        cwd=cwd,
    )
    _assert_ok(result)

    _assert_probe_fetched(cwd / "snapshot_storage", fetch_probe)
    after = set(parent_storage.iterdir()) if parent_storage.exists() else set()
    assert after == before, f"jhack wrote into the cwd's parent: {after - before}"


@pytest.mark.k8s
def test_snapshot_fetch_rejects_output_dir_that_is_a_file(
    jhack: JhackRunner, juju: jubilant.Juju, fetch_probe: FetchProbe, workdir: Path
):
    spec = fetch_probe.write_spec(workdir)
    not_a_dir = workdir / "afile"
    not_a_dir.write_text("not a directory")

    result = jhack.snapshot(
        fetch_probe.unit,
        juju.model,
        "-f",
        "json",
        "--fetch",
        str(spec),
        "--output-dir",
        str(not_a_dir),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not a directory" in result.stderr
    assert not_a_dir.read_text() == "not a directory"


@pytest.mark.k8s
def test_snapshot_output_dir_not_needed_without_fetch(
    jhack: JhackRunner, juju: jubilant.Juju, workdir: Path
):
    """A plain snapshot doesn't create (or need) an output directory."""
    unit = _any_unit(juju, POSTGRESQL_K8S)

    result = jhack.snapshot(unit, juju.model, "-f", "json", cwd=workdir)
    _assert_ok(result)
    assert not (workdir / "snapshot_storage").exists()


@pytest.mark.k8s
def test_snapshot_fetch_failures_are_reported_not_fatal(
    jhack: JhackRunner, juju: jubilant.Juju, fetch_probe: FetchProbe, workdir: Path
):
    """A file that can't be fetched is logged (with a message naming it), and the
    rest of the snapshot -- including the files that *could* be fetched -- still works.

    Regression: the error handler called ``logger.exception()`` with no message, so a
    failed fetch crashed with ``TypeError`` instead of reporting the real problem.
    """
    missing = f"{fetch_probe.remote_dir}/does-not-exist.txt"
    outside_any_mount = "/etc/hostname"
    spec = fetch_probe.write_spec(workdir, extra_paths=(missing, outside_any_mount))
    output_dir = workdir / "out"

    result = jhack.snapshot(
        fetch_probe.unit,
        juju.model,
        "-f",
        "json",
        "--fetch",
        str(spec),
        "--output-dir",
        str(output_dir),
    )
    _assert_ok(result)
    assert "failed to fetch" in result.stderr
    assert "does-not-exist.txt" in result.stderr
    assert "could not find mount corresponding to requested remote_path" in result.stderr
    _assert_probe_fetched(output_dir, fetch_probe)
