"""
Compiles a bound DRF serializer instance into a single "row" function that
produces exactly what ``rest_framework.serializers.Serializer.to_representation``
would produce for it.

The generated function inlines DRF's per-field loop. Each readable field gets:

* an *access* strategy, i.e. how the attribute is fetched:

  - ``attr``     plain ``o.name`` for concrete model columns (and FK ``*_id``);
  - ``fkobj``    ``o.fk`` for forward FK/O2O, mapping ObjectDoesNotExist to None;
  - ``prefetch`` read Django's prefetch cache directly for to-many relations,
                 falling back to ``get`` when the relation wasn't prefetched;
  - ``call``     ``o.name()`` for a model method source such as
                 ``get_FOO_display`` (DRF re-inspects its signature per row);
  - ``method``   call the bound ``get_<name>`` of a SerializerMethodField;
  - ``self``     the instance itself, for ``source='*'``;
  - ``path``     DRF's ``fields.get_attribute(o, source_attrs)`` with its
                 per-row type checks cached: dotted sources, properties,
                 annotations, dict keys. A missing attribute or key falls
                 back to ``get``, so DRF decides default/None/skip/error;
  - ``get``      ``field.get_attribute(o)`` with DRF's SkipField/None handling.

* a *conversion* applied to non-None values, which is either the exact
  expression DRF's ``to_representation`` evaluates for that field type
  (guarded by exact type checks), an inlined nested serializer, or a call to
  the field's own ``to_representation``.

Every shortcut is only taken when the field class has not overridden the DRF
method it replaces. Anything unrecognised goes through the field itself, so the
output is always the field's own output.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import functools
import inspect
import keyword
import sys
import threading
import types
from collections.abc import Callable, Generator, Mapping
from contextvars import ContextVar
from typing import Any, NamedTuple, Protocol

from django.conf import settings as dj_settings
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist
from django.db.models import Field, ManyToManyField, ManyToManyRel, ManyToOneRel, Model
from django.db.models.manager import BaseManager
from django.utils import timezone as dj_timezone
from rest_framework import ISO_8601
from rest_framework import fields as f
from rest_framework import relations as r
from rest_framework import serializers as s
from rest_framework.fields import BuiltinSignatureError, SkipField, is_simple_callable
from rest_framework.relations import PKOnlyObject
from rest_framework.settings import api_settings

from . import conf

# Set on methods (FastOutputMixin.to_representation, FastListSerializer
# .to_representation) that are pure accelerations of the DRF method they
# shadow, so the compiler can look through them.
PASSTHROUGH = "_fast_output_passthrough"
_PASSTHROUGH_FNS: set[Callable] = set()


def passthrough(fn: Callable) -> Callable:
    """Mark ``fn`` as a pure acceleration of the DRF method it shadows."""
    setattr(fn, PASSTHROUGH, True)
    _PASSTHROUGH_FNS.add(fn)
    return fn


# True while computing the reference output in VERIFY mode: every fast
# serializer then takes the stock DRF path.
force_drf: ContextVar[bool] = ContextVar("drf_fast_output_force_drf", default=False)


@contextlib.contextmanager
def stock_drf() -> Generator[None, None, None]:
    """Within this block every fast serializer takes the stock DRF path."""
    token = force_drf.set(True)
    try:
        yield
    finally:
        force_drf.reset(token)


_ROW_ATTR = "_fast_output_compiled"
_SKIP = object()

# Datetime modes, computed once per top-level call.
DT_OTHER, DT_UTC, DT_NAIVE = 0, 1, 2
_UTC_KEYS = frozenset({"UTC", "Etc/UTC"})


# True while a compiled call (a list, or a single object with
# fast_output_single) is in progress, so the single-object serializers it calls
# but doesn't inline (e.g. one with its own to_representation) compile too.
in_fast_call: ContextVar[bool] = ContextVar("drf_fast_output_in_fast_call", default=False)


def datetime_mode() -> int:
    """
    The datetime mode for the active timezone. Read per compiled call, never
    shared with the serializers it calls but doesn't inline: those may switch
    the timezone themselves (``timezone.override()`` in a to_representation).
    """
    if not dj_settings.USE_TZ:
        return DT_NAIVE
    tz = dj_timezone.get_current_timezone()
    if tz is _dt.timezone.utc or getattr(tz, "key", None) in _UTC_KEYS:
        return DT_UTC
    return DT_OTHER


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------


# Everything about field, serializer and model *classes* is decided once and
# cached in that class's own __dict__: as cheap as an attribute read, and gone
# with the class (no leaks for classes created at runtime). Entries record
# every method they were computed from, so patching one later (e.g. mock.patch
# in tests) is noticed; the results feed into the template key (_fingerprint),
# so compiled code is not reused across such a patch either. Patches made
# before this package is imported are caught by drf_impl().

_FIELD_CLASS_ATTR = "_fast_output_field_class"
_MODEL_ATTRS_ATTR = "_fast_output_model_attrs"


def _own(cls: type, attr: str) -> Any:
    return cls.__dict__.get(attr)


def _store(cls: type, attr: str, value: Any) -> Any:
    with contextlib.suppress(TypeError, AttributeError):  # e.g. builtin/extension types
        type.__setattr__(cls, attr, value)
    return value


def _class_memo(cls: type, attr: str) -> dict:
    return _own(cls, attr) or _store(cls, attr, {})


def drf_impl(cls: type, name: str, base: type, after: type | None = None) -> bool:
    """
    True if ``cls.<name>`` resolves to the library's original ``base.<name>``
    (DRF's, or Django's for ``Model``), looking through passthrough methods.
    With ``after``, only the MRO after that class counts.

    "Original" is decided from the function itself rather than by comparing
    with ``base.<name>``, which may already be patched (even before this
    package was imported), so a replaced method is never taken for stock.
    """
    home, fn = _resolve_impl(cls, name, after)
    return home is not None and fn is _resolve_impl(base, name, None)[1] and _is_original(fn, home, name)


def _resolve_impl(cls: type, name: str, after: type | None) -> tuple[type | None, Any]:
    """``(class whose __dict__ defines it, function)`` for ``cls.<name>``."""
    mro = cls.__mro__
    if after is not None and after in mro:
        mro = mro[mro.index(after) + 1 :]
    for klass in mro:
        fn = klass.__dict__.get(name)
        if fn is None or getattr(fn, PASSTHROUGH, False):
            continue
        return klass, fn
    return None, None


def _is_original(fn: Any, home: type, name: str) -> bool:
    """
    Is ``fn`` the function written as ``name`` in the body of class ``home``?
    A replacement (a lambda, a ``functools.wraps`` wrapper, another class's
    method) has a different qualified name or was defined in another module.
    """
    module = sys.modules.get(home.__module__)
    return (
        isinstance(fn, types.FunctionType)
        and fn.__qualname__ == f"{home.__qualname__}.{name}"
        and module is not None
        and fn.__globals__ is vars(module)
    )


def enabled() -> bool:
    """The master switch. Checked by the entry points on every call, not cached."""
    return bool(conf.get("ENABLED"))


def opted_in(serializer: Any) -> bool:
    meta = getattr(serializer, "Meta", None)
    return getattr(meta, "fast_output", True) is not False


def inlineable(serializer: Any) -> bool:
    """
    Can ``serializer.to_representation(x)`` be replaced by its compiled row?
    (The master switch is not part of this: compiled code only runs once an
    entry point has checked it.)
    """
    return (
        isinstance(serializer, s.Serializer)
        and "to_representation" not in serializer.__dict__  # not overridden on the instance
        and drf_impl(type(serializer), "to_representation", s.Serializer)
        and opted_in(serializer)
    )


def _is_iso(fmt: Any) -> bool:
    return isinstance(fmt, str) and fmt.lower() == ISO_8601


def _safe_attr(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


# --------------------------------------------------------------------------
# Field classification
# --------------------------------------------------------------------------


class Plan(NamedTuple):
    """How one field is rendered (see :func:`plan`; for introspection)."""

    key: str  # output key (field.field_name)
    field: Any  # the bound DRF field
    access: str  # attr | fkobj | prefetch | call | method | self | path | get
    attr: str | None  # attribute / prefetch cache name for attr, fkobj, prefetch, call
    conv: str  # see _CONV
    needs_dtm: bool = False


class _FieldClass(NamedTuple):
    kind: str  # what to_representation is, see _classify_field_class()
    get: str  # what get_attribute is: plain | related | many | other
    pk_only: bool  # stock PrimaryKeyRelatedField.use_pk_only_optimization


# Field classes whose stock to_representation maps directly to a conversion.
_DIRECT = (
    (f.CharField, "char"),  # str(value); also Email/Slug/URL/IP/Regex
    (f.IntegerField, "int"),  # int(value)
    *(((f.BigIntegerField, "bigint"),) if hasattr(f, "BigIntegerField") else ()),  # DRF >= 3.17
    (f.FloatField, "float"),  # float(value)
    (f.BooleanField, "bool"),
    (f.ReadOnlyField, "readonly"),  # value
    (f.JSONField, "json"),  # value unless binary
    (f.UUIDField, "uuid"),
    (f.DateField, "date"),
    (f.TimeField, "time"),
    (f.SerializerMethodField, "smf"),
    (r.PrimaryKeyRelatedField, "pkrel"),  # value.pk unless pk_field
)


# Every method _classify_field_class() inspects.
_FIELD_CLASS_DEPS = (
    "to_representation",
    "get_attribute",
    "enforce_timezone",
    "default_timezone",
    "use_pk_only_optimization",
)


def _field_class(t: type) -> _FieldClass:
    cached = t.__dict__.get(_FIELD_CLASS_ATTR)  # runs per field per instance: no helper call
    deps = tuple([getattr(t, name, None) for name in _FIELD_CLASS_DEPS])
    if deps[0] in _PASSTHROUGH_FNS:
        # The classification depends on the method behind the passthrough
        # (e.g. ListSerializer.to_representation), so that is what to watch.
        deps = (_resolve_impl(t, _FIELD_CLASS_DEPS[0], None)[1], *deps[1:])
    if cached is not None and cached[0] == deps:
        return cached[1]
    info = _classify_field_class(t)
    _store(t, _FIELD_CLASS_ATTR, (deps, info))
    return info


def _classify_field_class(t: type) -> _FieldClass:
    def same(base: type, name: str = "to_representation") -> bool:
        return drf_impl(t, name, base)

    if issubclass(t, s.ListSerializer):
        kind = "list" if same(s.ListSerializer) else "generic"
    elif issubclass(t, s.BaseSerializer):
        kind = "serializer"  # inlineability is decided per instance
    elif issubclass(t, r.ManyRelatedField):
        kind = "many" if same(r.ManyRelatedField) else "generic"
    elif issubclass(t, f.ChoiceField):
        kind = "choice" if same(f.ChoiceField) and not issubclass(t, f.MultipleChoiceField) else "generic"
    elif issubclass(t, f.DateTimeField):
        is_stock = (
            same(f.DateTimeField)
            and same(f.DateTimeField, "enforce_timezone")
            and same(f.DateTimeField, "default_timezone")
        )
        kind = "datetime" if is_stock else "generic"
    else:
        kind = next((k for base, k in _DIRECT if same(base)), "generic")

    if same(f.Field, "get_attribute"):
        get = "plain"
    elif same(r.RelatedField, "get_attribute"):
        get = "related"
    elif same(r.ManyRelatedField, "get_attribute"):
        get = "many"
    else:
        get = "other"
    pk_only = same(r.PrimaryKeyRelatedField, "use_pk_only_optimization")
    return _FieldClass(kind, get, pk_only)


# --- Per-instance inputs ------------------------------------------------------
#
# A field's plan is a pure function of its *fingerprint* (plus the model class),
# so plans are computed once and shared by every serializer instance with the
# same fingerprints. _fingerprint() is the only place that reads per-instance
# field configuration and the (patchable) field and model class information;
# anything the plan depends on must go through it.

# Fingerprint conv for a field with methods assigned on the instance itself.
_OPAQUE = "opaque"


class _Fingerprint(NamedTuple):
    key: str
    type: type
    source: str
    conv: str  # see _CONV, or _OPAQUE
    fc: _FieldClass | None  # None when opaque
    ma: _ModelAttr | None  # the model attribute ``source`` names, if any


def _fingerprint(field: Any, model: type[Model] | None) -> _Fingerprint:
    t = type(field)
    d = field.__dict__
    source = field.source
    # A method the class classification relies on assigned on the instance, or
    # a bind() that resolves something other than `source` (DRF's get_attribute
    # walks source_attrs): no shortcuts, the field does everything itself.
    if not d.keys().isdisjoint(_FIELD_CLASS_DEPS) or field.source_attrs != (
        [] if source == "*" else source.split(".")
    ):
        return _Fingerprint(field.field_name, t, source, _OPAQUE, None, None)
    fc = _field_class(t)
    ma = None
    if model is not None and source != "*" and "." not in source:
        ma = _model_attr(model, source)
    return _Fingerprint(field.field_name, t, source, _field_conv(field, fc.kind), fc, ma)


_SIMPLE_CONV = {
    "char": "str",
    "int": "int",
    "float": "float",
    "bool": "bool",
    "readonly": "raw",
    "choice": "choice",
}


def _field_conv(field: Any, kind: str) -> str:
    """The conversion (a key of ``_CONV``) this field applies to non-None values."""
    conv = _SIMPLE_CONV.get(kind)
    if conv is not None:
        return conv
    if kind == "list":
        return "list" if inlineable(field.child) else "generic"
    if kind == "serializer":
        return "nested" if inlineable(field) else "generic"
    if kind == "many":
        child = field.child_relation
        ok = (
            "to_representation" not in child.__dict__  # not overridden on the instance
            and _field_class(type(child)).kind == "pkrel"
            and getattr(child, "pk_field", None) is None
        )
        return "pklist" if ok else "generic"
    if kind == "bigint":  # str(value) when coercing to string, else int(value)
        return "str" if getattr(field, "coerce_to_string", api_settings.COERCE_BIGINT_TO_STRING) else "int"
    if kind == "json":  # value unless binary
        return "generic" if field.binary else "raw"
    if kind == "uuid":  # str(value)
        return "str" if field.uuid_format == "hex_verbose" else "generic"
    if kind == "date":
        return "date" if _is_iso(getattr(field, "format", api_settings.DATE_FORMAT)) else "generic"
    if kind == "time":
        return "time" if _is_iso(getattr(field, "format", api_settings.TIME_FORMAT)) else "generic"
    if kind == "datetime":
        iso = _is_iso(getattr(field, "format", api_settings.DATETIME_FORMAT))
        return "datetime" if iso and not hasattr(field, "timezone") else "generic"
    if kind == "pkrel":  # value.pk
        return "pk" if getattr(field, "pk_field", None) is None else "generic"
    return "generic"


# Nested serializers are compiled on first use, like DRF only builds a nested
# serializer's fields once it has a value to render. They may contain
# datetimes, so the datetime mode is always passed down to them.
_NESTED_CONVS = frozenset({"nested", "list"})
_DTM_CONVS = _NESTED_CONVS | {"datetime"}


class _ModelAttr(NamedTuple):
    kind: str  # column | fk | method | other
    attname: str | None  # fk: the ``<fk>_id`` attribute; None otherwise
    prefetch: str | None  # prefetch cache key if ``name`` is a to-many relation
    pk_only_ok: bool  # fk: serializable_value(name) == getattr(o, attname)


def _model_attr(model: type[Model], name: str) -> _ModelAttr:
    per = model.__dict__.get(_MODEL_ATTRS_ATTR) or _class_memo(model, _MODEL_ATTRS_ATTR)
    # What _describe_model_attr() depends on that can be patched at runtime:
    # serializable_value, and what `name` is on the class. The latter is read
    # from the class dicts like inspect.getattr_static (descriptors such as
    # partialmethod return a new object on every class attribute access), but
    # cheaper, as this runs for every field of every serializer instance.
    sv = model.serializable_value
    static = None
    for k in model.__mro__:
        d = k.__dict__
        if name in d:
            static = d[name]
            break
    hit = per.get(name)
    if hit is not None and hit[0] is sv and hit[1] is static:
        return hit[2]
    info = _describe_model_attr(model, name)
    per[name] = (sv, static, info)
    return info


def _describe_model_attr(model: type[Model], name: str) -> _ModelAttr:
    prefetch = _prefetch_cache_name(model, name)
    try:
        mf = model._meta.get_field(name)
    except FieldDoesNotExist:
        kind = "method" if _safe_attr(name) and _is_simple_method(model, name) else "other"
        return _ModelAttr(kind, None, prefetch, False)
    if not isinstance(mf, Field) or not mf.concrete or mf.many_to_many:
        return _ModelAttr("other", None, prefetch, False)
    if (mf.many_to_one or mf.one_to_one) and mf.name == name:
        ok = drf_impl(model, "serializable_value", Model) and _safe_attr(mf.attname)
        return _ModelAttr("fk", mf.attname, prefetch, ok)
    if mf.attname == name:  # concrete column, or the raw `<fk>_id` column
        return _ModelAttr("column", None, prefetch, False)
    return _ModelAttr("other", None, prefetch, False)


def _prefetch_cache_name(model: type[Model], name: str) -> str | None:
    """Key under which Django's related manager for ``name`` stores a prefetch."""
    for fld in model._meta.get_fields(include_hidden=True):
        if isinstance(fld, ManyToManyField):
            if fld.name == name:
                return fld.name
        elif isinstance(fld, (ManyToManyRel, ManyToOneRel)) and (fld.many_to_many or fld.one_to_many):
            if fld.get_accessor_name() != name:
                continue
            if fld.many_to_many:
                return fld.field.related_query_name()
            cache_name = getattr(fld, "cache_name", None)  # Django >= 5.1
            if isinstance(cache_name, str):
                return cache_name
            return fld.get_cache_name()  # pyrefly: ignore[missing-attribute]  # Django < 5.1
    return None


