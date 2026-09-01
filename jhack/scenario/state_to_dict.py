#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Facilities to convert State to json."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import PurePath
from typing import Any, Dict

from scenario import State

# dataclasses that also need a "relation_type" tag so dict_to_state can tell
# a plain Relation from a PeerRelation/SubordinateRelation on the way back in.
_RELATION_TYPE_NAMES = frozenset({"Relation", "PeerRelation", "SubordinateRelation"})


def _serialize(value: Any) -> Any:
    """Recursively convert a State's value tree into JSON-safe types.

    NB: `dataclasses.asdict()` does *not* recurse into `set`/`frozenset`-typed
    fields (e.g. `Container.execs`, `Container.check_infos`) -- it leaves them
    as (deep-copied) frozensets, potentially still containing raw nested
    dataclass instances. That's exactly what used to blow up `json.dumps()`
    with e.g. `TypeError: Object of type frozenset is not JSON serializable`.
    So we walk the tree ourselves instead of relying on `asdict()`.
    """
    if is_dataclass(value) and not isinstance(value, type):
        dct = {f.name: _serialize(getattr(value, f.name)) for f in fields(value)}
        if type(value).__name__ in _RELATION_TYPE_NAMES:
            dct["relation_type"] = type(value).__name__
        return dct
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, Mapping):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_serialize(v) for v in value]
    return value


def state_to_dict(state: State) -> Dict:
    return {f.name: _serialize(getattr(state, f.name)) for f in fields(state)}
