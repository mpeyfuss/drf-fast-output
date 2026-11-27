"""Automatic single-object fallback and nested-list dispatch."""

from unittest.mock import patch

import pytest
from django.test import override_settings
from rest_framework import serializers

from drf_fast_output import FastListSerializer, FastSerializer, compiler, fast_output


class Item(serializers.Serializer):
    name = serializers.CharField()


class Group(serializers.Serializer):
    items = Item(many=True)


class Detail(FastSerializer):
    group = Group()


def test_single_skips_compiler():
    with patch.object(compiler, "get_row", side_effect=AssertionError("compiled")):
        assert fast_output(Item)({"name": "one"}).data == {"name": "one"}


def test_nested_plain_lists_stay_fast():
    data = {"group": {"items": [{"name": str(i)} for i in range(200)]}}
    ser = Detail(data)
    with patch.object(compiler, "get_row", wraps=compiler.get_row) as compile_row:
        assert ser.data == data
    group = ser.fields["group"]
    assert isinstance(group, serializers.Serializer)
    items = group.fields["items"]
    assert isinstance(items, FastListSerializer)
    child = items.child
    assert compile_row.call_args_list == [((child,),)]
    assert "_fast_output_compiled" not in ser.__dict__
    # Class declarations and other DRF instances must remain untouched.
    assert type(Group().fields["items"]) is serializers.ListSerializer
    assert type(Detail._declared_fields["group"]) is Group


def test_force_single_compilation():
    class Forced(FastSerializer):
        fast_output_single = True
        name = serializers.CharField()

    ser = Forced({"name": "one"})
    assert ser.data == {"name": "one"}
    assert "_fast_output_compiled" in ser.__dict__


@pytest.mark.parametrize("count", [0, 1, 9, 10])
def test_nested_list_threshold(count):
    data = {"group": {"items": [{"name": str(i)} for i in range(count)]}}
    with patch.object(compiler, "get_row", wraps=compiler.get_row) as compile_row:
        assert Detail(data).data == data
    assert compile_row.call_count == (1 if count >= 10 else 0)


def test_nested_generator_is_not_consumed_ahead():
    ser = Detail()
    seen = []

    def rows():
        yield {"name": "first"}
        seen.append("advanced")
        yield {"name": "second"}

    real_get_row = compiler.get_row

    def compile_row(child):
        assert seen == []
        return real_get_row(child)

    with patch.object(compiler, "get_row", side_effect=compile_row):
        assert ser.to_representation({"group": {"items": rows()}}) == {
            "group": {"items": [{"name": "first"}, {"name": "second"}]}
        }
    assert seen == ["advanced"]


def test_custom_list_is_preserved():
    class CustomList(serializers.ListSerializer):
        def to_representation(self, data):
            return ["custom"]

    class CustomItem(Item):
        class Meta:
            list_serializer_class = CustomList

    class Parent(FastSerializer):
        items = CustomItem(many=True)

    assert Parent({"items": [{"name": "one"}]}).data == {"items": ["custom"]}
    assert type(Parent().fields["items"]) is CustomList


def test_opted_out_parent_does_not_adapt_fields():
    class OptedOut(Detail):
        class Meta:
            fast_output = False

    assert type(OptedOut().fields["group"]) is Group


def test_disabled_nested_lists_do_not_compile():
    ser = Detail({"group": {"items": [{"name": "one"}]}})
    group = ser.fields["group"]
    assert isinstance(group, serializers.Serializer)
    assert isinstance(group.fields["items"], FastListSerializer)
    with (
        override_settings(DRF_FAST_OUTPUT={"ENABLED": False}),
        patch.object(compiler, "get_row", side_effect=AssertionError("compiled")),
    ):
        assert ser.data == {"group": {"items": [{"name": "one"}]}}