def _is_simple_method(model: type[Model], name: str) -> bool:
    """
    Is ``name`` a method/partialmethod (e.g. ``get_FOO_display``) defined on
    the model class that DRF would call with no arguments? Decided once here
    with DRF's own ``is_simple_callable``; DRF re-decides it per row via
    ``inspect.signature``, which dominates the cost of such fields.
    """
    try:
        static = inspect.getattr_static(model, name)
    except AttributeError:
        return False
    if not (inspect.isfunction(static) or isinstance(static, functools.partialmethod)):
        return False
    try:
        bound = static.__get__(model.__new__(model), model)
        return f.is_simple_callable(bound)
    except Exception:
        return False


class _Spec(NamedTuple):
    """One field's entry in a serializer *shape*, the key of generated code."""

    key: str
    access: str
    attr: str | None
    conv: str
    # attr: ObjectDoesNotExist (a deferred column of a deleted row) gives None,
    # as in fields.get_attribute. Not for the pk-only read, which DRF makes
    # through serializable_value(), outside that handler.
    odne_none: bool = True


# Access for a plain model attribute, by _ModelAttr.kind. A forward FK/O2O is
# read as the related object, for a nested serializer or plain field.
_MODEL_ACCESS = {"fk": "fkobj", "column": "attr", "method": "call"}


