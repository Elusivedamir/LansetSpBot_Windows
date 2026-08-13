"""Composable, side-effect-free ordering for comment dispatch policy checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class CommentDispatchPolicy:
    name: str
    check: Callable[[], bool]


def evaluate_comment_dispatch_policies(
    policies: Iterable[CommentDispatchPolicy],
) -> bool:
    """Return False on the first rejected policy; propagate policy exceptions."""

    for policy in policies:
        if policy.check() is not True:
            return False
    return True
