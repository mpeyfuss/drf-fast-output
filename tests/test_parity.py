"""Fast output must be indistinguishable from stock DRF output."""

import datetime as dt
import enum
import zoneinfo
from collections import OrderedDict
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from django.db.models import Prefetch
from django.test import override_settings
from django.utils import timezone
from django.utils.functional import SimpleLazyObject
from rest_framework import serializers
from rest_framework.fields import BuiltinSignatureError

from drf_fast_output import FastModelSerializer, FastSerializer, explain, strict_equal
from drf_fast_output.compiler import plan, stock_drf
from tests.testapp.models import Author, Book, Chapter, Note, SpecialBook, Tag
from tests.testapp.serializers import (
    BookAllSerializer,
    BookRichSerializer,
    CustomBookSerializer,
    DictSerializer,
    OptedOutSerializer,
    ReaderAuthorSerializer,
    TagWithBooksSerializer,
)

pytestmark = pytest.mark.django_db

RICH_QS = lambda: Book.objects.select_related(  # noqa: E731
    "author__profile", "editor__profile"
).prefetch_related("tags", "chapters", "note_set", "readers")


def plans_by_key(serializer):
    return {p.key: (p.access, p.attr, p.conv) for p in plan(serializer)}


# --------------------------------------------------------------------------
# Model serializers
# --------------------------------------------------------------------------


def test_all_fields_many(library, parity):
    out = parity(lambda: BookAllSerializer(Book.objects.order_by("id"), many=True))
    assert len(out) == 3
    assert out[0]["created"] == "2024-01-02T03:04:05.678901Z"
    assert out[0]["tags"] == [library["tags"][0].pk, library["tags"][1].pk]


def test_all_fields_single(library, parity):
    out = parity(lambda: BookAllSerializer(Book.objects.get(pk=library["b1"].pk)))
    assert out["publisher"] == "PNG"  # FK with to_field: DRF exposes the raw column


def test_all_fields_plan_is_fast(library):
    plans = plans_by_key(BookAllSerializer(Book.objects.first()))
    assert plans["title"] == ("attr", "title", "str")
    assert plans["author"] == ("attr", "author_id", "raw")
    assert plans["created"] == ("attr", "created", "datetime")
    assert plans["status"] == ("attr", "status", "choice")
    assert plans["tags"] == ("prefetch", "tags", "pklist")
    assert plans["price"] == ("attr", "price", "generic")  # DecimalField is delegated


def test_rich_many_prefetched(library, parity):
    out = parity(lambda: BookRichSerializer(RICH_QS().order_by("id"), many=True))
    dune = out[0]
    assert dune["author"]["profile"] == {"id": library["alice"].profile.pk, "bio": "Writes things"}
    assert dune["editor"]["profile"] is None
    assert [c["number"] for c in dune["chapters"]] == [1, 2]
    assert dune["note_set"] == [{"id": 1, "text": "great"}]
    assert dune["author_bio"] == "Writes things"
    assert dune["status_display"] == "Published"
    assert dune["created_date"] == "2024-01-02"
    assert out[1]["editor"] is None
    assert out[1]["author_bio"] is None


def test_rich_not_prefetched(library, parity):
    """Same output and same queries when relations are not prefetched."""
    parity(lambda: BookRichSerializer(Book.objects.order_by("id"), many=True))


def test_rich_plan(library):
    plans = plans_by_key(BookRichSerializer(Book.objects.first()))
    assert plans["author"] == ("fkobj", "author", "nested")
    assert plans["tags"] == ("prefetch", "tags", "list")
    assert plans["chapters"] == ("prefetch", "chapters", "list")
    assert plans["note_set"] == ("prefetch", "note_set", "list")
    assert plans["reader_ids"] == ("prefetch", "readers", "pklist")
    assert plans["author_pk"] == ("attr", "author_id", "int")
    assert plans["editor_pk"] == ("attr", "editor_id", "raw")
    assert plans["author_name"] == ("path", None, "str")  # dotted source
    assert plans["tag_count"] == ("method", None, "raw")
    assert plans["title_upper"] == ("path", None, "raw")  # property
    assert plans["summary"] == ("call", "get_summary", "raw")
    assert plans["status_display"] == ("call", "get_status_display", "str")


def test_filtered_and_to_attr_prefetch(library, parity):
    def build():
        qs = Book.objects.prefetch_related(
            Prefetch("chapters", queryset=Chapter.objects.filter(number=1)),
            Prefetch("tags", queryset=Tag.objects.filter(slug="classic"), to_attr="classic_tags"),
        ).order_by("id")
        return BookRichSerializer(qs, many=True)

    out = parity(build)
    assert [c["number"] for c in out[0]["chapters"]] == [1]
    assert len(out[0]["tags"]) == 2  # to_attr doesn't populate the manager cache


def test_reverse_m2m_default_and_named(library, parity):
    parity(lambda: ReaderAuthorSerializer(Author.objects.prefetch_related("book_set", "books"), many=True))
    parity(lambda: ReaderAuthorSerializer(Author.objects.all(), many=True))
    plans = plans_by_key(ReaderAuthorSerializer(Author.objects.first()))
    assert plans["book_set"] == ("prefetch", "book", "pklist")
    assert plans["books"] == ("prefetch", "books", "pklist")
    parity(lambda: TagWithBooksSerializer(Tag.objects.prefetch_related("books"), many=True))


