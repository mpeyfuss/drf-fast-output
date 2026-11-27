import datetime as dt
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from drf_fast_output import strict_equal
from drf_fast_output.compiler import stock_drf
from tests.testapp.models import Author, Book, Chapter, Note, Profile, Publisher, SpecialBook, Tag


def run_both(build):
    """
    Build a fresh serializer with ``build()`` twice: once on the fast path and
    once forcing stock DRF. Assert identical output (types and key order too)
    and that the fast path never issues more queries. Returns the fast output.
    """
    with CaptureQueriesContext(connection) as fast_q:
        fast = build().data
    with stock_drf(), CaptureQueriesContext(connection) as drf_q:
        drf = build().data
    assert strict_equal(fast, drf), f"\nfast: {fast!r}\ndrf:  {drf!r}"
    assert len(fast_q) <= len(drf_q), (len(fast_q), len(drf_q))
    return fast


@pytest.fixture
def parity():
    return run_both


@pytest.fixture
def library(db):
    utc = dt.timezone.utc
    alice = Author.objects.create(name="Alice", email="a@example.com", born=dt.date(1980, 5, 1))
    bob = Author.objects.create(name="Bob")
    Profile.objects.create(author=alice, bio="Writes things")
    pub = Publisher.objects.create(code="PNG", name="Penguin")
    t1, t2 = Tag.objects.create(slug="sci-fi"), Tag.objects.create(slug="classic")

    b1 = Book.objects.create(
        title="Dune",
        pages=412,
        price=Decimal("9.99"),
        weight=1.5,
        in_print=True,
        is_featured=True,
        status=Book.Status.PUBLISHED,
        rating=Book.Rating.GOOD,
        published=dt.date(1965, 8, 1),
        created=dt.datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=utc),
        start_time=dt.time(9, 30),
        duration=dt.timedelta(hours=2),
        meta={"a": [1, 2]},
        author=alice,
        editor=bob,
        publisher=pub,
    )
    b1.tags.set([t1, t2])
    b1.readers.set([bob, alice])
    Chapter.objects.create(book=b1, number=2, title="Two")
    Chapter.objects.create(book=b1, number=1, title="One")
    Note.objects.create(book=b1, text="great")

    b2 = Book.objects.create(
        title="Empty",
        pages=0,
        price=Decimal("0"),
        created=dt.datetime(2024, 6, 1, tzinfo=utc),
        author=bob,
        in_print=False,
        meta=None,
    )
    SpecialBook.objects.create(
        title="Special",
        pages=10,
        price=Decimal("1.50"),
        created=dt.datetime(2020, 1, 1, tzinfo=utc),
        author=alice,
        edition="first",
    )
    return {"alice": alice, "bob": bob, "b1": b1, "b2": b2, "tags": [t1, t2]}
