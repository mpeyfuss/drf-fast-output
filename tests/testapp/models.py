import uuid

from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)
    uid = models.UUIDField(default=uuid.uuid4)
    born = models.DateField(null=True)

    def __str__(self) -> str:
        return f"Author<{self.name}>"


class Profile(models.Model):
    author = models.OneToOneField(Author, related_name="profile", on_delete=models.CASCADE)
    bio = models.TextField()


class Tag(models.Model):
    slug = models.SlugField(unique=True)


class Publisher(models.Model):
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100)


class Book(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    class Rating(models.IntegerChoices):
        BAD = 1, "Bad"
        GOOD = 5, "Good"

    title = models.CharField(max_length=200)
    pages = models.IntegerField()
    price = models.DecimalField(max_digits=8, decimal_places=2)
    weight = models.FloatField(null=True)
    in_print = models.BooleanField(default=True)
    is_featured = models.BooleanField(null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    rating = models.IntegerField(choices=Rating.choices, null=True)
    published = models.DateField(null=True)
    created = models.DateTimeField()
    start_time = models.TimeField(null=True)
    duration = models.DurationField(null=True)
    meta = models.JSONField(default=dict, null=True)
    uid = models.UUIDField(default=uuid.uuid4)

    author = models.ForeignKey(Author, related_name="books", on_delete=models.CASCADE)
    editor = models.ForeignKey(Author, related_name="edited", null=True, on_delete=models.SET_NULL)
    publisher = models.ForeignKey(
        Publisher, to_field="code", null=True, related_name="books", on_delete=models.SET_NULL
    )
    tags = models.ManyToManyField(Tag, related_name="books", blank=True)
    readers = models.ManyToManyField(Author, blank=True)  # reverse: book_set / "book"

    @property
    def title_upper(self) -> str:
        return self.title.upper()

    def get_summary(self) -> str:
        return f"{self.title} ({self.pages}p)"


class SpecialBook(Book):
    edition = models.CharField(max_length=20)


class Chapter(models.Model):
    book = models.ForeignKey(Book, related_name="chapters", on_delete=models.CASCADE)
    number = models.IntegerField()
    title = models.CharField(max_length=100)

    class Meta:
        ordering = ["number"]


class Note(models.Model):
    book = models.ForeignKey(Book, on_delete=models.CASCADE)  # reverse: note_set
    text = models.CharField(max_length=100)

    class Meta:
        ordering = ["id"]
