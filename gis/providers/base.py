"""Data provider abstraction (spec section 5).

Providers are NOT allowed to invent datasets: search() must only return
datasets the provider can actually resolve/download.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class DatasetInfo:
    """A dataset record returned by provider.search()."""

    id: str
    title: str
    provider: str
    source: str
    category: str                 # DEM | SATELLITE | LULC | ADMIN | HYDRO | ...
    resolution: str               # e.g. "30 m"
    crs: str                      # native CRS
    units: Optional[str] = None   # e.g. "metres" for DEM vertical units
    date: Optional[str] = None    # acquisition/publication date if known
    license: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class DataProvider(ABC):
    """Standardised interface every data provider implements."""

    provider_name: str = "abstract"
    category: str = "generic"

    @abstractmethod
    def search(self, aoi: dict, parameters: Optional[dict] = None) -> list[DatasetInfo]:
        """Return datasets available for the given prepared AOI."""

    @abstractmethod
    def download(self, dataset: DatasetInfo, dest_dir: str, aoi: Optional[dict] = None) -> str:
        """Download/resolve the dataset for the AOI. Returns local file path."""

    @abstractmethod
    def validate(self, path: str) -> dict:
        """Validate the downloaded file. Returns {'ok': bool, 'checks': [...]}."""

    @abstractmethod
    def metadata(self, path: str) -> dict:
        """Return full provenance metadata for the dataset file."""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} category={self.category}>"
