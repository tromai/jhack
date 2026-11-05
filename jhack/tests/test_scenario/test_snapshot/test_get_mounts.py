"""Unit tests for the ``--fetch`` code path of ``jhack scenario snapshot``.

These are the regression tests for canonical/jhack#252:

* ``--fetch`` paths are converted to ``Path`` (used to raise ``AttributeError``);
* files are fetched with ``fetch_blob`` (binary-safe);
* ``logger.exception`` is given a message (used to raise ``TypeError``);
* ``Mount.location`` is the path in the container, ``Mount.source`` the local mirror;
* the output directory defaults to the cwd, is created if missing, and is only
  needed with ``--fetch``.
"""

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, patch

import pytest

from jhack.helpers import FetchError
from jhack.scenario import snapshot as snapshot_module
from jhack.scenario.snapshot import (
    FormatOption,
    _RemoteControllerStorage,
    get_mounts,
    snapshot,
)
from jhack.scenario.utils import JujuUnitName

UNIT = JujuUnitName("foo/0")


def _meta(*mounts: tuple[str, str]) -> dict:
    return {
        "type": "oci-image",
        "mounts": [{"storage": s, "location": loc} for s, loc in mounts],
    }


@pytest.fixture
def fetch_blob():
    """Patch fetch_blob to write the remote path into the local file."""

    def _fetch(*, remote_path, local_path, **_):
        Path(local_path).write_text(str(remote_path))

    with patch("jhack.scenario.snapshot.fetch_blob", side_effect=_fetch) as m:
        yield m


def test_get_mounts_layout(tmp_path, fetch_blob):
    mounts = get_mounts(
        UNIT,
        None,
        "workload",
        _meta(("data", "/var/lib/pg/data")),
        [Path("/var/lib/pg/data/helloworld.txt"), Path("/var/lib/pg/data/sub/dir/f.bin")],
        temp_dir_base_path=tmp_path,
    )

    mount = mounts["data"]
    # location is where the mount lives in the container...
    assert PurePosixPath(mount.location) == PurePosixPath("/var/lib/pg/data")
    # ...and source is a local dir mirroring it, without the mount path repeated inside.
    source = Path(mount.source)
    assert source.parent == tmp_path
    assert (source / "helloworld.txt").read_text() == "/var/lib/pg/data/helloworld.txt"
    assert (source / "sub" / "dir" / "f.bin").exists()
    assert not (source / "var").exists()


def test_get_mounts_passes_blob_args(tmp_path, fetch_blob):
    get_mounts(
        UNIT,
        "mymodel",
        "workload",
        _meta(("data", "/data")),
        [Path("/data/a.txt")],
        temp_dir_base_path=tmp_path,
    )
    kwargs = fetch_blob.call_args.kwargs
    assert kwargs["unit"] == UNIT
    assert kwargs["model"] == "mymodel"
    assert kwargs["container_name"] == "workload"
    assert kwargs["remote_path"] == Path("/data/a.txt")


def test_get_mounts_most_specific_mount_wins(tmp_path, fetch_blob):
    mounts = get_mounts(
        UNIT,
        None,
        "workload",
        _meta(("data", "/data"), ("logs", "/data/logs")),
        [Path("/data/logs/a.log"), Path("/data/b.txt")],
        temp_dir_base_path=tmp_path,
    )
    assert set(mounts) == {"data", "logs"}
    assert (Path(mounts["logs"].source) / "a.log").exists()
    assert (Path(mounts["data"].source) / "b.txt").exists()


def test_get_mounts_does_not_match_path_prefix_siblings(tmp_path, fetch_blob):
    # /datastore is not under the /data mount, even though str.startswith says so.
    mounts = get_mounts(
        UNIT,
        None,
        "workload",
        _meta(("data", "/data")),
        [Path("/datastore/x.txt")],
        temp_dir_base_path=tmp_path,
    )
    assert mounts == {}
    fetch_blob.assert_not_called()


def test_get_mounts_without_mount_metadata(tmp_path, fetch_blob):
    mounts = get_mounts(
        UNIT,
        None,
        "workload",
        {"type": "oci-image"},
        [Path("/data/a.txt")],
        temp_dir_base_path=tmp_path,
    )
    assert mounts == {}
    fetch_blob.assert_not_called()


