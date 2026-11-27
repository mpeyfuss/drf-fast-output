import pytest
from rest_framework import generics, serializers, viewsets
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from drf_fast_output import FastListSerializer, FastOutputMixin, fast_output
from drf_fast_output.views import FastOutputViewMixin
from tests.testapp.models import Book
from tests.testapp.serializers import PlainBookSerializer

pytestmark = pytest.mark.django_db
factory = APIRequestFactory()


def test_fast_output_factory():
    Fast = fast_output(PlainBookSerializer)
    assert Fast is fast_output(PlainBookSerializer)
    assert fast_output(Fast) is Fast
    assert Fast.__name__ == "PlainBookSerializer"
    assert Fast.Meta is PlainBookSerializer.Meta
    assert issubclass(Fast, PlainBookSerializer)
    assert issubclass(Fast, FastOutputMixin)
    assert isinstance(Fast(many=True), FastListSerializer)
    with pytest.raises(TypeError):
        fast_output(Book)


def test_fast_output_respects_custom_list_serializer():
    class MyList(serializers.ListSerializer):
        pass

    class S(serializers.Serializer):
        x = serializers.IntegerField()

        class Meta:
            list_serializer_class = MyList

    assert type(fast_output(S)(many=True)) is MyList
    assert fast_output(S)([{"x": 1}], many=True).data == [{"x": 1}]


def test_fast_output_on_overridden_to_representation():
    class S(serializers.Serializer):
        x = serializers.IntegerField()

        def to_representation(self, instance):
            return {"custom": instance["x"]}

    assert fast_output(S)({"x": 1}).data == {"custom": 1}
    assert fast_output(S)([{"x": 1}], many=True).data == [{"custom": 1}]


class BookList(FastOutputViewMixin, generics.ListCreateAPIView):
    queryset = Book.objects.select_related("author__profile").order_by("id")
    serializer_class = PlainBookSerializer
    authentication_classes = ()
    permission_classes = ()


class BookViewSet(FastOutputViewMixin, viewsets.ModelViewSet):
    queryset = Book.objects.select_related("author__profile").order_by("id")
    serializer_class = PlainBookSerializer
    authentication_classes = ()
    permission_classes = ()


def test_view_mixin_swaps_only_for_reads(library):
    view = BookList()
    view.request = view.initialize_request(factory.get("/"))
    assert issubclass(view.get_serializer_class(), FastOutputMixin)
    view.request = view.initialize_request(factory.post("/"))
    assert view.get_serializer_class() is PlainBookSerializer
    view.request = view.initialize_request(factory.get("/"))
    view.swagger_fake_view = True  # pyrefly: ignore[missing-attribute]  # set by schema generators
    assert view.get_serializer_class() is PlainBookSerializer


def test_view_list_matches_stock(library):
    fast = BookList.as_view()(factory.get("/"))
    stock = generics.ListAPIView.as_view(
        queryset=BookList.queryset,
        serializer_class=PlainBookSerializer,
        authentication_classes=[],
        permission_classes=[],
    )(factory.get("/"))
    assert isinstance(fast, Response)
    assert isinstance(stock, Response)
    fast.render(), stock.render()
    assert fast.status_code == 200
    assert fast.content == stock.content


def test_viewset_retrieve(library):
    resp = BookViewSet.as_view({"get": "retrieve"})(factory.get("/"), pk=library["b1"].pk)
    assert resp.status_code == 200
    assert resp.data["author"]["profile"]["bio"] == "Writes things"


def test_fast_output_from_a_subclass_hook():
    """Creating the wrapper runs __init_subclass__, which may wrap another serializer."""
    import threading

    class Other(serializers.Serializer):
        x = serializers.CharField()

    class Base(serializers.Serializer):
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)
            if issubclass(cls, FastOutputMixin):
                cls.related = fast_output(Other)

    class Mine(Base):
        y = serializers.CharField()

    result = []
    t = threading.Thread(target=lambda: result.append(fast_output(Mine)), daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "fast_output() deadlocked"
    assert result[0] is fast_output(Mine)
    assert result[0].related is fast_output(Other)
