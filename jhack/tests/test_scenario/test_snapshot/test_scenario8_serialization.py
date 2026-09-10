"""Regression tests for Scenario 8 snapshot serialization."""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from ops import pebble
from scenario import Container, Network, Resource, State
from scenario.state import CheckInfo, Exec, Notice

from jhack.scenario.dict_to_state import dict_to_state
from jhack.scenario.snapshot import get_networks
from jhack.scenario.state_to_dict import state_to_dict
from jhack.scenario.utils import JujuUnitName


def test_get_networks_returns_network_objects_not_dict_keys():
    raw_network = json.dumps(
        {
            "bind-addresses": [
                {
                    "interface-name": "eth0",
                    "addresses": [
                        {"hostname": "", "value": "10.0.0.1", "cidr": "10.0.0.0/24"}
                    ],
                }
            ]
        }
    )
    with patch("jhack.scenario.snapshot._juju_exec", return_value=raw_network):
        networks = get_networks(JujuUnitName("app/0"), None, {})

    assert isinstance(networks, list)
    assert State(networks=networks).networks == frozenset(networks)
    assert all(isinstance(network, Network) for network in networks)


def test_json_serializes_frozenset_nested_dataclasses():
    state = State(containers=[Container("app", can_connect=True, execs={Exec(["echo"])})])

    serialized = state_to_dict(state)

    json.dumps(serialized)
    assert serialized["containers"][0]["execs"][0]["command_prefix"] == ["echo"]


def test_roundtrip_restores_container_scenario8_fields_and_enums():
    notice = Notice(
        id="1",
        user_id=None,
        type=pebble.NoticeType.CUSTOM,
        key="example.com/test",
        occurrences=1,
        first_occurred=datetime(2024, 1, 1),
        last_occurred=datetime(2024, 1, 1),
        last_repeated=datetime(2024, 1, 1),
        repeat_after=timedelta(minutes=1),
        expire_after=None,
    )
    check = CheckInfo(
        name="health",
        level=pebble.CheckLevel.ALIVE,
        startup=pebble.CheckStartup.ENABLED,
        status=pebble.CheckStatus.UP,
    )
    state = State(
        containers=[
            Container(
                "app",
                can_connect=True,
                execs={Exec(["echo", "ok"])},
                notices=[notice],
                check_infos={check},
            )
        ]
    )

    restored = dict_to_state(state_to_dict(state))

    assert restored == state
    assert isinstance(next(iter(restored.containers)).check_infos, frozenset)


def test_roundtrip_restores_resources():
    state = State(resources={Resource(name="image", path="")})

    restored = dict_to_state(state_to_dict(state))

    assert restored == state
