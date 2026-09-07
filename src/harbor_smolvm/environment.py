"""Compatibility names for the canonical Harbor provider in smolmachines."""

from __future__ import annotations

from typing import Any

from smol.harbor import SmolEnvironment, close_harbor_goldens


class SmolvmEnvironment(SmolEnvironment):
    """Backwards-compatible local provider name."""

    @staticmethod
    def type() -> str:
        return "smolvm"


class SmolvmCloudEnvironment(SmolEnvironment):
    """Backwards-compatible cloud provider name."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("target", "cloud")
        super().__init__(*args, **kwargs)

    @staticmethod
    def type() -> str:
        return "smolvm-cloud"


__all__ = [
    "SmolvmCloudEnvironment",
    "SmolvmEnvironment",
    "close_harbor_goldens",
]