def test_get_mounts_failed_fetch_logs_a_message_and_continues(tmp_path):
    def _fetch(*, remote_path, local_path, **_):
        if remote_path.name == "missing.txt":
            raise FetchError("no such file")
        Path(local_path).write_text("ok")

    fake_logger = MagicMock()
    with (
        patch("jhack.scenario.snapshot.fetch_blob", side_effect=_fetch),
        patch.object(snapshot_module, "logger", fake_logger),
    ):
        # logger.exception() without a message used to raise a TypeError here.
        mounts = get_mounts(
            UNIT,
            "mymodel",
            "workload",
            _meta(("data", "/data")),
            [Path("/data/missing.txt"), Path("/data/present.txt")],
            temp_dir_base_path=tmp_path,
        )

    fake_logger.exception.assert_called_once()
    (message,) = fake_logger.exception.call_args.args
    for expected in ("/data/missing.txt", "foo/0", "workload", "mymodel"):
        assert expected in message
    # the failure didn't stop the rest of the files from being fetched.
    assert (Path(mounts["data"].source) / "present.txt").read_text() == "ok"


# --- the `snapshot` command ---------------------------------------------------------------


def _call_snapshot(*, fetch=None, output_dir):
    """Call the typer command function with explicit values for every parameter."""
    with patch("jhack.scenario.snapshot._snapshot") as inner:
        snapshot(
            target="foo/0",
            model="m",
            format_=FormatOption.state,
            event_name=None,  # pyright: ignore[reportArgumentType]
            include="rckndtp",
            include_dead_relation_networks=False,
            include_juju_relation_data=False,
            fetch=fetch,
            output_dir=output_dir,
        )
    return inner


def test_snapshot_converts_fetch_paths_to_path(tmp_path):
    spec = tmp_path / "fetch.json"
    spec.write_text(json.dumps({"workload": ["/data/a.txt", "/data/b.bin"], "other": []}))

    inner = _call_snapshot(fetch=spec, output_dir=tmp_path / "out")

    fetch_files = inner.call_args.kwargs["fetch_files"]
    assert fetch_files == {
        "workload": [Path("/data/a.txt"), Path("/data/b.bin")],
        "other": [],
    }
    assert all(isinstance(p, Path) for p in fetch_files["workload"])


def test_snapshot_creates_missing_output_dir_with_fetch(tmp_path):
    spec = tmp_path / "fetch.json"
    spec.write_text("{}")
    out = tmp_path / "does" / "not" / "exist"

    inner = _call_snapshot(fetch=spec, output_dir=out)

    assert out.is_dir()
    assert inner.call_args.kwargs["temp_dir_base_path"] == out


def test_snapshot_rejects_output_dir_that_is_a_file(tmp_path):
    spec = tmp_path / "fetch.json"
    spec.write_text("{}")
    not_a_dir = tmp_path / "afile"
    not_a_dir.touch()

    with pytest.raises(SystemExit) as exc:
        _call_snapshot(fetch=spec, output_dir=not_a_dir)
    assert exc.value.code == 1


def test_snapshot_without_fetch_does_not_touch_output_dir(tmp_path):
    out = tmp_path / "out"
    inner = _call_snapshot(fetch=None, output_dir=out)

    assert not out.exists()
    assert inner.call_args.kwargs["fetch_files"] is None


def test_default_output_dir_is_under_the_cwd_not_its_parent(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from jhack.scenario.snapshot import SNAPSHOT_OUTPUT_DIR; print(SNAPSHOT_OUTPUT_DIR)",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert Path(out).resolve() == (cwd / "snapshot_storage").resolve()


# --- stored state parsing -----------------------------------------------------------------


def test_stored_states_ignore_keys_that_are_not_stored_state_handles():
    storage = _RemoteControllerStorage(None, UNIT)
    state_get = {
        "#notices#": "[]",
        "MyCharm/StoredStateData[_stored]": "{counter: 5}",
        "not-a-handle": "whatever",  # used to crash with AttributeError on None.groups()
    }
    with patch.object(storage, "_state_get", return_value=state_get):
        stored = storage.get_stored_states()

    assert [(s.owner_path, s.name, s.content) for s in stored] == [
        ("MyCharm/StoredStateData", "_stored", {"counter": 5})
    ]