def _spec(fp: _Fingerprint) -> _Spec:
    """The plan for one field, from its fingerprint alone."""
    key, _t, source, conv, fc, ma = fp
    if fc is None:  # opaque
        return _Spec(key, "get", None, "generic")

    # SerializerMethodField: source='*' so the attribute is the instance itself.
    if fc.kind == "smf" and fc.get == "plain" and source == "*":
        return _Spec(key, "method", None, "raw")

    # Stock Field.get_attribute: resolve the source ourselves, see _get_path().
    if fc.get == "plain":
        get = _Spec(key, "self" if source == "*" else "path", None, conv)
    else:
        get = _Spec(key, "get", None, conv)
    if ma is None:
        return get
    name = source

    # To-many relations: read the prefetch cache when it is populated (what
    # `manager.all()` would return), otherwise do what DRF does.
    if conv == "pklist" or conv == "list":
        if ma.prefetch is not None and fc.get == ("many" if conv == "pklist" else "plain"):
            return _Spec(key, "prefetch", ma.prefetch, conv)
        return get

    # Forward FK/O2O rendered as a primary key. DRF's pk-only optimisation reads
    # instance.serializable_value(name) == getattr(instance, attname).
    if conv == "pk":
        if ma.kind == "fk" and ma.pk_only_ok and fc.get == "related" and fc.pk_only:
            return _Spec(key, "attr", ma.attname, "raw", odne_none=False)
        return get

    access = _MODEL_ACCESS.get(ma.kind)
    if access is None or fc.get != "plain" or not _safe_attr(name):
        return get
    return _Spec(key, access, name, conv)


