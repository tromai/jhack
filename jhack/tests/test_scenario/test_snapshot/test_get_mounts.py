from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from jhack.helpers import FetchError
from jhack.scenario.utils import JujuUnitName

from jhack.scenario.snapshot import get_mounts


def _fetch_blob(*args, **kwargs):
    local_path = kwargs["local_path"]
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    Path(local_path).write_text("hello world")


@patch("jhack.scenario.snapshot.fetch_blob", new=_fetch_blob)
def test_get_mounts():
    fetch_files = {
        "postgresql": [
            Path("/var/lib/postgresql/data/patroni.yml"),
        ]
    }

    container_meta = {
        "mounts": [
            {"storage": "pgdata", "location": "/var/lib/postgresql/data"},
        ],
    }

    with TemporaryDirectory() as tempdir:
        mounts = get_mounts(
            target=JujuUnitName("postgresql-k8s/0"),
            model="jubilant-95853e18-test-charm",
            container_name="postgresql",
            container_meta=container_meta,
            fetch_files=fetch_files.get("postgresql"),
            temp_dir_base_path=Path(tempdir),
        )

        assert "pgdata" in mounts
        mount = mounts["pgdata"]
        local_file = Path(mount.location) / "var" / "lib" / "postgresql" / "data" / "patroni.yml"
        assert local_file.read_text() == "hello world"


_BINARY_CONTENT = bytes(range(256))


def _fetch_binary_blob(*args, **kwargs):
    local_path = kwargs["local_path"]
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    Path(local_path).write_bytes(_BINARY_CONTENT)


@patch("jhack.scenario.snapshot.fetch_blob", new=_fetch_binary_blob)
def test_get_mounts_binary_file():
    fetch_files = {
        "grafana": [
            Path("/var/lib/grafana/grafana.db"),
        ]
    }

    container_meta = {
        "mounts": [
            {"storage": "database", "location": "/var/lib/grafana"},
        ],
    }

    with TemporaryDirectory() as tempdir:
        mounts = get_mounts(
            target=JujuUnitName("grafana-k8s/0"),
            model="testing",
            container_name="grafana",
            container_meta=container_meta,
            fetch_files=fetch_files.get("grafana"),
            temp_dir_base_path=Path(tempdir),
        )

        assert "database" in mounts
        mount = mounts["database"]
        local_file = Path(mount.location) / "var" / "lib" / "grafana" / "grafana.db"
        assert local_file.read_bytes() == _BINARY_CONTENT
