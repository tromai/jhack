# Single source of truth for what the lint / format / type-check / unit-test
# pipeline covers: the `jhack scenario snapshot` module and the unit tests that
# import from it. Sourced by the justfile and .github/workflows/snapshot-ci.yaml.
#
# Add a test file here when it starts importing from jhack.scenario.snapshot.

# shellcheck shell=bash
SNAPSHOT_SRC=(
    jhack/scenario/snapshot.py
)
SNAPSHOT_TESTS=(
    jhack/tests/test_scenario/test_snapshot/test_fs_push_pull.py
    jhack/tests/test_scenario/test_snapshot/test_get_mounts.py
)
