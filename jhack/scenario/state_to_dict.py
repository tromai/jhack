#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Facilities to convert State to JSON-compatible dictionaries."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import PurePath
from typing import Any, Dict

from scenario import State

_RELATION_TYPE_NAMES = frozenset({"Relation", "PeerRelation", "SubordinateRelation"})


def _serialize(value: Any) -> Any:
    """Recursively convert State values into JSON-safe types."""
    if is_dataclass(value) and not isinstance(value, type):
        result = {field.name: _serialize(getattr(value, field.name)) for field in fields(value)}
        if type(value).__name__ in _RELATION_TYPE_NAMES:
            result["relation_type"] = type(value).__name__
        return result
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_serialize(item) for item in value]
    return value


def state_to_dict(state: State) -> Dict:
    return {field.name: _serialize(getattr(state, field.name)) for field in fields(state)}