def test_deferred_fields(library, parity):
    parity(lambda: BookAllSerializer(Book.objects.only("id", "title").order_by("id"), many=True))


def test_multi_table_child_instances(library, parity):
    parity(lambda: BookAllSerializer(SpecialBook.objects.all(), many=True))
    parity(lambda: BookRichSerializer(SpecialBook.objects.all(), many=True))
    parity(lambda: BookRichSerializer([Book.objects.get(title="Dune"), SpecialBook.objects.get()], many=True))


def test_method_source_shadowed_on_instance(library, parity):
    def build():
        book = Book.objects.get(title="Dune")
        book.get_summary = lambda: "shadowed"
        return BookRichSerializer(book)

    assert parity(build)["summary"] == "shadowed"


def test_method_source_raising(library, monkeypatch):
    from drf_fast_output.compiler import stock_drf

    def boom(self):
        raise AttributeError("nope")

    monkeypatch.setattr(Book, "get_summary", boom)
    book = Book.objects.get(title="Dune")
    with pytest.raises(ValueError) as fast_exc:
        BookRichSerializer(book).data
    with stock_drf(), pytest.raises(ValueError) as drf_exc:
        BookRichSerializer(book).data
    assert str(fast_exc.value) == str(drf_exc.value)


def test_manager_and_generator_input(library, parity):
    alice = library["alice"]
    parity(lambda: BookAllSerializer(alice.books, many=True))
    parity(lambda: BookAllSerializer((b for b in Book.objects.order_by("id")), many=True))


def test_unsaved_instance(parity, db):
    book = Book(title="New", pages=1, price=1, created=timezone.now(), author_id=None)
    out = parity(lambda: BookAllSerializer(book))
    assert out["tags"] == []
    assert out["readers"] == []


def test_non_model_instance_on_model_serializer(parity):
    data = {"id": 1, "slug": "x", "books": []}
    parity(lambda: TagWithBooksSerializer(data))


def test_mixed_list(library, parity):
    items = [Tag.objects.first(), {"id": 9, "slug": "d", "books": []}]
    parity(lambda: TagWithBooksSerializer(items, many=True))


def test_null_values(library, parity):
    out = parity(lambda: BookAllSerializer(library["b2"]))
    assert out["weight"] is None
    assert out["editor"] is None
    assert out["meta"] is None


# --------------------------------------------------------------------------
# Customisation is honoured
# --------------------------------------------------------------------------


def test_custom_fields_and_overrides(library, parity):
    out = parity(lambda: CustomBookSerializer(Book.objects.order_by("id"), many=True))
    assert out[0]["title"] == "DUNE!"
    assert out[0]["prefixed"] == "x-Dune"
    assert out[0]["added"] == 413
    assert out[0]["overridden"] is True
    assert all(t["custom"] for t in out[0]["tags"])
    out = parity(lambda: CustomBookSerializer(library["b1"]))
    assert out["overridden"] is True


def test_custom_plan_delegates(library):
    plans = plans_by_key(CustomBookSerializer(Book.objects.first()))
    assert plans["title"] == ("attr", "title", "generic")
    assert plans["prefixed"] == ("get", None, "str")
    assert plans["tags"][2] == "generic"  # CustomTagSerializer overrides to_representation


def test_opted_out(library):
    ser = OptedOutSerializer(Book.objects.first())
    assert ser.data["title"] == "Dune"
    assert "_fast_output_compiled" not in ser.__dict__


@override_settings(DRF_FAST_OUTPUT={"ENABLED": False})
def test_globally_disabled(library):
    ser = BookAllSerializer(Book.objects.first())
    assert ser.data["title"] == "Dune"
    assert "_fast_output_compiled" not in ser.__dict__


def test_runtime_field_customisation(library, parity):
    class Dynamic(FastModelSerializer):
        class Meta:
            model = Book
            fields = ["id", "title", "pages"]

        def __init__(self, *args, keep=None, **kwargs):
            super().__init__(*args, **kwargs)
            for name in set(self.fields) - set(keep or self.fields):
                self.fields.pop(name)
            self.fields["weird key"] = serializers.CharField(source="title")

    out = parity(lambda: Dynamic(Book.objects.order_by("id"), many=True, keep=["id"]))
    assert list(out[0]) == ["id", "weird key"]


def test_wrong_mixin_order():
    from drf_fast_output import FastOutputMixin

    with pytest.raises(TypeError, match="must come before"):

        class Bad(serializers.Serializer, FastOutputMixin):
            pass


# --------------------------------------------------------------------------
# Plain Serializer / dict input
# --------------------------------------------------------------------------


