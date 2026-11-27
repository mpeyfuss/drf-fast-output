from rest_framework import serializers

from drf_fast_output import FastModelSerializer, FastOutputMixin, FastSerializer

from .models import Author, Book, Chapter, Note, Profile, Tag


# Plain DRF serializers used as nested fields: these get inlined.
class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = ["id", "bio"]


class AuthorSerializer(serializers.ModelSerializer):
    profile = ProfileSerializer(allow_null=True, read_only=True)

    class Meta:
        model = Author
        fields = ["id", "name", "email", "uid", "born", "profile"]


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "slug"]


class ChapterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chapter
        fields = ["id", "number", "title"]


# The fast serializers below that the benchmark times each have a pure-DRF
# twin (*StockSerializer), so the baseline doesn't run through this package.
class NoteStockSerializer(serializers.ModelSerializer):
    class Meta:
        model = Note
        fields = ["id", "text"]


class NoteSerializer(FastOutputMixin, NoteStockSerializer):
    pass


class BookAllStockSerializer(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = "__all__"


class BookAllSerializer(FastOutputMixin, BookAllStockSerializer):
    pass


class BookRichStockSerializer(serializers.ModelSerializer):
    author = AuthorSerializer()
    editor = AuthorSerializer(allow_null=True)
    tags = TagSerializer(many=True)
    chapters = ChapterSerializer(many=True, read_only=True)
    note_set = NoteStockSerializer(many=True, read_only=True)
    reader_ids = serializers.PrimaryKeyRelatedField(source="readers", many=True, read_only=True)
    author_name = serializers.CharField(source="author.name")
    author_bio = serializers.CharField(source="author.profile.bio", allow_null=True)
    author_pk = serializers.IntegerField(source="author_id")
    editor_pk = serializers.PrimaryKeyRelatedField(source="editor", read_only=True)
    publisher_code = serializers.PrimaryKeyRelatedField(source="publisher", read_only=True)
    title_upper = serializers.ReadOnlyField()
    summary = serializers.ReadOnlyField(source="get_summary")
    tag_count = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display")
    created_date = serializers.DateTimeField(source="created", format="%Y-%m-%d")
    price_float = serializers.FloatField(source="price")
    pages_str = serializers.CharField(source="pages")

    class Meta:
        model = Book
        fields = [
            "id",
            "title",
            "pages",
            "price",
            "weight",
            "in_print",
            "is_featured",
            "status",
            "rating",
            "published",
            "created",
            "start_time",
            "duration",
            "meta",
            "uid",
            "author",
            "editor",
            "tags",
            "chapters",
            "note_set",
            "reader_ids",
            "author_name",
            "author_bio",
            "author_pk",
            "editor_pk",
            "publisher_code",
            "title_upper",
            "summary",
            "tag_count",
            "status_display",
            "created_date",
            "price_float",
            "pages_str",
        ]

    def get_tag_count(self, obj):
        return len(obj.tags.all())


class BookRichSerializer(FastOutputMixin, BookRichStockSerializer):
    note_set = NoteSerializer(many=True, read_only=True)  # a fast nested list


class ReaderAuthorSerializer(FastModelSerializer):
    """Reverse M2M with the default accessor (book_set; prefetch key 'book')."""

    book_set = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    books = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = Author
        fields = ["id", "name", "book_set", "books"]


class TagWithBooksSerializer(FastModelSerializer):
    books = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = Tag
        fields = ["id", "slug", "books"]


# Customisations that must be honoured.
class ShoutyCharField(serializers.CharField):
    def to_representation(self, value):
        return str(value).upper() + "!"


class PrefixedGetField(serializers.CharField):
    def get_attribute(self, instance):
        return "x-" + instance.title


class CustomTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["slug"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["custom"] = True
        return data


class CustomBookSerializer(FastModelSerializer):
    title = ShoutyCharField()
    prefixed = PrefixedGetField(read_only=True)
    tags = CustomTagSerializer(many=True)
    added = serializers.SerializerMethodField(method_name="compute_added")

    class Meta:
        model = Book
        fields = ["id", "title", "prefixed", "tags", "added"]

    def compute_added(self, obj):
        return obj.pages + 1

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["overridden"] = True
        return data


class OptedOutSerializer(FastModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title"]
        fast_output = False


class PlainBookSerializer(serializers.ModelSerializer):
    """Stock DRF, used with fast_output() and the view mixin."""

    author = AuthorSerializer()

    class Meta:
        model = Book
        fields = ["id", "title", "author", "tags"]


class DictSerializer(FastSerializer):
    name = serializers.CharField()
    count = serializers.IntegerField(required=False)
    ratio = serializers.FloatField(allow_null=True)
    flag = serializers.BooleanField(default=False)
    when = serializers.DateTimeField()
    day = serializers.DateField(required=False)
    color = serializers.ChoiceField(choices=["red", "green"], required=False)
    nested = AuthorSerializer(required=False)
