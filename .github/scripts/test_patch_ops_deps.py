"""Tests for patch_ops_deps.py (run with: pytest .github/scripts)."""

import importlib.util
import sys
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")  # python >= 3.11 (the CI runner uses 3.12)

_spec = importlib.util.spec_from_file_location(
    "patch_ops_deps", Path(__file__).parent / "patch_ops_deps.py"
)
assert _spec and _spec.loader
patch_ops_deps = importlib.util.module_from_spec(_spec)
sys.modules["patch_ops_deps"] = patch_ops_deps
_spec.loader.exec_module(patch_ops_deps)

PYPROJECT = """\
[project]
name = "jhack"
dependencies = [
    "ops[testing](>=3.0.0)",
    "typer(==0.18.0)",
]
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "uv.lock").write_text("lock")
    return tmp_path


def _deps(project: Path) -> list[str]:
    return tomllib.loads((project / "pyproject.toml").read_text())["project"]["dependencies"]


def run(project: Path, *args: str) -> int:
    return patch_ops_deps.main(["--pyproject", str(project / "pyproject.toml"), *args])


def test_noop(project):
    assert run(project) == 0
    assert (project / "pyproject.toml").read_text() == PYPROJECT
    assert (project / "uv.lock").exists()


def test_version(project):
    run(project, "--ops-version", "3.9.0")
    assert _deps(project) == ["ops[testing]==3.9.0", "typer(==0.18.0)"]
    assert not (project / "uv.lock").exists()
    assert not (project / "uv-overrides.txt").exists()


def test_specifier(project):
    run(project, "--ops-version", ">=3.9,<4")
    assert _deps(project)[0] == "ops[testing]>=3.9,<4"


def test_both_versions_drop_the_extra(project):
    run(project, "--ops-version", "3.9.0", "--ops-scenario-version", "8.9.0")
    assert _deps(project) == ["ops==3.9.0", "ops-scenario==8.9.0", "typer(==0.18.0)"]


def test_git_ref_default_repo(project):
    run(project, "--ops-git-ref", "my-branch")
    deps = _deps(project)
    assert deps[:2] == [
        "ops @ git+https://github.com/canonical/operator@my-branch",
        "ops-scenario @ git+https://github.com/canonical/operator@my-branch#subdirectory=testing",
    ]
    assert (project / "uv-overrides.txt").read_text().splitlines() == deps[:2]


def test_git_ref_other_repo_and_pr_ref(project):
    run(project, "--ops-git-ref", "tromai/operator@refs/pull/1/head")
    assert _deps(project)[0] == "ops @ git+https://github.com/tromai/operator@refs/pull/1/head"


def test_git_ref_and_version_conflict(project):
    with pytest.raises(SystemExit):
        run(project, "--ops-git-ref", "main", "--ops-version", "3.9.0")


def test_bad_repo(project):
    with pytest.raises(SystemExit):
        run(project, "--ops-git-ref", "not a repo@main")


def test_keep_lock(project):
    run(project, "--ops-version", "3.9.0", "--keep-lock")
    assert (project / "uv.lock").exists()


def test_stale_overrides_removed(project):
    (project / "uv-overrides.txt").write_text("x\n")
    run(project, "--ops-version", "3.9.0")
    assert not (project / "uv-overrides.txt").exists()


LOCK = """\
version = 1

[[package]]
name = "ops"
version = "3.7.0"

[[package]]
name = "ops-scenario"
version = "8.7.0"

[[package]]
name = "typer"
version = "0.18.0"
"""


def test_ops_version_lock(project):
    (project / "uv.lock").write_text(LOCK)
    run(project, "--ops-version", "lock")
    assert _deps(project)[:2] == ["ops==3.7.0", "ops-scenario==8.7.0"]
    assert not (project / "uv.lock").exists()


def test_ops_version_lock_needs_a_lock(project):
    (project / "uv.lock").unlink()
    with pytest.raises(SystemExit):
        run(project, "--ops-version", "lock")


def test_ops_version_lock_cannot_be_combined(project):
    (project / "uv.lock").write_text(LOCK)
    with pytest.raises(SystemExit):
        run(project, "--ops-version", "lock", "--ops-git-ref", "main")
