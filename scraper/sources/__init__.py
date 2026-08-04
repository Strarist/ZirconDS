"""Registered scrape source adapters."""

from scraper.sources.acres99 import Acres99Adapter
from scraper.sources.acress100 import Acress100Adapter
from scraper.sources.housing import HousingAdapter
from scraper.sources.magicbricks import MagicBricksAdapter
from scraper.sources.squareyards import SquareYardsAdapter

__all__ = [
    "Acress100Adapter",
    "Acres99Adapter",
    "HousingAdapter",
    "MagicBricksAdapter",
    "SquareYardsAdapter",
]
