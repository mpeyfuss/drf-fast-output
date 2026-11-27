from __future__ import annotations

import functools
import itertools
import threading
from collections.abc import Callable
from decimal import Decimal
from typing import Any, TypeVar

from django.db.models.manager import BaseManager
from django.db.models.query import QuerySet
from rest_framework import serializers as s

from . import compiler, conf
from .compiler import DT_OTHER, datetime_mode, force_drf, in_fast_call, passthrough, stock_drf

__all__ = [
    "FastListSerializer",
    "FastModelSerializer",
    "FastOutputMixin",
    "FastSerializer",
    "fast_output",
    "strict_equal",
]

_FAST_CLASS_ATTR = "_fast_output_class"


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def strict_equal(a: Any, b: Any) -> bool:
    """
    Equality that also requires identical types (containers included, so
    ``dict`` vs ``OrderedDict`` differ) and dict key order, so that
    ``1``/``True``/``1.0`` or ``{"a", "b"}`` vs ``{"b", "a"}`` are told apart
    (they render to different JSON). NaN equals NaN: both render the same.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return (
            list(a) == list(b)
            and all(type(x) is type(y) for x, y in zip(a, b, strict=True))  # {True: v} vs {1: v}
            and all(strict_equal(a[k], b[k]) for k in a)
        )
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(map(strict_equal, a, b))
    if isinstance(a, Decimal) and (a.is_nan() or b.is_nan()):
        return str(a) == str(b)  # `==` is never true for NaN, and raises for sNaN
    if a is b or a == b:
        return True
    return isinstance(a, float) and a != a and b != b


def _verify(fast: Any, compute_drf: Callable[[], Any], label: str) -> None:
    with stock_drf():
        expected = compute_drf()
    if not strict_equal(fast, expected):
        raise AssertionError(
            f"drf-fast-output: output differs from DRF for {label}:\n  fast: {fast!r}\n  drf:  {expected!r}"
        )


# --------------------------------------------------------------------------
# Serializers
# --------------------------------------------------------------------------


class FastOutputMixin:
    """
    Mix into a ``Serializer``/``ModelSerializer`` *before* the DRF base::

        class BookSerializer(FastOutputMixin, serializers.ModelSerializer):
            ...

    Only output (``.data`` / ``to_representation``) changes; validation and
    saving are stock DRF. Opt a serializer out with ``Meta.fast_output = False``.
    """

    fast_output_single = False

    def get_fields(self) -> dict:
        fields = super().get_fields()  # type: ignore[misc]
        # Adapt only this instance's copied fields. Keep nested collections fast
        # even when their parent takes the single-object DRF path. Nested single
        # serializers propagate this lazily when their own fields are built.
        if compiler.opted_in(self):
            for field in fields.values():
                if type(field) is s.ListSerializer:
                    field.__class__ = FastListSerializer
                elif isinstance(field, s.Serializer) and not isinstance(field, FastOutputMixin):
                    field.__class__ = fast_output(type(field))
        return fields

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        mro = cls.__mro__
        if s.BaseSerializer in mro and mro.index(FastOutputMixin) > mro.index(s.BaseSerializer):
            raise TypeError(
                f"{cls.__qualname__}: FastOutputMixin must come before the DRF "
                f"serializer base class, e.g. "
                f"`class {cls.__name__}(FastOutputMixin, serializers.ModelSerializer)`."
            )

    @functools.cached_property
    def _fast_output_row(self) -> compiler.Row | None:
        # Overrides above the mixin call super() and still reach it.
        ok = compiler.opted_in(self) and compiler.drf_impl(
            type(self), "to_representation", s.Serializer, after=FastOutputMixin
        )
        return compiler.get_row(self) if ok else None

    @passthrough
    def to_representation(self, instance: Any) -> Any:
        if force_drf.get() or not compiler.enabled():
            return super().to_representation(instance)  # type: ignore[misc]
        nested = in_fast_call.get()
        if not (self.fast_output_single or nested or _custom_list_child(self)):
            return super().to_representation(instance)  # type: ignore[misc]
        row = self._fast_output_row
        if row is None:
            return super().to_representation(instance)  # type: ignore[misc]
        dtm = datetime_mode() if row.needs_dtm else DT_OTHER
        if nested:
            out = row(instance, dtm)
        else:
            token = in_fast_call.set(True)
            try:
                out = row(instance, dtm)
            finally:
                in_fast_call.reset(token)
        if conf.get("VERIFY"):
            _verify(
                out,
                lambda: super(FastOutputMixin, self).to_representation(instance),  # type: ignore[misc]
                type(self).__qualname__,
            )
        return out

    @classmethod
    def many_init(cls, *args: Any, **kwargs: Any) -> s.ListSerializer:
        ls = super().many_init(*args, **kwargs)  # type: ignore[misc]
        # Only swap the stock ListSerializer; a custom list_serializer_class is
        # left alone (its children are still fast).
        if type(ls) is s.ListSerializer:
            ls.__class__ = FastListSerializer
        return ls


def _custom_list_child(serializer: Any) -> bool:
    # A row of a list serializer we don't swap (a custom list_serializer_class,
    # or a hand-built ListSerializer(child=...)): its loop calls the child per
    # row, so compile like a list would. FastListSerializer's own small-list
    # fallback to DRF is deliberate and stays uncompiled.
    parent = serializer.parent
    return isinstance(parent, s.ListSerializer) and not isinstance(parent, FastListSerializer)


_EMPTY = object()


class FastListSerializer(s.ListSerializer):
    """ListSerializer that runs the child's compiled row in a single loop."""

    @functools.cached_property
    def _fast_output_ok(self) -> bool:
        # Overrides above this class call super() and still reach it; a patched
        # ListSerializer.to_representation is honoured.
        return compiler.inlineable(self.child) and compiler.drf_impl(
            type(self), "to_representation", s.ListSerializer, after=FastListSerializer
        )

    @passthrough
    def to_representation(self, data: Any) -> list:
        if force_drf.get() or not compiler.enabled():
            return super().to_representation(data)
        if in_fast_call.get():
            return self._fast_representation(data)
        if self.parent is not None and not getattr(self.child, "fast_output_single", False):
            # A detail response's tiny nested collections have the same setup
            # problem as a single object. Only inspect already-materialized data;
            # never count a queryset or consume a generator to choose a path.
            data = data.all() if isinstance(data, BaseManager) else data
            cached = data._result_cache if isinstance(data, QuerySet) else data
            if isinstance(cached, (list, tuple)) and len(cached) < 10:
                return super().to_representation(data)
        # The outermost fast call: children that aren't inlineable (DRF's loop
        # calls them row by row) compile too.
        token = in_fast_call.set(True)
        try:
            return self._fast_representation(data)
        finally:
            in_fast_call.reset(token)

    def _fast_representation(self, data: Any) -> list:
        if not self._fast_output_ok:
            return super().to_representation(data)
        it = iter(data.all() if isinstance(data, BaseManager) else data)
        first = next(it, _EMPTY)
        if first is _EMPTY:
            return []  # like DRF, without building the child's fields
        child: Any = self.child
        row = compiler.get_row(child)  # compiled on the first row, then cached on the child
        dtm = datetime_mode() if row.needs_dtm else DT_OTHER
        items = itertools.chain((first,), it)
        if not conf.get("VERIFY"):
            return [row(o, dtm) for o in items]
        # Row by row, as each item is consumed, like DRF's own loop does.
        label = type(child).__qualname__
        out = []
        for i, o in enumerate(items):
            fast = row(o, dtm)
            _verify(fast, lambda o=o: child.to_representation(o), f"{label}(many=True)[{i}]")
            out.append(fast)
        return out


