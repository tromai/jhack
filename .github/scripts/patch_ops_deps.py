#!/usr/bin/env python3
"""Patch jhack's ``ops[testing]`` dependency on the fly (CI only).

jhack declares ``"ops[testing](>=3.0.0)"`` in pyproject.toml. To try jhack against a
different ops / ops-scenario (a released version, or a branch / PR / commit of
canonical/operator) without committing a change, run this script on the CI
checkout *before* installing jhack:

    # released versions (PyPI)
    patch_ops_deps.py --ops-version 3.9.0
    patch_ops_deps.py --ops-version 3.9.0 --ops-scenario-version 8.9.0
    patch_ops_deps.py --ops-version '>=3.9,<4'

    # a git ref of the operator repo (installs both ops and ops-scenario from it)
    patch_ops_deps.py --ops-git-ref main
    patch_ops_deps.py --ops-git-ref myfork/operator@my-branch
    patch_ops_deps.py --ops-git-ref canonical/operator@refs/pull/2000/head

With no option the script does nothing, so callers can invoke it unconditionally
with possibly-empty workflow inputs.

What it does when asked to patch:

* rewrites the ``ops[testing]`` line of ``[project].dependencies`` in place;
* writes ``uv-overrides.txt`` next to pyproject.toml (git mode only; use it with
  ``uv pip install --override uv-overrides.txt ...``) so that ops-scenario's exact
  ``ops==<dev version>`` pin resolves to the git checkout;
* removes ``uv.lock`` (it would pin the old ops);
* prints what it did (and appends it to ``$GITHUB_STEP_SUMMARY`` when set).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DEFAULT_REPO = "canonical/operator"
OPS_DEP_RE = re.compile(r'^(?P<indent>\s*)"ops\[testing\][^"]*",?[ \t]*(#.*)?$', re.MULTILINE)


def normalise_specifier(spec: str) -> str:
    """'3.9.0' -> '==3.9.0'; '>=3.9' stays as is."""
    spec = spec.strip()
    if not spec:
        return ""
    return f"=={spec}" if spec[0].isdigit() else spec


def parse_git_ref(value: str) -> tuple[str, str]:
    """'ref' / 'owner/repo@ref' -> (owner/repo, ref)."""
    value = value.strip()
    if "@" in value:
        repo, ref = value.split("@", 1)
    else:
        repo, ref = DEFAULT_REPO, value
    repo = repo.strip() or DEFAULT_REPO
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise SystemExit(f"invalid repository {repo!r}: expected 'owner/repo'")
    if not ref:
        raise SystemExit("empty git ref")
    return repo, ref


def build_requirements(
    ops_version: str, ops_scenario_version: str, git_ref: str
) -> tuple[list[str], list[str]]:
    """Return (replacement dependency lines, uv override lines)."""
    if git_ref:
        if ops_version or ops_scenario_version:
            raise SystemExit("--ops-git-ref cannot be combined with --ops-*version")
        repo, ref = parse_git_ref(git_ref)
        base = f"git+https://github.com/{repo}@{ref}"
        ops = f"ops @ {base}"
        scenario = f"ops-scenario @ {base}#subdirectory=testing"
        return [ops, scenario], [ops, scenario]

    ops_spec = normalise_specifier(ops_version)
    scenario_spec = normalise_specifier(ops_scenario_version)
    if scenario_spec:
        # Both pinned independently: drop the [testing] extra, which would add its own pin.
        return [f"ops{ops_spec}", f"ops-scenario{scenario_spec}"], []
    return [f"ops[testing]{ops_spec}"], []


def patch_pyproject(text: str, requirements: list[str]) -> str:
    match = OPS_DEP_RE.search(text)
    if not match:
        raise SystemExit('could not find the "ops[testing]..." dependency in pyproject.toml')
    indent = match.group("indent")
    replacement = "\n".join(f'{indent}"{req}",' for req in requirements)
    return text[: match.start()] + replacement + text[match.end() :]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--ops-version", default="", help="ops version or specifier")
    parser.add_argument(
        "--ops-scenario-version",
        default="",
        help="ops-scenario (ops[testing]) version or specifier",
    )
    parser.add_argument(
        "--ops-git-ref",
        default="",
        help="git ref (branch, tag, sha, refs/pull/N/head) of canonical/operator, "
        "or 'owner/repo@ref'; installs ops and ops-scenario from it",
    )
    parser.add_argument("--keep-lock", action="store_true", help="do not delete uv.lock")
    args = parser.parse_args(argv)

    ops_version = args.ops_version.strip()
    scenario_version = args.ops_scenario_version.strip()
    git_ref = args.ops_git_ref.strip()

    if not (ops_version or scenario_version or git_ref):
        print("no ops override requested: pyproject.toml left untouched")
        return 0

    requirements, overrides = build_requirements(ops_version, scenario_version, git_ref)

    pyproject: Path = args.pyproject
    pyproject.write_text(patch_pyproject(pyproject.read_text(), requirements))

    try:
        import tomllib

        deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    except ModuleNotFoundError:  # python < 3.11
        deps = requirements
    except Exception as e:  # pragma: no cover
        raise SystemExit(f"patched pyproject.toml is no longer valid: {e}") from e

    overrides_path = pyproject.parent / "uv-overrides.txt"
    if overrides:
        overrides_path.write_text("\n".join(overrides) + "\n")
    elif overrides_path.exists():
        overrides_path.unlink()

    lock = pyproject.parent / "uv.lock"
    if lock.exists() and not args.keep_lock:
        lock.unlink()

    lines = ["Patched ops dependencies in " + str(pyproject) + ":"]
    lines += [f"  - {r}" for r in requirements]
    if overrides:
        lines.append(f"uv overrides written to {overrides_path}")
    lines.append(f"[project].dependencies now: {deps}")
    print("\n".join(lines))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("### ops dependency override\n\n")
            f.write("\n".join(f"- `{r}`" for r in requirements) + "\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
