"""Helpers for declaring settings and (de)serialising them.

Each subsystem declares its settings as a dataclass in its own ``config.py``
using ``setting(...)``.  The metadata drives the generated settings window,
so every user-facing field needs a plain-English ``label`` and, where it
helps, a ``help`` line.  Annotations must be importable at runtime (no
``TYPE_CHECKING``-only imports) because loading uses ``typing.get_type_hints``.

The user's own JSON files (``settings.json``, ``pauses.json``) are read with
``loads_strict``: anyone can edit them, and a value Python's json module
accepts but Sightline cannot use (NaN, Infinity, ``1e999``, brackets nested
thousands deep) must make the file count as damaged, never stop the app
starting.
"""

from __future__ import annotations

import copy
import dataclasses
import enum
import json
import logging
import math
import re
import types
import typing
from dataclasses import MISSING, field
from pathlib import Path
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

MAX_JSON_DEPTH = 32  # settings nest three levels; anything near Python's recursion limit is damage
_JSON_TOKENS = re.compile(r'"(?:[^"\\]|\\.)*"|[\[\]{}]', re.DOTALL)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a number Sightline can use")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text[:20]} is too large a number")
    return value


def loads_strict(text: str, *, max_depth: int = MAX_JSON_DEPTH) -> Any:
    """Parse JSON from a file a person or another program may have edited.

    Raises ValueError for NaN, Infinity and numbers too large for a float
    (``1e999``), and for nesting deeper than ``max_depth``, besides the usual
    syntax errors.  Callers treat any of them as a damaged file.
    """
    depth = 0
    for token in _JSON_TOKENS.finditer(text):
        ch = token.group()
        if ch in "[{":
            depth += 1
            if depth > max_depth:
                raise ValueError(f"nested more than {max_depth} levels deep")
        elif ch in "]}":
            depth -= 1
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)


def setting(
    default: Any,
    *,
    label: str,
    help: str = "",
    group: str = "",
    minimum: float | None = None,
    maximum: float | None = None,
    step: float | None = None,
    unit: str = "",
    choices: dict[str, str] | None = None,
    advanced: bool = False,
    restart: bool = False,
    hidden: bool = False,
    readonly: bool = False,
    protected: bool = False,
) -> Any:
    """Declare a settings field.

    ``choices`` maps stored value to the label shown to the user.  ``hidden``
    fields are persisted but never shown (calibration flags and the like).
    ``restart`` marks fields that only take effect after a restart.
    ``readonly`` fields are values Sightline measures and shows but nobody
    sets (what it has learned about the user); the settings window shows
    them as text with nothing to change.
    ``protected`` fields keep the user safe from sound that is not theirs
    (the countdown, the attention gate): a voice command may turn one on but
    never off or weaker; the settings window asks for a blink or dwell instead.
    """
    meta = {
        "label": label,
        "help": help,
        "group": group,
        "minimum": minimum,
        "maximum": maximum,
        "step": step,
        "unit": unit,
        "choices": choices,
        "advanced": advanced,
        "restart": restart,
        "hidden": hidden,
        "readonly": readonly,
        "protected": protected,
    }
    if isinstance(default, (list, dict, set)):
        snapshot = copy.deepcopy(default)
        return field(default_factory=lambda: copy.deepcopy(snapshot), metadata=meta)
    return field(default=default, metadata=meta)


def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    return obj


def from_jsonable(cls: type[T], data: Any) -> T:
    """Build a settings dataclass from parsed JSON, keeping defaults for anything missing or invalid."""
    if not isinstance(data, dict):
        return cls()
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name not in data:
            continue
        default = _default_of(f)
        try:
            value = _coerce(hints.get(f.name, Any), data[f.name], default)
        except (TypeError, ValueError, OverflowError, RecursionError) as e:
            log.warning("Setting %s.%s ignored (%s); using the default", cls.__name__, f.name, e)
            continue
        value = _clamp(value, f.metadata)
        kwargs[f.name] = value
    return cls(**kwargs)


def _default_of(f: dataclasses.Field) -> Any:
    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return None


def _clamp(value: Any, meta: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    lo = meta.get("minimum") if meta else None
    hi = meta.get("maximum") if meta else None
    if lo is not None and value < lo:
        value = type(value)(lo)
    if hi is not None and value > hi:
        value = type(value)(hi)
    return value


def _coerce(hint: Any, raw: Any, default: Any) -> Any:
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)

    if hint is Any:
        return raw
    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        if raw is None and len(non_none) != len(args):
            return None
        for option in non_none:
            try:
                return _coerce(option, raw, default)
            except (TypeError, ValueError):
                continue
        raise ValueError(f"{raw!r} matches none of {hint}")
    if origin is typing.Literal:
        if raw in args:
            return raw
        raise ValueError(f"{raw!r} is not one of {args}")
    if dataclasses.is_dataclass(hint):
        return from_jsonable(hint, raw)
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return hint(raw)
    if hint is bool:
        if isinstance(raw, bool):
            return raw
        raise TypeError(f"expected true or false, got {raw!r}")
    if hint is int:
        if isinstance(raw, bool):
            raise TypeError("expected a number")
        if isinstance(raw, float) and not math.isfinite(raw):
            raise ValueError("expected a finite number")
        return int(raw)
    if hint is float:
        if isinstance(raw, bool):
            raise TypeError("expected a number")
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("expected a finite number")
        return value
    if hint is str:
        if not isinstance(raw, str):
            raise TypeError("expected text")
        return raw
    if origin in (list, tuple, set):
        if not isinstance(raw, list):
            raise TypeError("expected a list")
        item_hint = args[0] if args else Any
        items = [_coerce(item_hint, v, None) for v in raw]
        return origin(items) if origin is not list else items
    if origin is dict:
        if not isinstance(raw, dict):
            raise TypeError("expected a mapping")
        key_hint, val_hint = (args + (Any, Any))[:2] if args else (Any, Any)
        return {_coerce(key_hint, k, None): _coerce(val_hint, v, None) for k, v in raw.items()}
    return raw
