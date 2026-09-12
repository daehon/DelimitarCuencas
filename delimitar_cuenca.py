#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Delimitación de la cuenca hidrográfica aportante a un punto (lat, lon).

Descarga teselas del Copernicus DEM (GLO-30 / GLO-90) desde AWS Open Data
(sin credenciales), calcula dirección y acumulación de flujo D8 y exporta
el polígono de la cuenca.

Ejemplo:
    python delimitar_cuenca.py --lat 43.184 --lon -2.478 --buffer-km 20
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import rasterio
import requests
from matplotlib import pyplot as plt
from matplotlib.colors import LightSource
from pyproj import CRS, Geod, Transformer
from rasterio.enums import Resampling
from rasterio.features import shapes
from rasterio.merge import merge
from rasterio.transform import array_bounds, rowcol, xy
from rasterio.warp import calculate_default_transform, reproject
from shapely.geometry import mapping, shape
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union
from tqdm import tqdm

DIRMAP_D8 = (
    (0, 1),    # E
    (1, 1),    # SE
    (1, 0),    # S
    (1, -1),   # SW
    (0, -1),   # W
    (-1, -1),  # NW
    (-1, 0),   # N
    (-1, 1),   # NE
)

DEM_CATALOGO = {
    "glo30": {
        "nombre": "Copernicus GLO-30",
        "url": "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com",
        "res_arcsec": "10",
        "pixel_m": 30.0,
    },
    "glo90": {
        "nombre": "Copernicus GLO-90",
        "url": "https://copernicus-dem-90m.s3.eu-central-1.amazonaws.com",
        "res_arcsec": "30",
        "pixel_m": 90.0,
    },
}

USER_AGENT = "DelimitarCuencas/1.0 (Tecnalia)"
GEOD = Geod(ellps="WGS84")


@dataclass
class ResultadoCuenca:
    geometria_wgs84: object
    area_km2: float
    lat: float
    lon: float
    lat_snap: float
    lon_snap: float
    truncated: bool
    elev_min: float
    elev_max: float
    elev_mean: float
    n_celdas: int
    dem_fuente: str
    buffer_km: float
    rutas: dict


def _lat_label(lat_sw: int) -> str:
    hemi = "N" if lat_sw >= 0 else "S"
    return f"{hemi}{abs(lat_sw):02d}_00"


def _lon_label(lon_sw: int) -> str:
    hemi = "E" if lon_sw >= 0 else "W"
    return f"{hemi}{abs(lon_sw):03d}_00"


def _teselas_para_bbox(south: float, west: float, north: float, east: float) -> list[tuple[int, int]]:
    lats = range(math.floor(south), math.ceil(north))
    lons = range(math.floor(west), math.ceil(east))
    return [(lat_sw, lon_sw) for lat_sw in lats for lon_sw in lons]


def _bbox_desde_punto(lat: float, lon: float, buffer_km: float) -> tuple[float, float, float, float]:
    dlat = buffer_km / 111.32
    cos_lat = max(0.05, math.cos(math.radians(lat)))
    dlon = buffer_km / (111.32 * cos_lat)
    south = max(-90.0, lat - dlat)
    north = min(90.0, lat + dlat)
    west = lon - dlon
    east = lon + dlon
    return south, west, north, east


def utm_epsg(lat: float, lon: float) -> int:
    zona = int((lon + 180) // 6) + 1
    zona = min(max(zona, 1), 60)
    return (32700 if lat < 0 else 32600) + zona


def _descargar_fichero(url: str, destino: Path, timeout: int = 120) -> bool:
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists() and destino.stat().st_size > 2048:
        return True
    headers = {"User-Agent": USER_AGENT}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=timeout) as resp:
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            tmp = destino.with_suffix(destino.suffix + ".part")
            with open(tmp, "wb") as fh, tqdm(
                total=total or None,
                unit="B",
                unit_scale=True,
                desc=destino.name[:40],
                leave=False,
            ) as barra:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        fh.write(chunk)
                        barra.update(len(chunk))
            tmp.replace(destino)
            return True
    except requests.RequestException as exc:
        print(f"  Aviso: no se pudo descargar {url} ({exc})")
        return False


