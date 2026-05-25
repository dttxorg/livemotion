"""Headless Gooey compatibility shim for MotionPhoto2 CLI use in Docker.

MotionPhoto2 imports GooeyParser even when it is used from the command line.
The real Gooey package depends on wxPython/GUI libraries, which are unnecessary
and heavy in a NAS container. This shim keeps the argparse-compatible subset
that MotionPhoto2 needs for CLI execution.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def _strip_gooey_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    kwargs.pop("gooey_options", None)
    kwargs.pop("widget", None)
    if kwargs.get("action") in {"store_true", "store_false"}:
        kwargs.pop("metavar", None)
    return kwargs


def Gooey(*_args: Any, **_kwargs: Any) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        return func

    return decorator


class GooeyParser(argparse.ArgumentParser):
    def add_argument(self, *args: Any, **kwargs: Any) -> argparse.Action:
        return super().add_argument(*args, **_strip_gooey_kwargs(kwargs))

    def add_argument_group(self, *args: Any, **kwargs: Any) -> argparse._ArgumentGroup:  # noqa: SLF001 - argparse exposes this return type.
        group = super().add_argument_group(*args, **_strip_gooey_kwargs(kwargs))
        original_add_argument = group.add_argument

        def add_argument(*inner_args: Any, **inner_kwargs: Any) -> argparse.Action:
            return original_add_argument(*inner_args, **_strip_gooey_kwargs(inner_kwargs))

        group.add_argument = add_argument  # type: ignore[method-assign]
        return group