# --------------------------------------------------------------------------
# Code generation
# --------------------------------------------------------------------------


# {v}: value variable, {F}: field, {C}: per-field constant
# Guards use type(), not .__class__, which proxies such as SimpleLazyObject
# report as the wrapped type.
_CONV: dict[str, list[str]] = {
    "raw": [],
    "str": ["if _type({v}) is not str: {v} = _str({v})"],
    "int": ["if _type({v}) is not int: {v} = _int({v})"],
    "float": ["if _type({v}) is not float: {v} = _float({v})"],
    "bool": ["if _type({v}) is not bool: {v} = {F}.to_representation({v})"],
    "choice": [  # DRF returns '' before consulting choice_strings_to_values
        "if _type({v}) is str:",
        "    if {v}: {v} = {F}.choice_strings_to_values.get({v}, {v})",
        "elif _type({v}) is int: {v} = {F}.choice_strings_to_values.get(_str({v}), {v})",
        "else: {v} = {F}.to_representation({v})",
    ],
    "date": [
        "if _type({v}) is _date: {v} = {v}.isoformat()",
        "else: {v} = {F}.to_representation({v})",
    ],
    "time": [
        "if _type({v}) is _time: {v} = {v}.isoformat()",
        "else: {v} = {F}.to_representation({v})",
    ],
    "datetime": [
        (
            f"if dtm == {DT_UTC} and _type({{v}}) is _datetime and {{v}}.tzinfo is _utc:"
            " {v} = {v}.isoformat()[:-6] + 'Z'"
        ),
        (
            f"elif dtm == {DT_NAIVE} and _type({{v}}) is _datetime and {{v}}.tzinfo is None:"
            " {v} = {v}.isoformat()"
        ),
        "else: {v} = {F}.to_representation({v})",
    ],
    "pk": ["{v} = {v}.pk"],
    "pklist": ["{v} = [x.pk for x in {v}]"],
    "nested": ["{v} = {C}[0]({v}, dtm)"],
    "list": [
        "_r = {C}[0]",
        "{v} = [_r(x, dtm) for x in ({v}.all() if _isinstance({v}, _Manager) else {v})]",
    ],
    "generic": ["{v} = {F}.to_representation({v})"],
}

