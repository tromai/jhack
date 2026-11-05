# Live integration tests for `jhack scenario snapshot`

These tests run against a real Juju controller and are skipped unless
`JHACK_RUN_INTEGRATION_TESTS=1` is set.

## Running locally

```bash
uv venv && uv pip install -e . -r jhack/tests/integration/requirements.txt

# bootstrap a controller first, e.g. `sudo concierge prepare --preset k8s` (or `machine`)
JHACK_RUN_INTEGRATION_TESTS=1 .venv/bin/python -m pytest jhack/tests/integration -m k8s -v
#                                                                                     ^ or `-m machine`
```

Environment variables:

| Variable | Effect |
|----------|--------|
| `JHACK_RUN_INTEGRATION_TESTS` | Required to run anything. |
| `JHACK_TEST_BASE` | Base of the machine-leg charms (`24.04`, `26.04`, ...). `postgresql` has no 26.04 build, so it is skipped for that base. |
| `JHACK_TEST_SNAP` | Also run every test through the installed jhack snap (`/snap/bin/jhack`). Install it first: `sudo snap install --dangerous jhack_*.snap` and connect `dot-local-share-juju`, `dot-config-jhack` and `ssh-read`. |

All tests of `test_snapshot.py` share a single Juju model (it is deployed once by the
`test_deploy_*` tests) and are run once per jhack "runner" (`module`, and `snap` when
enabled).

## Running in CI

The `jhack scenario snapshot - live integration tests` workflow (Actions tab -> Run
workflow) takes:

* `target-branch`: the jhack branch to test. The tests themselves always come from the
  branch the workflow runs on.
* `ops-version`, `ops-scenario-version`: install a specific `ops` / `ops-scenario`
  release (`3.9.0`, `>=3.9,<4`, ...).
* `ops-git-ref`: install `ops` and `ops-scenario` from a git ref of the operator repo
  (`main`, `my-branch`, `myfork/operator@my-branch`, `canonical/operator@refs/pull/N/head`).
  Overrides the two inputs above.
* `run-snap-tests`: build the snap and run the tests through it too (Juju 3.6 legs only).

The dependency override is done by `.github/scripts/patch_ops_deps.py`, which you can also
run by hand on a checkout.
