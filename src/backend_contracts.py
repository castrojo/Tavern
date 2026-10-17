# backend_contracts.py - Explicit contracts between backend collaborators
# SPDX-License-Identifier: GPL-3.0-or-later
"""Protocols for collaborators that cross the backend boundary.

The backend is composed from mixins, but operations such as package tasks also
need a small part of its state API. Keeping that contract here prevents callers
from reaching into BrewBackend's private CatalogStore-backed fields.
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .package import Package


class PackageStateBackend(Protocol):
    """The backend state operation required by :class:`TaskManager`."""

    def set_package_installed(self, package: "Package", installed: bool) -> None:
        """Record package state in both the model and backend catalog."""