_GLOBALS: dict[str, Any] = {
    "_type": type,
    "_str": str,
    "_int": int,
    "_float": float,
    "_isinstance": isinstance,
    "_date": _dt.date,
    "_time": _dt.time,
    "_datetime": _dt.datetime,
    "_utc": _dt.timezone.utc,
    "_SkipField": SkipField,
    "_PKOnly": PKOnlyObject,
    "_SKIP": _SKIP,
    "_ODNE": ObjectDoesNotExist,
    "_tuple": tuple,
    "_path": None,  # set below, once _get_path is defined
    "_PATH_MISS": (KeyError, AttributeError, BuiltinSignatureError),
    "_Manager": BaseManager,
    "_drf": s.Serializer.to_representation,
}

# --- Attribute paths -----------------------------------------------------------
#
# DRF's fields.get_attribute() runs an ``isinstance(x, Mapping)`` ABC check per
# step and ``inspect.signature()`` for every callable it meets, on every row.
# Both answers are cached here: per class, and per function (bound or not, as a
# bound method's signature has no ``self``). Cached on the class / function
# itself, so they go away with it.

_IS_MAPPING_ATTR = "_fast_output_is_mapping"
_SIMPLE_ATTRS = ("_fast_output_simple_unbound", "_fast_output_simple_bound")


