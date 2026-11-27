from __future__ import annotations

from collections.abc import Collection
from typing import Any

from rest_framework.permissions import SAFE_METHODS

from .serializers import fast_output

__all__ = ["FastOutputViewMixin"]


class FastOutputViewMixin:
    """
    For ``GenericAPIView`` subclasses (generics, viewsets): serve read requests
    with a fast-output version of ``get_serializer_class()``. Writes keep the
    original class untouched::

        class BookViewSet(FastOutputViewMixin, viewsets.ModelViewSet):
            serializer_class = BookSerializer
    """

    fast_output_methods: Collection[str] = SAFE_METHODS

    def get_serializer_class(self) -> Any:
        cls = super().get_serializer_class()  # type: ignore[misc]
        request = getattr(self, "request", None)
        if (
            request is not None
            # Schema generators (drf-spectacular, drf-yasg) inspect the class;
            # hand them the original so component names don't collide.
            and not getattr(self, "swagger_fake_view", False)
            and request.method in self.fast_output_methods
        ):
            return fast_output(cls)
        return cls
