"""
Serialization-only benchmark (rows are fetched before timing).

    uv run python benchmarks/bench.py [rows]
"""

import datetime as dt
import os
import sys
import timeit
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

import django

django.setup()

from django.core.management import call_command  # noqa: E402
from rest_framework import serializers  # noqa: E402

from drf_fast_output import fast_output  # noqa: E402
from tests.testapp.models import Author, Book, Chapter, Profile, Tag  # noqa: E402
from tests.testapp.serializers import (  # noqa: E402
    BookAllSerializer,
    BookAllStockSerializer,
    BookRichSerializer,
    BookRichStockSerializer,
    PlainBookSerializer,
)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1000

call_command("migrate", run_syncdb=True, verbosity=0)
authors = [Author.objects.create(name=f"a{i}", born=dt.date(1980, 1, 1)) for i in range(20)]
for a in authors[::2]:
    Profile.objects.create(author=a, bio="bio")
tags = [Tag.objects.create(slug=f"t{i}") for i in range(10)]
now = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
books = Book.objects.bulk_create(
    Book(
        title=f"Book {i}",
        pages=i,
        price=Decimal("9.99"),
        weight=1.0,
        created=now,
        published=dt.date(2020, 1, 1),
        status="published",
        rating=5,
        meta={"k": i},
        author=authors[i % 20],
        editor=authors[(i + 1) % 20],
    )
    for i in range(N)
)
Book.tags.through.objects.bulk_create(Book.tags.through(book=b, tag=tags[j]) for b in books for j in range(3))
Chapter.objects.bulk_create(Chapter(book=b, number=n, title=f"c{n}") for b in books for n in range(3))

flat = list(Book.objects.prefetch_related("tags", "readers"))
rich = list(
    Book.objects.select_related("author__profile", "editor__profile").prefetch_related(
        "tags", "chapters", "note_set", "readers"
    )
)
plain = list(Book.objects.select_related("author__profile").prefetch_related("tags"))


class DictBookSerializer(serializers.Serializer):
    title = serializers.CharField()
    pages = serializers.IntegerField()
    weight = serializers.FloatField()


dict_rows = [{"title": b.title, "pages": b.pages, "weight": b.weight} for b in books]


# (label, plain DRF serializer, fast equivalent, rows)
CASES = [
    ("ModelSerializer, fields='__all__'", BookAllStockSerializer, BookAllSerializer, flat),
    ("Nested (FK, O2O, reverse FK, M2M, methods)", BookRichStockSerializer, BookRichSerializer, rich),
    ("fast_output(stock serializer)", PlainBookSerializer, fast_output(PlainBookSerializer), plain),
    ("Dictionary rows", DictBookSerializer, fast_output(DictBookSerializer), dict_rows),
]


def best(run, number, repeat=5):
    return min(timeit.repeat(run, number=number, repeat=repeat)) / number


def bench(label, drf_cls, fast_cls, rows):
    drf = best(lambda: drf_cls(rows, many=True).data, number=1)
    fast = best(lambda: fast_cls(rows, many=True).data, number=1)
    print(f"{label:<44} drf {drf * 1e3:8.1f} ms   fast {fast * 1e3:8.1f} ms   {drf / fast:5.1f}x")


def bench_small(label, drf_cls, fast_cls, rows, sizes=(1, 5, 25), number=200):
    """Small result sets, including serializer construction and compilation."""
    compiled_cls = type("Compiled", (fast_cls,), {"fast_output_single": True})
    label = label.split(" (")[0]
    for n in sizes:
        if n == 1:
            obj = rows[0]
            drf = best(lambda obj=obj: drf_cls(obj).data, number)
            fast = best(lambda obj=obj: fast_cls(obj).data, number)
            compiled = best(lambda obj=obj: compiled_cls(obj).data, number)
        else:
            items = rows[:n]
            drf = best(lambda items=items: drf_cls(items, many=True).data, number)
            fast = best(lambda items=items: fast_cls(items, many=True).data, number)
            compiled = best(lambda items=items: compiled_cls(items, many=True).data, number)
        print(
            f"{label:<34} n={n:<3} drf {drf * 1e6:6.1f} us   "
            f"auto {fast * 1e6:6.1f} us   compiled {compiled * 1e6:6.1f} us   {drf / fast:5.2f}x"
        )


print(f"{N} rows, Python {sys.version.split()[0]}, Django {django.__version__}")
for case in CASES:
    bench(*case)
print("\nsmall result sets (incl. serializer setup):")
for case in CASES:
    bench_small(*case)


class CollectionSerializer(serializers.Serializer):
    books = DictBookSerializer(many=True)


print("\nsingle object containing a plain DRF nested list:")
for count in (3, 200):
    obj = {"books": [dict_rows[0].copy() for _ in range(count)]}
    bench_small(
        f"Detail with {count} nested rows",
        CollectionSerializer,
        fast_output(CollectionSerializer),
        [obj],
        sizes=(1,),
    )