def _is_mapping(cls: type) -> bool:
    # Builtins cannot hold our class cache; avoid a failed write per dict lookup.
    if cls is dict:
        return True
    # Stored as (cls, answer): a plain class attribute read, which hits the type
    # attribute cache, and one inherited from a base is recognised as such.
    hit = getattr(cls, _IS_MAPPING_ATTR, None)
    if hit is not None and hit[0] is cls:
        return hit[1]
    answer = issubclass(cls, Mapping)
    _store(cls, _IS_MAPPING_ATTR, (cls, answer))  # no-op for builtins such as dict
    return answer


def _is_simple(value: Any) -> bool:
    """``is_simple_callable(value)``, cached for functions and methods."""
    cls = type(value)
    if cls is types.MethodType:
        fn, slot = value.__func__, 1
    elif cls is types.FunctionType:
        fn, slot = value, 0
    else:  # partials, classes, builtins, callable instances
        return is_simple_callable(value)
    attr = _SIMPLE_ATTRS[slot]
    hit = getattr(fn, attr, None)
    if hit is None:
        hit = is_simple_callable(value)
        setattr(fn, attr, hit)
    return hit


def _get_path(instance: Any, attrs: tuple[str, ...]) -> Any:
    """``rest_framework.fields.get_attribute``, with its type checks cached."""
    for attr in attrs:
        cls = type(instance)
        hit = getattr(cls, _IS_MAPPING_ATTR, None)  # _is_mapping(), inlined
        is_mapping = hit[1] if hit is not None and hit[0] is cls else _is_mapping(cls)
        try:
            instance = instance[attr] if is_mapping else getattr(instance, attr)
        except ObjectDoesNotExist:
            return None
        if callable(instance) and _is_simple(instance):
            try:
                instance = instance()
            except (AttributeError, KeyError) as exc:
                raise ValueError(
                    f'Exception raised in callable attribute "{attr}"; original exception was: {exc}'
                ) from None
    return instance


_GLOBALS["_path"] = _get_path


# Generated code (keyed by shape) and templates (keyed by model and field
# fingerprints) are shared by every serializer instance that matches. Both are
# bounded, because field sets can be client-driven (e.g. sparse fieldsets via
# ?fields=). A race may build an entry twice, which is harmless.
_FACTORY_CACHE_SIZE = 512
_factory_cache: dict[tuple, Callable] = {}
_TEMPLATE_CACHE_SIZE = 512
_template_cache: dict[tuple, _Template] = {}
_cache_lock = threading.Lock()