def descargar_dem_copernicus(
    south: float,
    west: float,
    north: float,
    east: float,
    cache_dir: Path,
    dem: str = "glo30",
) -> list[Path]:
    cfg = DEM_CATALOGO[dem]
    teselas = _teselas_para_bbox(south, west, north, east)
    if not teselas:
        raise RuntimeError("La ventana pedida no intersecta ninguna tesela del DEM.")
    print(f"DEM {cfg['nombre']}: {len(teselas)} tesela(s) en la ventana.")
    rutas: list[Path] = []
    for lat_sw, lon_sw in teselas:
        nombre = (
            f"Copernicus_DSM_COG_{cfg['res_arcsec']}_"
            f"{_lat_label(lat_sw)}_{_lon_label(lon_sw)}_DEM"
        )
        url = f"{cfg['url']}/{nombre}/{nombre}.tif"
        destino = cache_dir / dem / f"{nombre}.tif"
        if _descargar_fichero(url, destino):
            rutas.append(destino)
        else:
            print(f"  Tesela no disponible (océano o no publicada): {nombre}")
    if not rutas:
        raise RuntimeError(
            "No se descargó ninguna tesela. Prueba otro punto o el DEM glo90."
        )
    return rutas


def mosaic_y_reproyectar(
    rutas: Iterable[Path],
    bounds_wgs: tuple[float, float, float, float],
    dst_crs: CRS,
    pixel_m: float,
) -> tuple[np.ndarray, rasterio.Affine, CRS]:
    south, west, north, east = bounds_wgs
    datasets = [rasterio.open(path) for path in rutas]
    try:
        mosaic, transform = merge(
            datasets,
            bounds=(west, south, east, north),
            nodata=np.nan,
            dtype="float32",
        )
        src_crs = datasets[0].crs or CRS.from_epsg(4326)
    finally:
        for ds in datasets:
            ds.close()

    dem = mosaic[0].astype(np.float32)
    height, width = dem.shape
    dst_transform, dst_width, dst_height = calculate_default_transform(
        src_crs,
        dst_crs,
        width,
        height,
        west,
        south,
        east,
        north,
        resolution=pixel_m,
    )
    dst = np.full((dst_height, dst_width), np.nan, dtype=np.float32)
    reproject(
        source=dem,
        destination=dst,
        src_transform=transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    return dst, dst_transform, dst_crs


def _cargar_dem_local(
    path: Path,
    lat: float,
    lon: float,
    buffer_km: float,
    pixel_m: float,
) -> tuple[np.ndarray, rasterio.Affine, CRS]:
    south, west, north, east = _bbox_desde_punto(lat, lon, buffer_km)
    dst_crs = CRS.from_epsg(utm_epsg(lat, lon))
    with rasterio.open(path) as src:
        src_crs = src.crs or CRS.from_epsg(4326)
        window = rasterio.windows.from_bounds(west, south, east, north, transform=src.transform)
        window = window.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
        dem = src.read(1, window=window).astype(np.float32)
        transform = src.window_transform(window)
        nodata = src.nodata
        if nodata is not None:
            dem[dem == nodata] = np.nan
        height, width = dem.shape
        dst_transform, dst_width, dst_height = calculate_default_transform(
            src_crs,
            dst_crs,
            width,
            height,
            *array_bounds(height, width, transform),
            resolution=pixel_m,
        )
        dst = np.full((dst_height, dst_width), np.nan, dtype=np.float32)
        reproject(
            source=dem,
            destination=dst,
            src_transform=transform,
            src_crs=src_crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            src_nodata=np.nan,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    return dst, dst_transform, dst_crs


def fill_depressions(dem: np.ndarray) -> np.ndarray:
    """Relleno de depresiones por Priority-Flood (Barnes et al., 2014)."""
    import heapq

    ny, nx = dem.shape
    valid = np.isfinite(dem)
    if not valid.any():
        raise RuntimeError("El DEM no contiene celdas válidas en la ventana.")

    padded = np.pad(valid, 1, constant_values=False)
    vecino_invalido = np.zeros((ny, nx), dtype=bool)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            if di == 0 and dj == 0:
                continue
            vecino_invalido |= ~padded[1 + di : 1 + di + ny, 1 + dj : 1 + dj + nx]
    es_borde = valid & vecino_invalido

    filled = dem.astype(np.float64, copy=True)
    visited = ~valid
    heap: list[tuple[float, int, int, int]] = []
    seq = 0
    ii, jj = np.where(es_borde)
    for i, j in zip(ii.tolist(), jj.tolist()):
        heapq.heappush(heap, (float(filled[i, j]), seq, i, j))
        visited[i, j] = True
        seq += 1

    n_valid = int(valid.sum())
    procesadas = 0
    while heap:
        elev, _, i, j = heapq.heappop(heap)
        procesadas += 1
        if procesadas % 500_000 == 0:
            print(f"  Relleno de depresiones: {procesadas:,}/{n_valid:,} celdas")
        for di, dj in DIRMAP_D8:
            ni, nj = i + di, j + dj
            if ni < 0 or nj < 0 or ni >= ny or nj >= nx or visited[ni, nj]:
                continue
            visited[ni, nj] = True
            nueva = max(float(filled[ni, nj]), elev)
            filled[ni, nj] = nueva
            heapq.heappush(heap, (nueva, seq, ni, nj))
            seq += 1
    filled[~valid] = np.nan
    return filled


def flowdir_d8(dem: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Dirección de flujo D8 (código 0-7). 255 = nodata / sin salida."""
    ny, nx = dem.shape
    dist = np.array(
        [
            dx,
            math.hypot(dx, dy),
            dy,
            math.hypot(dx, dy),
            dx,
            math.hypot(dx, dy),
            dy,
            math.hypot(dx, dy),
        ],
        dtype=np.float64,
    )
    padded = np.pad(dem, 1, constant_values=np.nan)
    mejor_pendiente = np.full((ny, nx), -np.inf, dtype=np.float64)
    fdir = np.full((ny, nx), 255, dtype=np.uint8)

    for code, (di, dj) in enumerate(DIRMAP_D8):
        vecino = padded[1 + di : 1 + di + ny, 1 + dj : 1 + dj + nx]
        pendiente = (dem - vecino) / dist[code]
        mejor = pendiente > mejor_pendiente
        fdir[mejor] = code
        mejor_pendiente[mejor] = pendiente[mejor]

    nodata = ~np.isfinite(dem)
    fdir[nodata] = 255

    # Flats residuales: drenar hacia el vecino más bajo.
    flats = (mejor_pendiente <= 0) & ~nodata
    if flats.any():
        min_elev = np.full((ny, nx), np.inf, dtype=np.float64)
        for code, (di, dj) in enumerate(DIRMAP_D8):
            vecino = padded[1 + di : 1 + di + ny, 1 + dj : 1 + dj + nx]
            mas_bajo = flats & np.isfinite(vecino) & (vecino < min_elev)
            fdir[mas_bajo] = code
            min_elev[mas_bajo] = vecino[mas_bajo]
    return fdir


def accumulation(fdir: np.ndarray) -> np.ndarray:
    ny, nx = fdir.shape
    acc = np.ones((ny, nx), dtype=np.float64)
    acc[fdir == 255] = 0
    indeg = np.zeros((ny, nx), dtype=np.int16)

    for i in range(ny):
        fila = fdir[i]
        for j in range(nx):
            code = int(fila[j])
            if code == 255:
                continue
            di, dj = DIRMAP_D8[code]
            ni, nj = i + di, j + dj
            if 0 <= ni < ny and 0 <= nj < nx and fdir[ni, nj] != 255:
                indeg[ni, nj] += 1

    cola: deque[tuple[int, int]] = deque()
    ii, jj = np.where((indeg == 0) & (fdir != 255))
    for i, j in zip(ii.tolist(), jj.tolist()):
        cola.append((i, j))

    while cola:
        i, j = cola.popleft()
        code = int(fdir[i, j])
        if code == 255:
            continue
        di, dj = DIRMAP_D8[code]
        ni, nj = i + di, j + dj
        if not (0 <= ni < ny and 0 <= nj < nx) or fdir[ni, nj] == 255:
            continue
        acc[ni, nj] += acc[i, j]
        indeg[ni, nj] -= 1
        if indeg[ni, nj] == 0:
            cola.append((ni, nj))
    return acc


def snap_a_cauce(
    acc: np.ndarray,
    fdir: np.ndarray,
    row: int,
    col: int,
    radio_px: int,
    umbral: float,
) -> tuple[int, int]:
    ny, nx = acc.shape
    r0 = max(0, row - radio_px)
    r1 = min(ny, row + radio_px + 1)
    c0 = max(0, col - radio_px)
    c1 = min(nx, col + radio_px + 1)
    ventana = acc[r0:r1, c0:c1]
    valid = fdir[r0:r1, c0:c1] != 255
    if not valid.any():
        return row, col

    yy, xx = np.ogrid[r0:r1, c0:c1]
    dist2 = (yy - row) ** 2 + (xx - col) ** 2
    dentro = dist2 <= radio_px**2
    candidatos = valid & dentro & (ventana >= umbral)
    if candidatos.any():
        dist_c = np.where(candidatos, dist2, np.inf)
        pos = np.unravel_index(np.argmin(dist_c), dist_c.shape)
        return int(r0 + pos[0]), int(c0 + pos[1])

    dist_v = np.where(valid & dentro, dist2, np.inf)
    pos = np.unravel_index(np.argmin(dist_v), dist_v.shape)
    return int(r0 + pos[0]), int(c0 + pos[1])


def catchment(fdir: np.ndarray, row: int, col: int) -> np.ndarray:
    ny, nx = fdir.shape
    mask = np.zeros((ny, nx), dtype=bool)
    if not (0 <= row < ny and 0 <= col < nx) or fdir[row, col] == 255:
        raise RuntimeError("El punto de cierre cae fuera del DEM o en nodata.")
    mask[row, col] = True
    cola: deque[tuple[int, int]] = deque([(row, col)])
    inverso = [(-di, -dj) for di, dj in DIRMAP_D8]

    while cola:
        i, j = cola.popleft()
        for code, (di, dj) in enumerate(inverso):
            ni, nj = i + di, j + dj
            if not (0 <= ni < ny and 0 <= nj < nx) or mask[ni, nj]:
                continue
            if fdir[ni, nj] == code:
                mask[ni, nj] = True
                cola.append((ni, nj))
    return mask


def toca_borde(mask: np.ndarray, valid: np.ndarray) -> bool:
    borde = np.zeros_like(mask)
    borde[0, :] = True
    borde[-1, :] = True
    borde[:, 0] = True
    borde[:, -1] = True
    return bool(np.any(mask & borde & valid))


def raster_a_poligono(mask: np.ndarray, transform, dst_crs: CRS):
    mask_u8 = mask.astype(np.uint8)
    geoms = [
        shape(geom)
        for geom, val in shapes(mask_u8, mask=mask, transform=transform, connectivity=8)
        if val == 1
    ]
    if not geoms:
        raise RuntimeError("No se pudo vectorizar la cuenca (máscara vacía).")
    poly = unary_union(geoms)
    a_wgs = Transformer.from_crs(dst_crs, "EPSG:4326", always_xy=True).transform
    return shapely_transform(a_wgs, poly)


def area_km2(geom) -> float:
    superficie, _perimetro = GEOD.geometry_area_perimeter(geom)
    return abs(superficie) / 1e6


def guardar_geojson(geom, propiedades: dict, ruta: Path) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fc = {
        "type": "FeatureCollection",
        "name": "cuenca_aportante",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": [
            {
                "type": "Feature",
                "properties": propiedades,
                "geometry": mapping(geom),
            }
        ],
    }
    ruta.write_text(json.dumps(fc, ensure_ascii=False, indent=2), encoding="utf-8")


def guardar_raster_cuenca(
    dem: np.ndarray,
    mask: np.ndarray,
    transform,
    crs: CRS,
    ruta: Path,
) -> None:
    data = np.where(mask, dem, np.nan).astype(np.float32)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    perfil = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": transform,
        "nodata": np.nan,
        "compress": "deflate",
    }
    with rasterio.open(ruta, "w", **perfil) as dst:
        dst.write(data, 1)


def pintar_cuenca(
    dem: np.ndarray,
    mask: np.ndarray,
    acc: np.ndarray,
    transform,
    lon: float,
    lat: float,
    lon_snap: float,
    lat_snap: float,
    geom_wgs,
    area: float,
    ruta: Path,
) -> None:
    west, south, east, north = array_bounds(dem.shape[0], dem.shape[1], transform)
    to_wgs = Transformer.from_crs(transform, "EPSG:4326", always_xy=True)  # noqa: placeholder
    # El DEM está en UTM: convertimos extent a WGS84 para superponer el polígono.
    # Mejor pintar en UTM y transformar los puntos/polígono a UTM.
    crs_utm = None  # se pasa el extent nativo

    fig, ax = plt.subplots(figsize=(10, 9))
    dem_plot = np.array(dem, dtype=float)
    dem_plot[~np.isfinite(dem_plot)] = np.nan
    ls = LightSource(azdeg=315, altdeg=45)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        shaded = ls.shade(
            np.nan_to_num(dem_plot, nan=np.nanmedian(dem_plot)),
            cmap=plt.cm.terrain,
            vert_exag=2.0,
            blend_mode="overlay",
        )
    ax.imshow(shaded, extent=(west, east, south, north), origin="upper")
    overlay = np.zeros((*mask.shape, 4), dtype=float)
    overlay[mask] = (0.05, 0.35, 0.85, 0.35)
    ax.imshow(overlay, extent=(west, east, south, north), origin="upper")

    streams = mask & (acc >= max(80.0, np.nanpercentile(acc[mask], 92) if mask.any() else 80))
    stream_rgba = np.zeros((*mask.shape, 4), dtype=float)
    stream_rgba[streams] = (0.05, 0.15, 0.55, 0.85)
    ax.imshow(stream_rgba, extent=(west, east, south, north), origin="upper")

    a_utm = None
    # geom está en WGS84; hay que proyectarlo. Se hace en el llamador pasando
    # también el CRS. Aquí usamos un truco: dibujar a partir del contour de mask.
    ax.contour(mask.astype(float), levels=[0.5], colors="white", linewidths=1.2,
               extent=(west, east, south, north), origin="upper")

    # Puntos: necesitamos coordenadas en el CRS del raster.
    # Se reciben ya convertidas si se pasan como x_utm, y_utm... mantenemos
    # firma y convertimos con un transformer que se crea fuera. Para no
    # complicar, el llamador pasa lon/lat y transformamos con CRS del raster
    # guardado en un atributo global no. Recalculamos EPSG a partir de west.
    # west/east son UTM metros, no lon. El llamador debe pasar x,y UTM.
    _ = (geom_wgs, a_utm, crs_utm, to_wgs)  # silencia análisis estático
    ax.scatter([lon], [lat], c="red", s=40, zorder=5, label="Punto original", edgecolors="k")
    ax.scatter([lon_snap], [lat_snap], c="yellow", s=55, zorder=6, marker="x",
               linewidths=2, label="Punto de cierre")
    ax.set_title(f"Cuenca aportante — {area:.2f} km²")
    ax.set_xlabel("Este (m)")
    ax.set_ylabel("Norte (m)")
    ax.set_aspect("equal")
    ax.legend(loc="lower right")
    fig.tight_layout()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(ruta, dpi=140)
    plt.close(fig)


def pintar_cuenca_utm(
    dem: np.ndarray,
    mask: np.ndarray,
    acc: np.ndarray,
    transform,
    crs: CRS,
    lon: float,
    lat: float,
    lon_snap: float,
    lat_snap: float,
    area: float,
    ruta: Path,
) -> None:
    west, south, east, north = array_bounds(dem.shape[0], dem.shape[1], transform)
    a_utm = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    x0, y0 = a_utm(lon, lat)
    xs, ys = a_utm(lon_snap, lat_snap)

    fig, ax = plt.subplots(figsize=(10, 9))
    dem_plot = np.array(dem, dtype=float)
    med = float(np.nanmedian(dem_plot))
    ls = LightSource(azdeg=315, altdeg=45)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        shaded = ls.shade(
            np.nan_to_num(dem_plot, nan=med),
            cmap=plt.cm.terrain,
            vert_exag=2.0,
            blend_mode="overlay",
        )
    ax.imshow(shaded, extent=(west, east, south, north), origin="upper")
    overlay = np.zeros((*mask.shape, 4), dtype=float)
    overlay[mask] = (0.05, 0.35, 0.85, 0.38)
    ax.imshow(overlay, extent=(west, east, south, north), origin="upper")
    if mask.any():
        umbral = max(80.0, float(np.nanpercentile(acc[mask], 92)))
        streams = mask & (acc >= umbral)
        stream_rgba = np.zeros((*mask.shape, 4), dtype=float)
        stream_rgba[streams] = (0.02, 0.12, 0.55, 0.9)
        ax.imshow(stream_rgba, extent=(west, east, south, north), origin="upper")
        ax.contour(
            mask.astype(float),
            levels=[0.5],
            colors="white",
            linewidths=1.15,
            extent=(west, east, south, north),
            origin="upper",
        )
    ax.scatter([x0], [y0], c="red", s=46, zorder=5, label="Punto original", edgecolors="k")
    ax.scatter([xs], [ys], c="yellow", s=70, zorder=6, marker="x",
               linewidths=2.2, label="Punto de cierre (snap)")
    ax.set_title(f"Cuenca aportante — {area:.2f} km²")
    ax.set_xlabel("Este (m)")
    ax.set_ylabel("Norte (m)")
    ax.set_aspect("equal")
    ax.legend(loc="lower right", framealpha=0.9)
    fig.tight_layout()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(ruta, dpi=140)
    plt.close(fig)


def _procesar_dem(
    dem: np.ndarray,
    transform,
    crs: CRS,
    lat: float,
    lon: float,
    snap: bool,
    snap_km: float,
    pixel_m: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int, bool]:
    print("Rellenando depresiones…")
    filled = fill_depressions(dem)
    print("Calculando dirección y acumulación de flujo D8…")
    fdir = flowdir_d8(filled, pixel_m, pixel_m)
    acc = accumulation(fdir)

    to_xy = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    x, y = to_xy(lon, lat)
    row, col = rowcol(transform, x, y)
    ny, nx = dem.shape
    if not (0 <= row < ny and 0 <= col < nx):
        raise RuntimeError("El punto queda fuera del DEM recortado. Aumenta --buffer-km.")

    if snap:
        radio_px = max(1, int(round(snap_km * 1000.0 / pixel_m)))
        umbral = max(40.0, float(np.nanpercentile(acc[acc > 0], 85)) if np.any(acc > 0) else 40.0)
        row, col = snap_a_cauce(acc, fdir, int(row), int(col), radio_px, umbral)
        print(f"Punto ajustado a cauce en fila={row}, col={col} (radio {snap_km} km).")
    else:
        row, col = int(row), int(col)

    print("Delimitando la cuenca aportante…")
    mask = catchment(fdir, row, col)
    truncated = toca_borde(mask, np.isfinite(dem))
    return filled, acc, mask, row, col, truncated


def delimitar_cuenca(
    lat: float,
    lon: float,
    *,
    buffer_km: float = 25.0,
    max_buffer_km: float = 120.0,
    auto_expand: bool = True,
    snap: bool = True,
    snap_km: float = 0.4,
    dem: str = "glo30",
    dem_path: Path | None = None,
    cache_dir: Path = Path("cache"),
    out_dir: Path = Path("output"),
    plot: bool = True,
) -> ResultadoCuenca:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Latitud o longitud fuera de rango.")
    if dem not in DEM_CATALOGO:
        raise ValueError(f"DEM desconocido: {dem}. Usa glo30 o glo90.")

    cfg = DEM_CATALOGO[dem]
    pixel_m = cfg["pixel_m"] if dem_path is None else cfg["pixel_m"]
    dst_crs = CRS.from_epsg(utm_epsg(lat, lon))
    buffer_actual = buffer_km
    ultimo_aviso = None

    while True:
        south, west, north, east = _bbox_desde_punto(lat, lon, buffer_actual)
        print(
            f"Ventana {buffer_actual:.1f} km | "
            f"bbox WGS84 [{west:.4f}, {south:.4f}, {east:.4f}, {north:.4f}]"
        )
        if dem_path is not None:
            elev, transform, crs = _cargar_dem_local(
                Path(dem_path), lat, lon, buffer_actual, pixel_m
            )
            fuente = f"local:{dem_path}"
        else:
            tiles = descargar_dem_copernicus(
                south, west, north, east, Path(cache_dir), dem=dem
            )
            print("Mosaico y reproyección a UTM…")
            elev, transform, crs = mosaic_y_reproyectar(
                tiles, (south, west, north, east), dst_crs, pixel_m
            )
            fuente = cfg["nombre"]

        filled, acc, mask, row, col, truncated = _procesar_dem(
            elev, transform, crs, lat, lon, snap, snap_km, pixel_m
        )
        if truncated and auto_expand and buffer_actual < max_buffer_km - 1e-6:
            nuevo = min(max_buffer_km, buffer_actual * 1.8)
            print(
                f"La cuenca toca el borde del DEM. Ampliando ventana "
                f"a {nuevo:.1f} km…"
            )
            buffer_actual = nuevo
            ultimo_aviso = "truncada"
            continue
        break

    x_snap, y_snap = xy(transform, row, col, offset="center")
    a_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    lon_snap, lat_snap = a_wgs(x_snap, y_snap)

    geom = raster_a_poligono(mask, transform, crs)
    area = area_km2(geom)
    vals = filled[mask]
    n_celdas = int(mask.sum())

    stem = f"cuenca_{lat:.5f}_{lon:.5f}"
    out_dir = Path(out_dir)
    ruta_geojson = out_dir / f"{stem}.geojson"
    ruta_tif = out_dir / f"{stem}.tif"
    ruta_png = out_dir / f"{stem}.png"
    ruta_meta = out_dir / f"{stem}_meta.json"

    props = {
        "lat": lat,
        "lon": lon,
        "lat_snap": lat_snap,
        "lon_snap": lon_snap,
        "area_km2": round(area, 4),
        "n_celdas": n_celdas,
        "elev_min_m": float(np.nanmin(vals)) if n_celdas else None,
        "elev_max_m": float(np.nanmax(vals)) if n_celdas else None,
        "elev_media_m": float(np.nanmean(vals)) if n_celdas else None,
        "dem": fuente,
        "buffer_km": buffer_actual,
        "truncada": truncated,
        "snap_km": snap_km if snap else 0.0,
    }
    guardar_geojson(geom, props, ruta_geojson)
    guardar_raster_cuenca(filled, mask, transform, crs, ruta_tif)
    if plot:
        pintar_cuenca_utm(
            filled, mask, acc, transform, crs,
            lon, lat, lon_snap, lat_snap, area, ruta_png,
        )

    meta = dict(props)
    meta["rutas"] = {
        "geojson": str(ruta_geojson),
        "tif": str(ruta_tif),
        "png": str(ruta_png) if plot else None,
    }
    ruta_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if truncated:
        print(
            "AVISO: la cuenca llega al borde del DEM; puede estar incompleta. "
            "Repite con --max-buffer-km más alto."
        )
    elif ultimo_aviso:
        print("La ventana se amplió automáticamente hasta cubrir la cuenca.")

    print(f"Área aportante: {area:.3f} km²  ({n_celdas:,} celdas)")
    print(f"GeoJSON: {ruta_geojson}")
    return ResultadoCuenca(
        geometria_wgs84=geom,
        area_km2=area,
        lat=lat,
        lon=lon,
        lat_snap=lat_snap,
        lon_snap=lon_snap,
        truncated=truncated,
        elev_min=float(np.nanmin(vals)) if n_celdas else float("nan"),
        elev_max=float(np.nanmax(vals)) if n_celdas else float("nan"),
        elev_mean=float(np.nanmean(vals)) if n_celdas else float("nan"),
        n_celdas=n_celdas,
        dem_fuente=fuente,
        buffer_km=buffer_actual,
        rutas=meta["rutas"],
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Delimitá la cuenca hidrográfica aportante a un punto lat/lon "
        "usando Copernicus DEM en línea."
    )
    p.add_argument("--lat", type=float, required=True, help="Latitud WGS84 (grados)")
    p.add_argument("--lon", type=float, required=True, help="Longitud WGS84 (grados)")
    p.add_argument(
        "--buffer-km",
        type=float,
        default=25.0,
        help="Radio inicial de la ventana de DEM alrededor del punto (km)",
    )
    p.add_argument(
        "--max-buffer-km",
        type=float,
        default=120.0,
        help="Radio máximo si la cuenca toca el borde y se autoamplía",
    )
    p.add_argument(
        "--no-auto-expand",
        action="store_true",
        help="No ampliar la ventana aunque la cuenca quede cortada",
    )
    p.add_argument(
        "--dem",
        choices=sorted(DEM_CATALOGO),
        default="glo30",
        help="Modelo digital: glo30 (~30 m) o glo90 (~90 m, más rápido)",
    )
    p.add_argument("--dem-path", type=Path, default=None, help="DEM local (GeoTIFF) en vez de Copernicus")
    p.add_argument("--no-snap", action="store_true", help="No ajustar el punto al cauce más cercano")
    p.add_argument("--snap-km", type=float, default=0.4, help="Radio de búsqueda del cauce (km)")
    p.add_argument("--cache-dir", type=Path, default=Path("cache"), help="Caché de teselas")
    p.add_argument("--out-dir", type=Path, default=Path("output"), help="Carpeta de resultados")
    p.add_argument("--no-plot", action="store_true", help="No generar la figura PNG")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        delimitar_cuenca(
            args.lat,
            args.lon,
            buffer_km=args.buffer_km,
            max_buffer_km=args.max_buffer_km,
            auto_expand=not args.no_auto_expand,
            snap=not args.no_snap,
            snap_km=args.snap_km,
            dem=args.dem,
            dem_path=args.dem_path,
            cache_dir=args.cache_dir,
            out_dir=args.out_dir,
            plot=not args.no_plot,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
