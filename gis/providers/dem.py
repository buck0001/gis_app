"""DEM data provider - Copernicus GLO-30 (30 m global DEM).

Source: Copernicus DEM GLO-30, distributed by ESA on AWS Open Data:
  https://copernicus-dem-30m.s3.amazonaws.com/<tile>/<tile>.tif

* 1 degree x 1 degree tiles, EPSG:4326, 1 arc-second (~30 m)
* Vertical datum: EGM2008 geoid, units = metres
* Derived from TanDEM-X (2011-2015), Copernicus DEM published 2023
* No API key required. Tiles are cached locally (spec section 51).

This module never invents data: if a tile cannot be downloaded/validated the
pipeline fails loudly with a recommended action (spec section 35).
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Optional

import requests

from .base import DataProvider, DatasetInfo

S3_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
TILE_SIZE_DEG = 1.0

DEM_DATASET = DatasetInfo(
    id="copernicus_glo30",
    title="Copernicus DEM GLO-30 (30 m)",
    provider="DEMProvider",
    source="Copernicus DEM GLO-30 (ESA) via AWS Open Data",
    category="DEM",
    resolution="30 m (1 arc-second)",
    crs="EPSG:4326",
    units="metres (vertical, EGM2008 geoid)",
    date="2023 release; TanDEM-X acquisitions 2011-2015",
    license="Free for any use with attribution (ESA Copernicus)",
    url=S3_BASE,
    description=(
        "Global 1 arc-second DSM covering the land surface between 90N and 90S. "
        "Sourced from the Copernicus GLO-30 COG distribution."
    ),
)


class DEMProviderError(RuntimeError):
    pass


def tile_id(lon: float, lat: float) -> str:
    """Tile id for the 1-degree cell containing (lon, lat); named by SW corner."""
    lat_i = math.floor(lat)
    lon_i = math.floor(lon)
    ns = "N" if lat_i >= 0 else "S"
    ew = "E" if lon_i >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_i):02d}_00_{ew}{abs(lon_i):03d}_00_DEM"


def tiles_for_bbox(bbox: list[float]) -> list[str]:
    """All 1-degree tiles intersecting [minx, miny, maxx, maxy] (WGS84)."""
    minx, miny, maxx, maxy = bbox
    # shrink by epsilon so a bbox edge exactly on a tile boundary doesn't add a tile
    eps = 1e-9
    lon0, lon1 = math.floor(minx + eps), math.floor(maxx - eps)
    lat0, lat1 = math.floor(miny + eps), math.floor(maxy - eps)
    out = []
    for lat in range(lat0, lat1 + 1):
        for lon in range(lon0, lon1 + 1):
            out.append(tile_id(lon + 0.5, lat + 0.5))
    return out


def download_tile(tile: str, cache_dir: Path, progress_cb=None) -> Path:
    """Download one COG tile into the cache; reuses valid cached copies."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{tile}.tif"
    if dest.exists() and dest.stat().st_size > 1_000_000:
        try:
            import rasterio
            with rasterio.open(dest) as src:
                if src.width > 0 and src.height > 0:
                    return dest  # cache hit (spec section 51)
        except Exception:
            dest.unlink(missing_ok=True)  # corrupt cache -> redownload

    url = f"{S3_BASE}/{tile}/{tile}.tif"
    tmp = dest.with_suffix(".part")
    try:
        with requests.get(url, stream=True, timeout=120) as resp:
            if resp.status_code != 200:
                raise DEMProviderError(
                    f"DEM tile '{tile}' not available (HTTP {resp.status_code}) at {url}. "
                    "Recommended action: check the AOI is over land and within -90..90 lat."
                )
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if progress_cb and total:
                        progress_cb(tile, done, total)
    except requests.RequestException as exc:
        tmp.unlink(missing_ok=True)
        raise DEMProviderError(
            f"Network error while downloading DEM tile '{tile}': {exc}. "
            "Recommended action: check internet connection and retry."
        ) from exc

    # validate the download opens as a raster
    try:
        import rasterio
        with rasterio.open(tmp) as src:
            if src.width == 0 or src.height == 0:
                raise ValueError("empty raster")
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise DEMProviderError(
            f"Downloaded DEM tile '{tile}' failed validation ({exc}). "
            "Recommended action: retry; if it persists the remote tile may be corrupt."
        ) from exc

    tmp.replace(dest)
    return dest