def _bounded_put(cache: dict, key: Any, value: Any, size: int) -> None:
    with _cache_lock:
        if key not in cache and len(cache) >= size:
            del cache[next(iter(cache))]
        cache[key] = value


def _ind(n: int, block: list[str]) -> list[str]:
    return [" " * n + ln for ln in block]


def _generate(has_model: bool, shape: tuple[_Spec, ...]) -> str:
    """Source of ``make(F, C, S, M) -> row(o, dtm)`` for a serializer shape."""
    make = ["def make(F, C, S, M):"]
    row: list[str] = []
    may_skip = False

    def not_null(v: str, body: list[str]) -> list[str]:
        # DRF's check before calling field.to_representation.
        return [
            f"if {v} is None or (_isinstance({v}, _PKOnly) and {v}.pk is None): {v} = None",
            "else:",
            *(_ind(4, body) or ["    pass"]),
        ]

    def drf_get(i: int, v: str, body: list[str]) -> list[str]:
        # Exactly the body of Serializer.to_representation's field loop.
        nonlocal may_skip
        may_skip = True
        return [
            "try:",
            f"    {v} = G{i}(o)",
            "except _SkipField:",
            f"    {v} = _SKIP; sk = True",
            "else:",
            *_ind(4, not_null(v, body)),
        ]

    for i, (_key, access, attr, conv, odne_none) in enumerate(shape):
        v = f"v{i}"
        template = _CONV[conv]
        body = [ln.format(v=v, F=f"F{i}", C=f"C{i}") for ln in template]

        # Bind only the per-field names this field's code uses.
        names = []
        if any("{F}" in ln for ln in template):
            names.append(f"F{i} = F[{i}]")
        if access in ("get", "prefetch", "call", "path"):
            names.append(f"G{i} = F[{i}].get_attribute")
        if access == "path":
            names.append(f"P{i} = _tuple(F[{i}].source_attrs)")
        if access == "method" or any("{C}" in ln for ln in template):
            names.append(f"C{i} = C[{i}]")
        if names:
            make.append("    " + "; ".join(names))

        if access == "attr":
            if odne_none:
                row += ["try:", f"    {v} = o.{attr}", "except _ODNE:", f"    {v} = None"]
            else:
                row.append(f"{v} = o.{attr}")
            if body:
                row += [f"if {v} is not None:", *_ind(4, body)]
        elif access == "fkobj":
            # fields.get_attribute maps ObjectDoesNotExist to None.
            row += ["try:", f"    {v} = o.{attr}", "except _ODNE:", f"    {v} = None"]
            if body:
                row += [f"if {v} is not None:", *_ind(4, body)]
        elif access == "call":
            # Only for exactly the compiled model class with the method not
            # shadowed on the instance; anything else takes DRF's path.
            row += [
                f"if o.__class__ is M and {attr!r} not in o.__dict__:",
                "    try:",
                f"        {v} = o.{attr}()",
                "    except (AttributeError, KeyError) as exc:",
                f"        raise ValueError('Exception raised in callable attribute \"{attr}\"; '",
                "                         f'original exception was: {exc}')",
                *_ind(4, not_null(v, body)),
                "else:",
                *_ind(4, drf_get(i, v, body)),
            ]
        elif access == "method":
            row.append(f"{v} = None if o is None else C{i}(o)")
        elif access == "self":
            row += [f"{v} = o", *not_null(v, body)]
        elif access == "path":
            # A miss (or a builtin callable) re-reads through the field, so DRF
            # decides between default, None, SkipField and its error message.
            row += [
                "try:",
                f"    {v} = _path(o, P{i})",
                "except _PATH_MISS:",
                *_ind(4, drf_get(i, v, body)),
                "else:",
                *_ind(4, not_null(v, body)),
            ]
        elif access == "prefetch":
            # Read per field: an earlier field (e.g. a method calling
            # refresh_from_db()) may have replaced the cache.
            row += [
                "_pc = o.__dict__.get('_prefetched_objects_cache')",
                f"if _pc is not None and {attr!r} in _pc:",
                f"    {v} = _pc[{attr!r}]",
                *_ind(4, body),
                "else:",
                *_ind(4, drf_get(i, v, body)),
            ]
        elif access == "get":
            row += drf_get(i, v, body)
        else:  # pragma: no cover
            raise AssertionError(access)

    head = []
    if has_model:
        head.append("if o.__class__ is not M and not _isinstance(o, M): return _drf(S, o)")
    if may_skip:
        head.append("sk = False")
    items = ", ".join(f"{p.key!r}: v{i}" for i, p in enumerate(shape))
    tail = [f"d = {{{items}}}"]
    if may_skip:
        tail.append("if sk: d = {k: x for k, x in d.items() if x is not _SKIP}")
    tail.append("return d")
    lines = [*make, "    def row(o, dtm):", *_ind(8, head + row + tail), "    return row"]
    return "\n".join(lines)


