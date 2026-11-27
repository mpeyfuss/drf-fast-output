"""
Settings, read from ``settings.DRF_FAST_OUTPUT`` (a dict)::

    DRF_FAST_OUTPUT = {
        # Master switch. False makes every fast serializer behave exactly like
        # the stock DRF one.
        "ENABLED": True,
        # Compute both the fast and the stock DRF output on every call and raise
        # AssertionError if they differ. Meant for test settings.
        "VERIFY": False,
    }
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.signals import setting_changed

SETTING_NAME = "DRF_FAST_OUTPUT"

DEFAULTS: dict[str, Any] = {
    "ENABLED": True,
    "VERIFY": False,
}

_cache: dict[str, Any] | None = None


def get(name: str) -> Any:
    global _cache
    if _cache is None:
        user = getattr(settings, SETTING_NAME, None) or {}
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown {SETTING_NAME} keys: {sorted(unknown)}")
        _cache = {**DEFAULTS, **user}
    return _cache[name]


def _reset(*, setting: str, **kwargs: Any) -> None:
    global _cache
    if setting == SETTING_NAME:
        _cache = None


setting_changed.connect(_reset)
