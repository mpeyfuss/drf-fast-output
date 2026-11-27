# drf-fast-output

Faster output serialization for Django REST Framework read endpoints, with the
same output as stock DRF.

DRF's `Serializer.to_representation` does a lot of generic work for every field
of every row: `field.get_attribute()` → `fields.get_attribute()` → `getattr`, a
`PKOnlyObject` check, a method call to `field.to_representation()`, and for
method sources (`source="get_status_display"`) an `inspect.signature()` call per
row. This package compiles each serializer into one generated Python function
that does the same thing with that work inlined.

Serialization time only (`benchmarks/bench.py`, Python 3.15, Django 6.1), the
same serializer as plain DRF classes vs fast:

| case | 1 row | 5 rows | 25 rows | 2000 rows |
|---|---|---|---|---|
| `ModelSerializer`, `fields='__all__'` | 0.98x | 1.37x | 2.68x | 6.0x |
| Nested (FK, O2O, reverse FK, M2M, method fields) | 0.95x | 1.33x | 2.5x | 4.4x |
| `fast_output()` on a stock serializer | 0.97x | 1.08x | 1.96x | 5.3x |

The small-row numbers include constructing the serializer. Single objects use
DRF's field loop by default, avoiding compilation setup; a small dispatch and
nested-field adaptation cost remains. Lists use compiled rows. Compiled plans
are shared by serializer instances with the same field configuration, so each
request still builds its DRF fields, checks their configuration, and binds its
own row function. Timings vary by environment and serializer shape.

## Usage

The options below all give the same result. Pick whichever fits your code.

**1. Per serializer**: mix it in before the DRF base class, or use the ready-made bases:

```python
from drf_fast_output import FastOutputMixin, FastModelSerializer

class BookSerializer(FastOutputMixin, serializers.ModelSerializer):
    ...

class AuthorSerializer(FastModelSerializer):  # same thing
    ...
```

**2. Per view, read requests only**: leave your serializers untouched:

```python
from drf_fast_output.views import FastOutputViewMixin

class BookViewSet(FastOutputViewMixin, viewsets.ModelViewSet):
    serializer_class = BookSerializer   # GET/HEAD/OPTIONS get the fast version
```

**3. Wrap a class**:

```python
from drf_fast_output import fast_output

class BookList(generics.ListAPIView):
    serializer_class = fast_output(BookSerializer)  # cached subclass, same name/Meta
```

On the compiled path, nested serializers are compiled into the parent
automatically, even plain DRF ones. Only output changes. Validation,
`is_valid()` and `save()` are stock DRF.

### Single objects

Single-object calls use DRF by default. Nested plain DRF serializer fields are
adapted on the copied field instances so their lists can still use compiled
rows. Already-loaded nested lists with fewer than 10 rows use DRF too; larger
ones compile. This size check only inspects lists, tuples, and populated queryset
caches: it never counts a query or reads ahead in a generator. Unknown-length
nested collections and top-level `many=True` calls use the compiled path.
Custom `list_serializer_class` implementations are left alone.

A detail response containing 200 plain nested dictionary rows measured 2.68x
faster than stock DRF. With three nested rows it measured 0.93x (about 2 µs of
overhead). The threshold is a heuristic, not a guarantee of a speedup.

To force the previous single-object compilation behavior, for example when
reusing a serializer for repeated `to_representation()` calls:

```python
class BookSerializer(FastModelSerializer):
    fast_output_single = True

    class Meta:
        model = Book
        fields = "__all__"
```

`ENABLED=False` and `Meta.fast_output=False` still take precedence. `VERIFY`
checks output where compiled rows run; a single object already using DRF does
not need a second reference pass.

### Write endpoints

Fast serializers work on write endpoints. `is_valid()`, `create()`, `update()`
and `save()` are untouched, and the response after `save()` uses the same
automatic selection described above. The view mixin only switches serializers for reads by default. To
include write responses, set `fast_output_methods`:

```python
class BookViewSet(FastOutputViewMixin, viewsets.ModelViewSet):
    fast_output_methods = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH")
```

A write response is usually a single object, so expect break-even rather than
a speedup.

### Renderers

