"""Faster output serialization for Django REST Framework read paths."""

from __future__ import annotations

from typing import Any

from .serializers import (
    FastListSerializer,
    FastModelSerializer,
    FastOutputMixin,
    FastSerializer,
    fast_output,
    strict_equal,
)

__all__ = [
    "FastListSerializer",
    "FastModelSerializer",
    "FastOutputMixin",
    "FastSerializer",
    "explain",
    "fast_output",
    "strict_equal",
]


def explain(serializer: Any) -> str:
    """
    Human-readable per-field plan for a serializer instance: how each field is
    read and converted. Fields on ``get`` / ``generic`` go through DRF itself.
    """
    from . import compiler

    # Mirrors the entry points' checks, including methods assigned on the
    # instance (.data then calls those, not the compiled row).
    usable = compiler.enabled()
    single_auto = isinstance(serializer, FastOutputMixin) and not serializer.fast_output_single
    if hasattr(serializer, "child"):  # many=True
        if isinstance(serializer, FastListSerializer):
            usable = usable and serializer._fast_output_ok
        usable = usable and "to_representation" not in serializer.__dict__
        serializer = serializer.child
    usable = usable and "to_representation" not in serializer.__dict__
    lines: list[str] = [type(serializer).__qualname__]

    def walk(ser: Any, indent: str) -> None:
        # Nested/list conversions are only planned for inlineable serializers.
        for p in compiler.plan(ser):
            target = f" {p.attr}" if p.attr else ""
            lines.append(f"{indent}{p.key}: access={p.access}{target} conv={p.conv}")
            if p.conv in ("nested", "list"):
                walk(getattr(p.field, "child", p.field), indent + "    ")

    if isinstance(serializer, FastOutputMixin):
        usable = usable and serializer._fast_output_row is not None
    else:
        usable = usable and compiler.inlineable(serializer)
    if usable:
        if single_auto:
            lines.append(
                "    (single object: DRF; plan below applies in lists or with fast_output_single=True)"
            )
        walk(serializer, "    ")
    else:
        lines.append("    (not inlined: custom to_representation or opted out)")
    return "\n".join(lines)