def test_dicts_with_skip_default_and_nested(library, parity):
    when = dt.datetime(2024, 1, 1, 12, tzinfo=dt.timezone.utc)
    rows = [
        {
            "name": "a",
            "count": 3,
            "ratio": 0.5,
            "flag": True,
            "when": when,
            "day": dt.date(2024, 1, 1),
            "color": "red",
            "nested": library["alice"],
        },
        {"name": 5, "ratio": None, "when": when},  # count/day/color/nested skipped, flag defaulted
        {"name": "c", "count": "7", "ratio": 1, "flag": "yes", "when": "already-a-string", "color": "blue"},
    ]
    out = parity(lambda: DictSerializer(rows, many=True))
    assert list(out[1]) == ["name", "ratio", "flag", "when"]
    assert out[2]["count"] == 7
    assert out[2]["ratio"] == 1.0
    assert out[2]["flag"] is True


def test_missing_required_attribute_raises_like_drf():
    with pytest.raises(KeyError):
        DictSerializer({"ratio": 1}).data


# --------------------------------------------------------------------------
# Datetimes, timezones and formats
# --------------------------------------------------------------------------


class WhenSerializer(FastSerializer):
    at = serializers.DateTimeField()
    at_fmt = serializers.DateTimeField(source="at", format="%Y")
    at_ny = serializers.DateTimeField(source="at", default_timezone=zoneinfo.ZoneInfo("America/New_York"))
    day = serializers.DateField(source="d")
    t = serializers.TimeField()


def _times():
    utc = dt.timezone.utc
    return [
        {"at": dt.datetime(2024, 3, 10, 6, 30, tzinfo=utc), "d": dt.date(2024, 1, 1), "t": dt.time(1, 2, 3)},
        {"at": dt.datetime(2024, 3, 10, 6, 30, 0, 1, tzinfo=utc), "d": dt.date(2024, 1, 1), "t": dt.time(0)},
        {
            "at": dt.datetime(2024, 3, 10, 6, 30, tzinfo=zoneinfo.ZoneInfo("Asia/Tokyo")),
            "d": dt.date(2024, 1, 1),
            "t": dt.time(23, 59, 59, 999),
        },
        {"at": dt.datetime(2024, 3, 10, 6, 30), "d": dt.date(2024, 1, 1), "t": dt.time(1)},
    ]


@pytest.mark.parametrize("use_tz", [True, False])
@pytest.mark.parametrize("tz", ["UTC", "Etc/UTC", "America/New_York", None])
def test_datetimes(use_tz, tz, parity):
    with override_settings(USE_TZ=use_tz):
        if tz is None:
            parity(lambda: WhenSerializer(_times(), many=True))
        else:
            with timezone.override(zoneinfo.ZoneInfo(tz)):
                parity(lambda: WhenSerializer(_times(), many=True))


@override_settings(REST_FRAMEWORK={"DATETIME_FORMAT": "%d/%m/%Y", "DATE_FORMAT": None})
def test_api_settings_formats(library, parity):
    out = parity(lambda: BookAllSerializer(library["b1"]))
    assert out["created"] == "02/01/2024"
    assert out["published"] == dt.date(1965, 8, 1)


# --------------------------------------------------------------------------
# VERIFY mode and debugging helpers
# --------------------------------------------------------------------------


@override_settings(DRF_FAST_OUTPUT={"VERIFY": True})
def test_verify_mode_passes(library):
    BookRichSerializer(RICH_QS(), many=True).data
    BookRichSerializer(RICH_QS().first()).data


@override_settings(DRF_FAST_OUTPUT={"VERIFY": True})
def test_verify_mode_catches_divergence(library, monkeypatch):
    from drf_fast_output import compiler

    monkeypatch.setitem(compiler._CONV, "str", ["{v} = 'wrong'"])
    monkeypatch.setattr(compiler, "_factory_cache", {})
    monkeypatch.setattr(compiler, "_template_cache", {})
    with pytest.raises(AssertionError, match="differs from DRF"):
        BookAllSerializer(Book.objects.first()).data


def test_unknown_setting_key():
    with override_settings(DRF_FAST_OUTPUT={"ENABLD": True}), pytest.raises(ValueError, match="ENABLD"):
        BookAllSerializer(Book(title="x")).data


def test_explain(library):
    text = explain(BookRichSerializer(Book.objects.all(), many=True))
    assert "author: access=fkobj author conv=nested" in text
    assert "        profile: access=path conv=nested" in text


@override_settings(REST_FRAMEWORK={"COERCE_BIGINT_TO_STRING": True})
def test_bigint_coerced_to_string(library, parity):
    from rest_framework import fields

    out = parity(lambda: BookAllSerializer(Book.objects.order_by("id"), many=True))
    # DRF < 3.17 has no BigIntegerField: BigAutoField ids stay ints.
    assert isinstance(out[0]["id"], str if hasattr(fields, "BigIntegerField") else int)


def test_bigint_plan(library):
    # "int" on every DRF version: IntegerField, or BigIntegerField not coercing.
    assert plans_by_key(BookAllSerializer(Book.objects.first()))["id"] == ("attr", "id", "int")


def test_explain_top_level_override(library):
    text = explain(CustomBookSerializer(Book.objects.first()))
    assert "not inlined" not in text
    assert "tags: access=path conv=generic" in text  # CustomTagSerializer runs as-is
    assert "title: access=attr title conv=generic" in text


# --------------------------------------------------------------------------
# Plans are shared across serializer instances
# --------------------------------------------------------------------------


