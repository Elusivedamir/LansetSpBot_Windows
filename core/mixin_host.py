from __future__ import annotations

from typing import Any, Protocol


class MixinHost(Protocol):
    """Static-only host contract for cooperative mixins.

    Concrete facades provide the attributes and sibling methods. The dynamic
    fallback keeps each focused mixin independently type-checkable without
    adding runtime base classes to the production MRO.
    """

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)
