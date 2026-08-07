"""Source adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from scraper.fetch import Fetcher


class SourceAdapter(ABC):
    site: str = "unknown"

    def __init__(self, category: str = "all") -> None:
        """Initialise the adapter.

        Args:
            category: Listing category hint (``"residential"``, ``"commercial"``,
                ``"all"``).  Most adapters ignore this and store it as a no-op;
                only ``Acress100Adapter`` uses it to select listing seed URLs.
        """
        self.category = category

    @abstractmethod
    def discover(self, fetcher: Fetcher, max_projects: int | None = None) -> list[str]:
        """Return project page URLs."""

    @abstractmethod
    def fetch_project(self, fetcher: Fetcher, url: str) -> list[dict[str, Any]]:
        """Return schema-shaped unit records (may include verification extras)."""