class FastSerializer(FastOutputMixin, s.Serializer):
    pass


class FastModelSerializer(FastOutputMixin, s.ModelSerializer):
    pass


# --------------------------------------------------------------------------
# Opting existing serializers in without touching them
# --------------------------------------------------------------------------

_S = TypeVar("_S", bound=type)
_fast_classes_lock = threading.Lock()


def fast_output(serializer_class: _S) -> _S:
    """
    Return a subclass of ``serializer_class`` with fast output, keeping its
    name, fields and Meta. The result is cached, so this is cheap to call per
    request::

        class BookList(generics.ListAPIView):
            serializer_class = fast_output(BookSerializer)
    """
    if issubclass(serializer_class, FastOutputMixin):
        return serializer_class
    if not issubclass(serializer_class, s.BaseSerializer):
        raise TypeError(f"{serializer_class!r} is not a DRF serializer class")
    # Cached on the class itself, so it goes away with it.
    cls = serializer_class.__dict__.get(_FAST_CLASS_ATTR)
    if cls is None:
        # Built outside the lock: creating the class runs user metaclass and
        # __init_subclass__ hooks, which may call fast_output() themselves. A
        # race builds a spare class that is thrown away; the first one stored wins.
        new = type(serializer_class)(
            serializer_class.__name__,
            (FastOutputMixin, serializer_class),
            {
                "__module__": serializer_class.__module__,
                "__qualname__": serializer_class.__qualname__,
                "__doc__": serializer_class.__doc__,
            },
        )
        with _fast_classes_lock:
            cls = serializer_class.__dict__.get(_FAST_CLASS_ATTR)
            if cls is None:
                cls = new
                setattr(serializer_class, _FAST_CLASS_ATTR, cls)
    return cls  # type: ignore[return-value]