def _factory(has_model: bool, shape: tuple[_Spec, ...]) -> Callable:
    key = (has_model, shape)
    make = _factory_cache.get(key)
    if make is None:
        src = _generate(has_model, shape)
        ns = dict(_GLOBALS)
        exec(compile(src, "<drf_fast_output>", "exec"), ns)
        make = ns["make"]
        make.source = src
        _bounded_put(_factory_cache, key, make, _FACTORY_CACHE_SIZE)
    return make


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def _lazy_row(serializer: Any) -> list:
    """
    A one-slot holder the generated code calls as ``C[0](value, dtm)``. The
    first call compiles ``serializer`` and swaps its row into the slot.
    """
    holder: list = [None]

    def first_call(o: Any, dtm: int) -> Any:
        if holder[0] is first_call:
            holder[0] = get_row(serializer)
        return holder[0](o, dtm)

    holder[0] = first_call
    return holder


def _model_of(serializer: Any) -> type[Model] | None:
    if not isinstance(serializer, s.ModelSerializer):
        return None
    model = getattr(getattr(serializer, "Meta", None), "model", None)
    return model if isinstance(model, type) and issubclass(model, Model) else None


class Row(Protocol):
    """A compiled serializer row: ``row(instance, datetime_mode) -> dict``."""

    needs_dtm: bool  # whether the row reads the datetime mode argument

    def __call__(self, instance: Any, dtm: int, /) -> Any: ...


class _Template(NamedTuple):
    make: Callable
    specs: tuple[_Spec, ...]
    binds: tuple[tuple[int, str], ...]  # (field index, "method" | "nested" | "list")
    needs_dtm: bool


def _bind(p: _Spec) -> str | None:
    """What per-instance constant field ``p``'s code reads as ``C{i}``."""
    if p.access == "method":
        return "method"
    return p.conv if p.conv in _NESTED_CONVS else None


def _template(model: type[Model] | None, fields: tuple) -> _Template:
    key = (model, tuple([_fingerprint(fl, model) for fl in fields]))
    tpl = _template_cache.get(key)
    if tpl is None:
        specs = tuple(_spec(fp) for fp in key[1])
        tpl = _Template(
            make=_factory(model is not None, specs),
            specs=specs,
            binds=tuple((i, b) for i, p in enumerate(specs) if (b := _bind(p)) is not None),
            needs_dtm=any(p.conv in _DTM_CONVS for p in specs),
        )
        _bounded_put(_template_cache, key, tpl, _TEMPLATE_CACHE_SIZE)
    return tpl


def _template_of(serializer: Any) -> tuple[type[Model] | None, tuple, _Template]:
    model = _model_of(serializer)
    fields = tuple(serializer._readable_fields)
    return model, fields, _template(model, fields)


def plan(serializer: Any) -> list[Plan]:
    """The per-field plan for a bound serializer instance."""
    _model, fields, tpl = _template_of(serializer)
    return [
        Plan(p.key, fl, p.access, p.attr, p.conv, p.conv in _DTM_CONVS)
        for p, fl in zip(tpl.specs, fields, strict=True)
    ]


def get_row(serializer: Any) -> Row:
    """
    Compiled equivalent of ``Serializer.to_representation`` bound to this
    serializer instance, cached on it. Callers decide whether it may be used
    (see :func:`inlineable`).
    """
    row = serializer.__dict__.get(_ROW_ATTR)
    if row is not None:
        return row
    model, fields, tpl = _template_of(serializer)
    consts: list = [None] * len(fields)
    for i, bind in tpl.binds:
        fl = fields[i]
        if bind == "method":
            consts[i] = getattr(serializer, fl.method_name)
        else:
            consts[i] = _lazy_row(fl.child if bind == "list" else fl)
    row = tpl.make(fields, consts, serializer, model)
    row.needs_dtm = tpl.needs_dtm
    serializer.__dict__[_ROW_ATTR] = row
    return row