class ConfigurableSerializer(FastModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "created", "uid", "editor", "meta"]

    def __init__(self, *args, fmt=None, hex_uuid=False, pk_as_str=False, binary=False, **kwargs):
        super().__init__(*args, **kwargs)
        if fmt:
            self.fields["created"].format = fmt  # pyrefly: ignore[missing-attribute]
        if hex_uuid:
            self.fields["uid"].uuid_format = "hex"  # pyrefly: ignore[missing-attribute]
        if pk_as_str:
            self.fields["editor"].pk_field = serializers.CharField()  # pyrefly: ignore[missing-attribute]
        if binary:
            self.fields["meta"].binary = True  # pyrefly: ignore[missing-attribute]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"fmt": "%Y"},
        {"hex_uuid": True},
        {"pk_as_str": True},
        {"binary": True},
        {},  # noqa: PT014 - default again, after the others, to catch leaks via shared plans
    ],
)
def test_shared_plans_respect_instance_config(library, parity, kwargs):
    out = parity(lambda: ConfigurableSerializer(library["b1"], **kwargs))
    if kwargs.get("fmt"):
        assert out["created"] == "2024"
    if kwargs.get("pk_as_str"):
        assert out["editor"] == str(library["bob"].pk)


def test_instance_level_method_override(library, parity):
    def build():
        ser = ConfigurableSerializer(library["b1"])
        ser.fields["id"].to_representation = lambda value: f"patched-{value}"
        return ser

    assert parity(build)["id"].startswith("patched-")


def test_template_reused_across_instances(library):
    from drf_fast_output import compiler

    BookAllSerializer(Book.objects.first()).data
    size = len(compiler._template_cache)
    for _ in range(5):
        BookAllSerializer(Book.objects.first()).data
    assert len(compiler._template_cache) == size


def test_template_cache_is_bounded(library, monkeypatch):
    from drf_fast_output import compiler

    monkeypatch.setattr(compiler, "_TEMPLATE_CACHE_SIZE", 3)
    monkeypatch.setattr(compiler, "_template_cache", {})
    names = ["id", "title", "pages", "weight", "status"]
    for i in range(len(names)):

        class Sparse(FastModelSerializer):
            class Meta:
                model = Book
                fields = names[: i + 1]

        Sparse(Book.objects.first()).data
    assert len(compiler._template_cache) == 3


# --------------------------------------------------------------------------
# Patches and overrides made after compiling
# --------------------------------------------------------------------------


def _patched(cls, name, value):
    return mock.patch.object(cls, name, value)


@pytest.mark.parametrize(
    ("cls", "name", "value", "key", "expected"),
    [
        (serializers.CharField, "get_attribute", lambda self, inst: "PATCHED", "title", "PATCHED"),
        (serializers.CharField, "to_representation", lambda self, v: "PATCHED", "title", "PATCHED"),
        (
            serializers.DateTimeField,
            "enforce_timezone",
            lambda self, v: v.astimezone(zoneinfo.ZoneInfo("Asia/Tokyo")),
            "created",
            "2024-01-02T12:04:05.678901+09:00",
        ),
        (Book, "serializable_value", lambda self, name: 999 if name == "author" else 0, "author", 999),
    ],
)
def test_class_patched_after_compile(library, parity, cls, name, value, key, expected):
    """Compiled code is not reused once a method it relies on is patched."""
    build = lambda: BookAllSerializer(Book.objects.get(pk=library["b1"].pk))  # noqa: E731
    before = parity(build)[key]
    with _patched(cls, name, value):
        assert parity(build)[key] == expected
    assert parity(build)[key] == before  # and the stock fast path is back afterwards


def test_patched_pk_only_optimization(library, parity):
    build = lambda: BookAllSerializer(Book.objects.get(pk=library["b1"].pk))  # noqa: E731
    assert plans_by_key(build())["author"] == ("attr", "author_id", "raw")
    with _patched(serializers.PrimaryKeyRelatedField, "use_pk_only_optimization", lambda self: False):
        assert plans_by_key(build())["author"][0] == "get"
        parity(build)


def test_patched_model_method_source(library, parity):
    build = lambda: BookRichSerializer(Book.objects.get(pk=library["b1"].pk))  # noqa: E731
    parity(build)
    with _patched(Book, "get_status_display", lambda self: "PATCHED"):
        assert parity(build)["status_display"] == "PATCHED"


def test_instance_override_on_many_related_child(library, parity):
    def build():
        ser = BookAllSerializer(Book.objects.get(pk=library["b1"].pk))
        ser.fields["tags"].child_relation.to_representation = lambda v: f"t{v.pk}"  # pyrefly: ignore[missing-attribute]
        return ser

    tags = library["tags"]
    assert parity(build)["tags"] == [f"t{tags[0].pk}", f"t{tags[1].pk}"]


@pytest.mark.parametrize("name", ["tags", "note_set"])  # stock and fast ListSerializer
def test_instance_override_on_nested_list_child(library, parity, name):
    def build():
        ser = BookRichSerializer(RICH_QS().get(pk=library["b1"].pk))
        ser.fields[name].child.to_representation = lambda v: f"x{v.pk}"  # pyrefly: ignore[missing-attribute]
        return ser

    assert all(x.startswith("x") for x in parity(build)[name])


