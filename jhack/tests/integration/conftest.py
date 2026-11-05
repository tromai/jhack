"""Shared fixtures for the live `jhack scenario snapshot` integration tests.

Everything here is stdlib-only so that collecting this directory (e.g. by
``pytest jhack/tests``) works without the ``integration`` extras installed; the
tests themselves skip unless JHACK_RUN_INTEGRATION_TESTS is set.

The ``jhack`` fixture runs every test once per way of invoking jhack:

``module``
    the jhack installed in the Python environment running pytest
    (``python -m jhack.main``). Always available.
``snap``
    the jhack snap (``/snap/bin/jhack``). Only parametrized in when
    JHACK_TEST_SNAP is set -- CI installs the freshly built snap and sets it. All
    tests of a module share one Juju model (pytest-jubilant's ``juju`` fixture is
    module-scoped), so the model is set up once and exercised through both.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

SNAP_BIN = Path("/snap/bin/jhack")


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


RUNNERS = ["module"] + (["snap"] if _truthy("JHACK_TEST_SNAP") else [])


@dataclass(frozen=True)
class JhackRunner:
    """A way of invoking the jhack CLI."""

    name: str
    argv: tuple[str, ...]

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        timeout: float = 15 * 60,
    ) -> subprocess.CompletedProcess:
        cmd = [*self.argv, *args]
        logger.info("[%s] running: %s (cwd=%s)", self.name, " ".join(cmd), cwd or os.getcwd())
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)

    def snapshot(
        self,
        unit: str,
        model: str,
        *args: str,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        """Run ``jhack scenario snapshot <unit> -m <model> ... --devmode``."""
        return self.run("scenario", "snapshot", unit, "-m", model, *args, "--devmode", cwd=cwd)


@pytest.fixture(params=RUNNERS)
def jhack(request: pytest.FixtureRequest) -> JhackRunner:
    if request.param == "snap":
        if not SNAP_BIN.exists():
            pytest.fail(f"JHACK_TEST_SNAP is set but {SNAP_BIN} does not exist; install the snap")
        return JhackRunner("snap", (str(SNAP_BIN),))
    return JhackRunner("module", (sys.executable, "-m", "jhack.main"))


@pytest.fixture
def workdir() -> Iterator[Path]:
    """A scratch directory jhack can read and write in every mode.

    It lives directly under $HOME and is not hidden: the snap is strictly confined
    and can neither see /tmp (where pytest's ``tmp_path`` lives) nor dot-directories
    in $HOME.
    """
    path = Path(tempfile.mkdtemp(prefix="jhack-it-", dir=Path.home()))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