def mosaic_and_clip(tile_paths: list[Path], aoi_geom_geojson: dict,
                    bbox: list[float], out_path: Path) -> Path:
    """Merge tiles and clip to the AOI geometry (in EPSG:4326)."""
    import rasterio
    from rasterio.merge import merge
    from rasterio.mask import mask
    from shapely.geometry import mapping, shape

    datasets = []
    try:
        for p in tile_paths:
            datasets.append(rasterio.open(str(p)))
        minx, miny, maxx, maxy = bbox
        mosaic, transform = merge(datasets, bounds=(minx, miny, maxx, maxy))
        profile = datasets[0].profile.copy()
        profile.update(
            height=mosaic.shape[1],
            width=mosaic.shape[2],
            transform=transform,
            compress="deflate",
            tiled=True,
            blockxsize=256,
            blockysize=256,
        )
        nodata = datasets[0].nodata
        if nodata is not None:
            profile["nodata"] = nodata
        with rasterio.open(str(out_path), "w", **profile) as dst:
            dst.write(mosaic)
            dst.update_tags(
                SOURCE=DEM_DATASET.source,
                DATASET=DEM_DATASET.id,
                RESOLUTION=DEM_DATASET.resolution,
            )
    finally:
        for d in datasets:
            d.close()

    # clip to exact AOI polygon
    geom = shape(aoi_geom_geojson)
    with rasterio.open(str(out_path)) as src:
        clip_nodata = src.nodata if src.nodata is not None else -9999
        try:
            out_image, out_transform = mask(
                src, [mapping(geom)], crop=True, filled=True,
                nodata=clip_nodata,
            )
        except ValueError as exc:
            raise DEMProviderError(
                f"AOI does not overlap the downloaded DEM tiles: {exc}. "
                "Recommended action: verify the AOI coordinates."
            ) from exc
        profile = src.profile.copy()

    profile.update(
        height=out_image.shape[1],
        width=out_image.shape[2],
        transform=out_transform,
        nodata=clip_nodata,
        compress="deflate",
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    tmp = out_path.with_suffix(".tmp.tif")
    with rasterio.open(str(tmp), "w", **profile) as dst:
        dst.write(out_image)
        dst.update_tags(
            SOURCE=DEM_DATASET.source, DATASET=DEM_DATASET.id,
            PROCESSING="mosaicked_and_clipped_to_aoi",
        )
    tmp.replace(out_path)
    return out_path


class CopernicusDEMProvider(DataProvider):
    """Standard DataProvider implementation for the DEM category."""

    provider_name = "CopernicusDEMProvider"
    category = "DEM"

    def __init__(self, cache_dir: str = "data/cache/dem"):
        self.cache_dir = Path(cache_dir)

    def search(self, aoi: dict, parameters: Optional[dict] = None) -> list[DatasetInfo]:
        tiles = tiles_for_bbox(aoi["bbox"])
        info = DEM_DATASET.to_dict()
        info["extra"] = {"tiles": tiles, "tile_count": len(tiles)}
        return [DatasetInfo(**info)]

    def download(self, dataset: DatasetInfo, dest_dir: str, aoi: Optional[dict] = None,
                 progress_cb=None) -> str:
        """Download tiles for the AOI, mosaic + clip, return local GeoTIFF path."""
        if aoi is None:
            raise DEMProviderError("download() requires a prepared AOI.")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_path = dest_dir / "dem_clip_4326.tif"
        if out_path.exists() and out_path.stat().st_size > 10_000:
            return str(out_path)  # reuse (caching rule)

        tiles = tiles_for_bbox(aoi["bbox"])
        paths = []
        for i, tile in enumerate(tiles):
            def cb(t, done, total, _i=i, _n=len(tiles)):
                if progress_cb:
                    base = 60.0 * _i / max(_n, 1)
                    progress_cb(base + 60.0 * (done / total) / max(_n, 1),
                                f"Downloading DEM tile {_i + 1}/{_n}")
            paths.append(download_tile(tile, self.cache_dir, progress_cb=cb))
        return str(mosaic_and_clip(paths, aoi["geometry"], aoi["bbox"], out_path))

    def validate(self, path: str) -> dict:
        import rasterio
        import numpy as np
        checks = []
        ok = True
        p = Path(path)
        exists = p.exists() and p.stat().st_size > 0
        checks.append({"check": "file_exists", "ok": exists})
        ok &= exists
        if exists:
            with rasterio.open(path) as src:
                crs_ok = src.crs is not None
                checks.append({"check": "crs_present", "ok": crs_ok, "detail": str(src.crs)})
                ok &= crs_ok
                data = src.read(1, masked=True)
                valid = np.ma.compressed(data)
                has_data = valid.size > 0
                checks.append({"check": "contains_valid_pixels", "ok": has_data})
                ok &= has_data
                if has_data:
                    checks.append({
                        "check": "plausible_elevation_range",
                        "ok": bool(valid.min() >= -500 and valid.max() <= 9000),
                        "detail": f"min={valid.min():.1f} max={valid.max():.1f}",
                    })
                    ok &= bool(valid.min() >= -500 and valid.max() <= 9000)
                checks.append({"check": "dimensions", "ok": src.width > 1 and src.height > 1,
                               "detail": f"{src.width}x{src.height}"})
                ok &= bool(src.width > 1 and src.height > 1)
        return {"ok": bool(ok), "checks": checks}

    def metadata(self, path: str) -> dict:
        import rasterio
        with rasterio.open(path) as src:
            return {
                "name": "Copernicus DEM GLO-30 clip",
                "type": "raster",
                "source": DEM_DATASET.source,
                "dataset": DEM_DATASET.id,
                "resolution": DEM_DATASET.resolution,
                "date": DEM_DATASET.date,
                "crs": str(src.crs),
                "units": DEM_DATASET.units,
                "nodata": src.nodata,
                "width": src.width,
                "height": src.height,
                "bbox": list(src.bounds),
                "license": DEM_DATASET.license,
                "processing": ["tiles_downloaded", "mosaicked", "clipped_to_aoi"],
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