def test_instance_override_on_top_level_list_child(library, parity):
    from tests.testapp.serializers import NoteSerializer

    def build():
        ser = NoteSerializer(Note.objects.all(), many=True)
        ser.child.to_representation = lambda v: f"x{v.pk}"  # pyrefly: ignore[missing-attribute]
        return ser

    assert parity(build) == [f"x{n.pk}" for n in Note.objects.all()]


# --------------------------------------------------------------------------
# strict_equal
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "equal"),
    [
        ({"a": 1}, OrderedDict(a=1), False),
        ([{"a": 1}], [OrderedDict(a=1)], False),
        ({"a": 1, "b": 2}, {"b": 2, "a": 1}, False),
        (1, True, False),
        (1, 1.0, False),
        (float("nan"), float("nan"), True),
        ({"x": [float("nan")]}, {"x": [float("nan")]}, True),
        (float("nan"), 1.0, False),
        (Decimal("NaN"), Decimal("NaN"), True),
        (Decimal("sNaN"), Decimal("sNaN"), True),
        (Decimal("NaN"), Decimal("1"), False),
        (Decimal("1.0"), Decimal("1.00"), True),
        ({True: "v"}, {1: "v"}, False),  # render as "true" / "1"
    ],
)
def test_strict_equal(a, b, equal):
    assert strict_equal(a, b) is equal
    assert strict_equal(b, a) is equal


@override_settings(DRF_FAST_OUTPUT={"VERIFY": True})
def test_verify_mode_accepts_nan(library):
    class NaNSerializer(FastModelSerializer):
        x = serializers.SerializerMethodField()

        class Meta:
            model = Book
            fields = ["id", "x"]

        def get_x(self, obj):
            return float("nan")

    NaNSerializer(Book.objects.all(), many=True).data
    NaNSerializer(Book.objects.first()).data


# --------------------------------------------------------------------------
# Recognising DRF's own methods
# --------------------------------------------------------------------------


def test_stock_methods_are_recognised():
    """Every DRF method the compiler shortcuts is seen as DRF's own; if a DRF
    release wrapped one in a decorator, the fast path would silently turn off."""
    from django.db.models import Model
    from rest_framework import fields, relations

    from drf_fast_output import compiler

    bases = {b for b, _k in compiler._DIRECT} | {
        fields.Field,
        fields.ChoiceField,
        fields.DateTimeField,
        relations.RelatedField,
        relations.ManyRelatedField,
        serializers.ListSerializer,
        serializers.Serializer,
    }
    for base in bases:
        for name in compiler._FIELD_CLASS_DEPS:
            if hasattr(base, name):
                assert compiler.drf_impl(base, name, base), (base, name)
    assert compiler.drf_impl(Book, "serializable_value", Model)


def test_wrapped_method_is_not_stock(library, parity):
    """A functools.wraps wrapper keeps the name and module, but is not DRF's."""
    import functools

    original = serializers.CharField.to_representation

    @functools.wraps(original)
    def shout(self, value):
        return original(self, value).upper()

    build = lambda: BookAllSerializer(Book.objects.get(pk=library["b1"].pk))  # noqa: E731
    with _patched(serializers.CharField, "to_representation", shout):
        assert parity(build)["title"] == "DUNE"
        assert plans_by_key(build())["title"][2] == "generic"


def test_patch_made_before_import():
    """A patch applied before drf_fast_output is first imported is honoured."""
    import subprocess
    import sys
    from pathlib import Path

    script = """
import os
os.environ["DJANGO_SETTINGS_MODULE"] = "tests.settings"
import django
django.setup()
from types import SimpleNamespace
from rest_framework import serializers
serializers.CharField.to_representation = lambda self, value: "PATCHED"
from drf_fast_output import FastSerializer
class S(FastSerializer):
    name = serializers.CharField()
print(S(SimpleNamespace(name="x")).data["name"])
"""
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "PATCHED"


# --------------------------------------------------------------------------
# Edge cases where the fast path used to differ from DRF
# --------------------------------------------------------------------------


def test_lazy_proxy_values_are_converted(parity):
    """SimpleLazyObject reports the wrapped type as __class__; DRF still converts it."""

    class S(FastSerializer):
        name = serializers.CharField()
        flag = serializers.BooleanField()
        color = serializers.ChoiceField(choices=["red", "green"])

    obj = SimpleNamespace(
        name=SimpleLazyObject(lambda: "hello"),
        flag=SimpleLazyObject(lambda: True),
        color=SimpleLazyObject(lambda: "red"),
    )
    assert parity(lambda: S(obj)) == {"name": "hello", "flag": True, "color": "red"}


class _Blankable(enum.Enum):
    BLANK = ""
    RED = "red"


@pytest.mark.parametrize("value", ["", "red", _Blankable.BLANK, _Blankable.RED])
def test_choice_empty_string_with_enum_choices(parity, value):
    """DRF returns '' before the lookup, which here maps '' to the enum member."""

    class S(FastSerializer):
        color = serializers.ChoiceField(choices=_Blankable)  # pyrefly: ignore[bad-argument-type]

    parity(lambda: S(SimpleNamespace(color=value)))


