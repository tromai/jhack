"""Regression tests for jhack.scenario.snapshot.get_mounts.

See: jhack_snapshot_exercise/JHACK_SNAPSHOT_REPORT.md, issue #1.

``--fetch`` accepts a path to a JSON file containing a
``{container_name: [remote_path, ...]}`` mapping. That file is parsed with
``json.loads``, which yields plain ``str`` objects for each remote path, not
``pathlib.Path``. ``get_mounts`` used to assume its ``fetch_files`` entries
were already ``Path`` instances and called ``.parts`` on them directly,
raising ``AttributeError: 'str' object has no attribute 'parts'`` for any
non-empty fetch spec.
"""
from unittest.mock import patch

from jhack.scenario.snapshot import get_mounts

CONTAINER_META = {
    "mounts": [
        {"storage": "pgdata", "location": "/var/lib/postgresql/data"},
    ],
}


def _fake_fetch_file(**kwargs):
    """Stand-in for jhack.scenario.snapshot.fetch_file: no real juju/ssh calls."""
    return None


@patch("jhack.scenario.snapshot.fetch_file", side_effect=_fake_fetch_file)
def test_get_mounts_accepts_str_paths_from_json(mock_fetch_file, tmp_path):
    """Simulates what json.loads(fetch_spec.read_text()) actually produces:
    plain str remote paths, not pathlib.Path. This used to crash with
    AttributeError: 'str' object has no attribute 'parts'."""
    fetch_files = ["/var/lib/postgresql/data/patroni.yml"]

    mounts = get_mounts(
        target="postgresql-k8s/0",
        model=None,
        container_name="postgresql",
        container_meta=CONTAINER_META,
        fetch_files=fetch_files,
        temp_dir_base_path=tmp_path,
    )

    assert "pgdata" in mounts
    mock_fetch_file.assert_called_once()
    _, call_kwargs = mock_fetch_file.call_args
    assert str(call_kwargs["remote_path"]) == fetch_files[0]


@patch("jhack.scenario.snapshot.fetch_file", side_effect=_fake_fetch_file)
def test_get_mounts_accepts_path_objects(mock_fetch_file, tmp_path):
    """Path objects (e.g. passed programmatically) should keep working too."""
    from pathlib import Path

    fetch_files = [Path("/var/lib/postgresql/data/patroni.yml")]

    mounts = get_mounts(
        target="postgresql-k8s/0",
        model=None,
        container_name="postgresql",
        container_meta=CONTAINER_META,
        fetch_files=fetch_files,
        temp_dir_base_path=tmp_path,
    )

    assert "pgdata" in mounts
    mock_fetch_file.assert_called_once()


@patch("jhack.scenario.snapshot.fetch_file", side_effect=_fake_fetch_file)
def test_get_mounts_empty_fetch_files_is_a_noop(mock_fetch_file, tmp_path):
    """An empty fetch list (e.g. `{"loki": []}`) should not error and should
    not attempt to fetch anything."""
    mounts = get_mounts(
        target="loki/0",
        model=None,
        container_name="loki",
        container_meta=CONTAINER_META,
        fetch_files=[],
        temp_dir_base_path=tmp_path,
    )

    assert mounts == {}
    mock_fetch_file.assert_not_called()