The output is the same Python objects stock DRF produces, so any renderer
gives the same bytes it would without this package. That includes DRF's
`JSONRenderer`, [drf-orjson-renderer](https://github.com/brianjbuck/drf_orjson_renderer),
and XML or YAML renderers. The two speedups combine: on the nested benchmark
above, `JSONRenderer` takes 7.2 ms to render what this package serializes in
about 42 ms, and `ORJSONRenderer` takes 1.6 ms.

## What gets inlined

Each readable field gets an **access** strategy and a **conversion**:

| access     | used for                                                         |
|------------|------------------------------------------------------------------|
| `attr`     | concrete model columns (`o.title`), FK pks (`o.author_id`)       |
| `fkobj`    | forward FK/O2O to a nested serializer (`o.author`, missing → `None`) |
| `prefetch` | to-many relations: reads Django's prefetch cache when populated  |
| `call`     | model method sources such as `get_FOO_display`                   |
| `method`   | `SerializerMethodField`                                          |
| `self`     | `source="*"`: the instance itself                                |
| `path`     | dotted sources (`author.profile.bio`), properties, annotations, dict keys, and every field of a plain `Serializer`: DRF's `get_attribute` with its per-row checks cached. A missing attribute or key re-reads through the field, so DRF decides default/`None`/skip/error |
| `get`      | fields with their own `get_attribute()` (related fields, ...): DRF's own `field.get_attribute()` |

Conversions are inlined for `CharField` (and Email/Slug/URL/...), `IntegerField`,
`BigIntegerField`, `FloatField`, `BooleanField`, `ChoiceField`, `UUIDField`,
`DateField`, `TimeField`, `DateTimeField` (ISO-8601 output in UTC or with
`USE_TZ=False`), `JSONField`, `ReadOnlyField`, `PrimaryKeyRelatedField`, many-pk
fields, and nested serializers / `many=True`. Anything else, such as `DecimalField`,
custom fields, or non-ISO formats, calls the field's own `to_representation`.

### Correctness rules

- A shortcut is only taken when the field class has **not** overridden the DRF
  method it replaces. A `CharField` subclass with its own `to_representation`
  still gets fast attribute access, but its conversion is its own.
- Serializers that override `to_representation` are honoured. Nested ones are
  called as-is, and top-level ones still get the fast path when they call `super()`.
- Inline conversions are guarded by exact type checks (`type(v) is str`),
  and other values go to the field. For example, a `TextChoices` member or a
  `SimpleLazyObject` goes through the field's `to_representation`.
- Instances that are not of `Meta.model` (dicts, other objects) take DRF's
  path for that row.
- Relations that weren't prefetched, or were prefetched with `to_attr`, take DRF's
  path. The fast path never issues more queries than DRF.
- Custom `list_serializer_class` is left alone. Its children are still fast.

## When not to use it

The output is always correct, but there are cases where it doesn't help, or
where its assumptions don't hold.

**It won't be faster:**

- **Single-object endpoints** (detail views, write responses) now avoid
  compilation by default but retain dispatch and nested-field adaptation
  overhead. The model examples above measured 2–3% slower than stock DRF;
  a tiny dictionary serializer measured 11.8 µs stock versus 13.8 µs here.
  Use a regular DRF serializer when even that overhead matters. Forcing
  `fast_output_single=True` can cost more than it saves on a fresh instance.
- **Very small lists.** Compilation setup may outweigh the per-row savings.
  The five-row dictionary benchmark measured 0.79x. Automatic fallback for
  small, already-loaded nested lists helps detail responses; top-level lists
  continue to compile regardless of size.
- **When serialization isn't the bottleneck.** If the endpoint has N+1 queries
  or slow database calls, fix those first. This package doesn't change what
  gets queried.
- **When most fields fall back to DRF.** Run `explain()`. Fields marked `get`
  or `generic` still go through DRF: `DecimalField`, `DurationField`,
  hyperlinked fields, custom fields, non-ISO date formats, and datetimes in a
  non-UTC timezone when `USE_TZ=True`. A serializer made mostly of these gains
  little. Fields marked `path` read the attribute with DRF's rules but skip its
  per-row type checks; a source that is often missing (an optional annotation)
  still goes through DRF on every miss.
- **Slow `SerializerMethodField`s.** If a method does its own queries or heavy
  work, that cost is unchanged.

**Avoid these patterns, or test them with `VERIFY` on:**

- **Overriding `all()` on a related model's default manager.** For prefetched
  relations the fast path reads Django's prefetch cache directly instead of
  calling `.all()`, so a custom `all()` is skipped. Relations that weren't
  prefetched still call it.
- **Changing a serializer after its first `.data`.** A serializer compiles on
  first use and keeps the result. Swapping `serializer.fields`, field
  formats or choices, or a `get_<name>` method afterwards isn't picked up.
  Changes made in `__init__` are fine.
- **Model subclasses that override `serializable_value()`.** Only `Meta.model`
  itself is checked, not subclass instances.
- **Code that expects DRF's per-row lookups.** A `SerializerMethodField`'s
  method is looked up once per serializer instead of once per row. A model
  method used as a source (`source="get_summary"`, or `author.get_summary`
  along a dotted path) is checked once up front instead of being re-inspected
  on every row. That check is cached per function, so replacing the method is
  noticed, but changing an existing function's signature in place
  (`__signature__`, `__defaults__`) is not.
- **Switching timezones during compiled serialization.** The active timezone is read
  once by the outermost compiled call (a `many=True` call, or a single object
  with `fast_output_single=True`), and
  shared with every serializer it calls, including nested ones that aren't
  inlined, such as serializers with their own `to_representation`. If the input is a generator
  that yields rows inside different `timezone.override()` blocks, all rows are
  rendered in the timezone that was active when serialization started. Stock
  DRF uses each row's. `VERIFY` catches this.

**Other things to know:**

- **`VERIFY` runs compiled serialization twice.** Method fields run twice and
  queries can double, so use it in tests, not production.
- **Compiled code is cached per field configuration, up to 512 entries.**
  Endpoints whose fields are chosen by the client (sparse fieldsets via
  `?fields=`) can produce many configurations. Past the limit, the oldest
  entries are evicted and recompiled when they come back. That is always
  correct, but an endpoint that cycles through more than 512 combinations
  pays the compile cost again and again. The limit isn't configurable.
- **Errors raised inside the generated code have hard-to-read tracebacks.**
  Frames show `<drf_fast_output>` without source lines. Set
  `DRF_FAST_OUTPUT = {"ENABLED": False}` to debug through stock DRF.
- **Mixin order matters.** `FastOutputMixin` must come before the DRF base
  class. The wrong order raises a `TypeError`.
- **It generates Python code with `exec`.** Only field names, prefetch keys and
  validated identifiers go into the generated source, but environments that
  forbid `exec` can't use it.
- **It depends on DRF and Django internals:** `_readable_fields`,
  `PKOnlyObject`, `choice_strings_to_values` and `_prefetched_objects_cache`.
  Run your test suite with `VERIFY` on when you upgrade DRF or Django.

## Settings

```python
DRF_FAST_OUTPUT = {
    "ENABLED": True,   # master switch
    "VERIFY": False,   # compute stock DRF output too and raise on any difference
}
```

Turn on **`VERIFY` in your test settings**. Every compiled serialization is then
compared against stock DRF, checking types and key order as well, so `1` vs `True` or a
reordered dict fails loudly. Lists are compared row by row as they are consumed,
so generators behave as they do without `VERIFY`.

Both settings take effect on the next call, including on serializer instances
that have already been used.

Opt a single serializer out with `Meta.fast_output = False`.

## Debugging

```python
>>> from drf_fast_output import explain
>>> print(explain(BookSerializer(qs, many=True)))
BookSerializer
    id: access=attr id conv=int
    title: access=attr title conv=str
    author: access=fkobj author conv=nested
        id: access=attr id conv=int
        name: access=attr name conv=str
    tags: access=prefetch tags conv=pklist
    status_display: access=call get_status_display conv=str
    price: access=attr price conv=generic
```

Fields with `get` or `generic` are the ones still going through DRF's machinery.

## Development

```bash
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check   # lint + formatting
uv run pyrefly check                              # type check
uv run python benchmarks/bench.py 2000   # large and small result sets
```

Every test in the suite renders through both paths and asserts identical output
and no extra queries. It covers Python 3.10–3.15, Django 4.2–6.1 and DRF 3.15–3.18.

## License

MIT. See [LICENSE](LICENSE).