def test_instance_pk_only_optimization_override(library, parity):
    """publisher_code: a PrimaryKeyRelatedField over an FK to a to_field, whose
    raw column ("PNG") is not the related pk."""

    def build():
        ser = BookRichSerializer(RICH_QS().get(pk=library["b1"].pk))
        ser.fields["publisher_code"].use_pk_only_optimization = lambda: False  # pyrefly: ignore[missing-attribute]
        return ser

    assert parity(build)["publisher_code"] == library["b1"].publisher.pk


def test_instance_enforce_timezone_override(library, parity):
    tokyo = zoneinfo.ZoneInfo("Asia/Tokyo")

    def build():
        ser = BookAllSerializer(Book.objects.get(pk=library["b1"].pk))
        ser.fields["created"].enforce_timezone = lambda v: v.astimezone(tokyo)  # pyrefly: ignore[missing-attribute]
        return ser

    assert parity(build)["created"].endswith("+09:00")


def test_deferred_field_of_deleted_row(library, parity):
    """DRF maps ObjectDoesNotExist to None in get_attribute, but not in the
    pk-only read of a PrimaryKeyRelatedField (serializable_value)."""

    class Columns(FastModelSerializer):
        class Meta:
            model = Book
            fields = ["id", "title", "pages"]

    class WithFk(FastModelSerializer):
        class Meta:
            model = Book
            fields = ["id", "author"]

    obj = Book.objects.only("id").get(pk=library["b1"].pk)
    Book.objects.filter(pk=obj.pk).delete()
    assert parity(lambda: Columns(obj)) == {"id": obj.pk, "title": None, "pages": None}
    with pytest.raises(Book.DoesNotExist):
        WithFk(obj).data
    with stock_drf(), pytest.raises(Book.DoesNotExist):
        WithFk(obj).data


def test_prefetch_cache_replaced_mid_row(library, parity):
    """A method field that refreshes the instance replaces its prefetch cache."""
    t1, t2 = library["tags"]

    class S(FastModelSerializer):
        reset = serializers.SerializerMethodField()

        class Meta:
            model = Book
            fields = ["id", "reset", "tags"]

        def get_reset(self, obj):
            Book.tags.through.objects.filter(book=obj, tag=t2).delete()
            obj.refresh_from_db()
            return 0

    build = lambda: S(Book.objects.prefetch_related("tags").get(pk=library["b1"].pk))  # noqa: E731
    assert parity(build)["tags"] == [t1.pk]


def test_bind_that_changes_source_attrs(library, parity):
    class TitleField(serializers.CharField):
        def bind(self, field_name, parent):
            super().bind(field_name, parent)
            self.source_attrs = ["title"]

    class S(FastModelSerializer):
        label = TitleField(source="pages")

        class Meta:
            model = Book
            fields = ["id", "label"]

    assert parity(lambda: S(Book.objects.get(pk=library["b1"].pk)))["label"] == "Dune"


def test_patched_list_serializer_after_compile(library, parity):
    """Nested (note_set: a FastListSerializer) and top-level many=True."""
    from tests.testapp.serializers import NoteSerializer

    nested = lambda: BookRichSerializer(RICH_QS().get(pk=library["b1"].pk))  # noqa: E731
    top = lambda: NoteSerializer(Note.objects.all(), many=True)  # noqa: E731
    parity(nested)
    parity(top)
    with _patched(serializers.ListSerializer, "to_representation", lambda self, data: ["PATCHED"]):
        assert parity(nested)["note_set"] == ["PATCHED"]
        assert parity(top) == ["PATCHED"]
    assert parity(nested)["note_set"] != ["PATCHED"]


def test_empty_list_does_not_build_child_fields(db):
    """Stock DRF never touches the child's fields for an empty list."""
    calls = []

    class S(FastModelSerializer):
        class Meta:
            model = Book
            fields = ["id"]

        def get_fields(self):
            calls.append(1)
            return super().get_fields()

    assert S([], many=True).data == []
    assert S(Book.objects.none(), many=True).data == []
    assert calls == []
    with stock_drf():
        assert S([], many=True).data == []
    assert calls == []


def test_disabling_applies_to_already_compiled_instances(library):
    b1 = library["b1"]
    single, many = BookAllSerializer(b1), BookAllSerializer([b1], many=True)
    single.data, many.data  # compile both
    shout = lambda self, value: "PATCHED"  # noqa: E731  # only stock DRF calls the class method now
    with (
        override_settings(DRF_FAST_OUTPUT={"ENABLED": False}),
        _patched(serializers.CharField, "to_representation", shout),
    ):
        assert single.to_representation(b1)["title"] == "PATCHED"
        assert many.to_representation([b1])[0]["title"] == "PATCHED"
    assert single.to_representation(b1)["title"] == "Dune"


def test_enabling_applies_to_instances_first_used_while_disabled(library):
    ser = BookAllSerializer(library["b1"])
    with override_settings(DRF_FAST_OUTPUT={"ENABLED": False}):
        ser.to_representation(library["b1"])
        assert "(not inlined" in explain(ser)
    assert "_fast_output_compiled" not in ser.__dict__
    ser.to_representation(library["b1"])
    assert "_fast_output_compiled" in ser.__dict__


