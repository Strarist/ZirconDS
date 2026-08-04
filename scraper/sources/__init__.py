"""Registered scrape source adapters."""

from scraper.sources.acress100 import Acress100Adapter
from scraper.sources.housing import HousingAdapter
from scraper.sources.magicbricks import MagicBricksAdapter

__all__ = [
    "Acress100Adapter",
    "HousingAdapter",
    "MagicBricksAdapter",
]