def test_explain_instance_override(library):
    from tests.testapp.serializers import NoteSerializer

    single = BookAllSerializer(library["b1"])
    single.to_representation = lambda instance: {}
    many = NoteSerializer(Note.objects.all(), many=True)
    many.child.to_representation = lambda instance: {}  # pyrefly: ignore[missing-attribute]
    for ser in (single, many):
        assert "(not inlined" in explain(ser)


def test_verify_compares_rows_as_they_are_consumed(db):
    class S(FastSerializer):
        x = serializers.IntegerField()

    def rows():  # reuses one mutable row
        buf = {"x": 0}
        yield buf
        buf["x"] = 1
        yield buf

    expected = [{"x": 0}, {"x": 1}]
    with stock_drf():
        assert S(rows(), many=True).data == expected
    assert S(rows(), many=True).data == expected
    with override_settings(DRF_FAST_OUTPUT={"VERIFY": True}):
        assert S(rows(), many=True).data == expected


@override_settings(DRF_FAST_OUTPUT={"VERIFY": True})
def test_verify_reports_the_failing_row(library, monkeypatch):
    from drf_fast_output import compiler

    monkeypatch.setitem(compiler._CONV, "str", ["{v} = 'wrong'"])
    monkeypatch.setattr(compiler, "_factory_cache", {})
    monkeypatch.setattr(compiler, "_template_cache", {})
    with pytest.raises(AssertionError, match=r"BookAllSerializer\(many=True\)\[0\]"):
        BookAllSerializer(Book.objects.order_by("id"), many=True).data


# --------------------------------------------------------------------------
# Attribute paths (`path` and `self` access)
# --------------------------------------------------------------------------


class _Thing:
    """Every kind of value DRF's get_attribute can meet along a source path."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    @property
    def prop(self):
        return "p"

    def simple(self):
        return "called"

    def with_default(self, x=1):
        return x + 1

    def needs_arg(self, x):  # not simple: DRF returns the bound method itself
        return x

    def raises_attr(self):
        raise AttributeError("inner")

    def raises_key(self):
        raise KeyError("inner")

    def raises_odne(self):
        raise Author.DoesNotExist()

    @property
    def odne_prop(self):  # from getattr: DRF maps it to None
        raise Author.DoesNotExist()

    @staticmethod
    def static():
        return "static"


class PathSerializer(FastSerializer):
    prop = serializers.CharField()
    simple = serializers.CharField()
    with_default = serializers.IntegerField()
    static = serializers.CharField()
    deep = serializers.CharField(source="child.child.prop", allow_null=True)
    deep_call = serializers.CharField(source="child.simple")
    deep_key = serializers.CharField(source="data.inner.key")
    odne = serializers.CharField(source="child.odne_prop")
    missing_default = serializers.CharField(source="child.nope", default="dflt")
    missing_null = serializers.CharField(source="child.nope", allow_null=True)
    missing_skip = serializers.CharField(source="child.nope", required=False)
    whole = serializers.ReadOnlyField(source="*")
    level = serializers.ChoiceField(choices=[(1, "one"), (2, "two")])


def _things():
    leaf = _Thing()
    return [
        _Thing(child=_Thing(child=leaf), data={"inner": {"key": "k"}}, level=1),
        _Thing(child=_Thing(child=None), data={"inner": {"key": 5}}, level=3),  # None mid-path
        _Thing(child=_Thing(child=leaf), data={"inner": {"key": "k"}}, level=True),  # bool is not int
    ]


def test_paths_match_drf(parity):
    def build():
        ser = PathSerializer(_things(), many=True)
        child = ser.child  # pyrefly: ignore[missing-attribute]
        child.fields["whole"].to_representation = lambda value: type(value).__name__
        return ser

    out = parity(build)
    assert out[0]["deep"] == "p"
    assert out[0]["deep_call"] == "called"
    assert out[0]["deep_key"] == "k"
    assert out[0]["odne"] is None
    assert out[0]["missing_default"] == "dflt"
    assert out[0]["missing_null"] is None
    assert "missing_skip" not in out[0]
    assert out[0]["level"] == 1
    assert out[1]["deep"] is None


def test_paths_plan():
    plans = plans_by_key(PathSerializer(_Thing()))
    assert plans["deep"] == ("path", None, "str")
    assert plans["level"] == ("path", None, "choice")
    assert plans["whole"] == ("self", None, "raw")


def test_path_to_method_with_required_arg(parity):
    class S(FastSerializer):
        m = serializers.ReadOnlyField(source="child.needs_arg")

    def build():
        ser = S(_Thing(child=_Thing()))
        ser.fields["m"].to_representation = lambda value: value.__name__
        return ser

    assert parity(build) == {"m": "needs_arg"}


@pytest.mark.parametrize(
    ("source", "data", "exc"),
    [
        ("child.raises_attr", _Thing(child=_Thing()), ValueError),  # wrapped, not treated as missing
        ("child.raises_key", _Thing(child=_Thing()), ValueError),
        ("child.nope", _Thing(child=_Thing()), AttributeError),  # missing attribute, required
        ("child.nope", {"child": {}}, KeyError),  # missing key, required
        ("child.upper", _Thing(child="text"), BuiltinSignatureError),  # builtin method
    ],
)
def test_path_errors_match_drf(source, data, exc):
    class S(FastSerializer):
        v = serializers.CharField(source=source)

    with pytest.raises(exc) as fast_exc:
        S(data).data
    with stock_drf(), pytest.raises(exc) as drf_exc:
        S(data).data
    assert type(fast_exc.value) is type(drf_exc.value)
    assert str(fast_exc.value) == str(drf_exc.value)


def test_path_odne_from_a_called_method_propagates_like_drf():
    class S(FastSerializer):
        v = serializers.CharField(source="child.raises_odne", allow_null=True)

    obj = _Thing(child=_Thing())
    with pytest.raises(Author.DoesNotExist):
        S(obj).data
    with stock_drf(), pytest.raises(Author.DoesNotExist):
        S(obj).data


def test_path_function_patched_after_compile(parity):
    """The simple-callable cache is per function, so a replaced method is re-checked."""

    class S(FastSerializer):
        v = serializers.ReadOnlyField(source="child.simple")

    obj = _Thing(child=_Thing())
    assert parity(lambda: S(obj)) == {"v": "called"}
    with mock.patch.object(_Thing, "simple", lambda self, x: x):

        def build():
            ser = S(obj)
            ser.fields["v"].to_representation = lambda value: callable(value)
            return ser

        assert parity(build) == {"v": True}  # not simple any more: not called


def test_path_through_model_relations(library, parity):
    """Dotted sources over select_related FKs and a reverse one-to-one."""

    class S(FastModelSerializer):
        author_name = serializers.CharField(source="author.name")
        author_bio = serializers.CharField(source="author.profile.bio", allow_null=True)
        editor_name = serializers.CharField(source="editor.name", allow_null=True)
        status_display = serializers.CharField(source="author.__str__")

        class Meta:
            model = Book
            fields = ["id", "author_name", "author_bio", "editor_name", "status_display"]

    out = parity(lambda: S(RICH_QS().order_by("id"), many=True))
    assert out[0]["author_bio"] == "Writes things"
    assert out[1]["author_bio"] is None  # Bob has no profile: ObjectDoesNotExist


def test_int_choices(parity):
    class Level(enum.IntEnum):
        ONE = 1
        TWO = 2

    class S(FastSerializer):
        a = serializers.ChoiceField(choices=[(1, "one"), (2, "two")])
        b = serializers.ChoiceField(choices=[("1", "one")])
        c = serializers.ChoiceField(choices=Level)  # pyrefly: ignore[bad-argument-type]

    rows = [SimpleNamespace(a=v, b=v, c=v) for v in (1, 2, 0, 7, -1, Level.ONE, True)]
    parity(lambda: S(rows, many=True))


def test_mapping_check_is_per_class(parity):
    """A cached answer for a base class isn't reused for a subclass that is a Mapping."""
    from collections.abc import Mapping

    class Base:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Sub(Base):
        def __getitem__(self, key):
            return f"item-{key}"

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

    Mapping.register(Sub)  # pyrefly: ignore[missing-attribute]

    class S(FastSerializer):
        name = serializers.CharField()

    assert parity(lambda: S(Base(name="attr"))) == {"name": "attr"}
    assert parity(lambda: S(Sub(name="attr"))) == {"name": "item-name"}


# --------------------------------------------------------------------------
# Datetime mode shared with serializers that aren't inlined
# --------------------------------------------------------------------------


class _WhenStock(serializers.Serializer):
    at = serializers.DateTimeField()


class _WhenOverride(FastSerializer):
    at = serializers.DateTimeField()

    def to_representation(self, instance):
        return {**super().to_representation(instance), "custom": True}


class _Outer(FastSerializer):
    at = serializers.DateTimeField()
    inner = _WhenOverride(source="*")


@pytest.mark.parametrize("tz", ["UTC", "America/New_York"])
def test_not_inlined_children_share_the_datetime_mode(parity, tz, monkeypatch):
    from drf_fast_output import compiler

    rows = [{"at": dt.datetime(2024, 1, 1, i, tzinfo=dt.timezone.utc)} for i in range(5)]
    calls = []
    real = compiler._current_datetime_mode
    monkeypatch.setattr(compiler, "_current_datetime_mode", lambda: calls.append(1) or real())
    with timezone.override(zoneinfo.ZoneInfo(tz)):
        _Outer(rows, many=True).data
        assert len(calls) == 1  # once for the list, not per row or per nested serializer
        _WhenOverride(rows, many=True).data  # a child that isn't inlineable
        assert len(calls) == 2
        out = parity(lambda: _Outer(rows, many=True))
        parity(lambda: _WhenOverride(rows, many=True))
    assert out[0]["inner"]["custom"] is True


def test_datetime_mode_is_reset_after_the_call(monkeypatch):
    from drf_fast_output.compiler import active_dtm

    rows = [{"at": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)}]
    _Outer(rows, many=True).data
    _Outer(rows[0]).data
    assert active_dtm.get() is None
    with timezone.override(zoneinfo.ZoneInfo("Asia/Tokyo")):
        assert _Outer(rows[0]).data["at"] == "2024-01-01T09:00:00+09:00"
