#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Delimitación de la cuenca hidrográfica aportante a un punto (lat, lon).

Descarga un modelo digital del terreno (Copernicus GLO-30/GLO-90, FABDEM o
ANADEM), calcula dirección y acumulación de flujo D8 y exporta el polígono
de la cuenca.

Ejemplo:
    python delimitar_cuenca.py --lat -25.634 --lon -56.273 --dem anadem --buffer-km 20
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import warnings
import zipfile
import zlib
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
from rasterio.warp import calculate_default_transform, reproject, transform_bounds
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
        "tipo": "copernicus",
        "url": "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com",
        "res_arcsec": "10",
        "pixel_m": 30.0,
    },
    "glo90": {
        "nombre": "Copernicus GLO-90",
        "tipo": "copernicus",
        "url": "https://copernicus-dem-90m.s3.eu-central-1.amazonaws.com",
        "res_arcsec": "30",
        "pixel_m": 90.0,
    },
    "fabdem": {
        "nombre": "FABDEM v1.2",
        "tipo": "fabdem",
        "url": "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn",
        "pixel_m": 30.0,
    },
    "anadem": {
        "nombre": "ANADEM v1",
        "tipo": "anadem",
        "url": "https://metadados.snirh.gov.br/files/anadem_v1_tiles",
        "pixel_m": 30.0,
    },
}

# Copernicus ~90 m a veces se cita como 80 m.
DEM_ALIAS = {
    "glo80": "glo90",
    "copernicus": "glo30",
    "copernicus30": "glo30",
    "copernicus90": "glo90",
}

MGRS_BANDAS = "CDEFGHJKLMNPQRSTUVWX"
FABDEM_VERSION = "V1-2"
ANADEM_NOMBRE = "anadem_v1_{gzd}.tif"

USER_AGENT = "DelimitarCuencas/1.0 (Tecnalia; +https://github.com/daehon/DelimitarCuencas)"
GEOD = Geod(ellps="WGS84")
_HTTP = None


def _sesion_http():
    global _HTTP
    if _HTTP is None:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        sesion = requests.Session()
        sesion.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        reintentos = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
        )
        adaptador = HTTPAdapter(max_retries=reintentos)
        sesion.mount("https://", adaptador)
        sesion.mount("http://", adaptador)
        _HTTP = sesion
    return _HTTP


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


def normalizar_dem(dem: str) -> str:
    clave = dem.strip().lower()
    clave = DEM_ALIAS.get(clave, clave)
    if clave not in DEM_CATALOGO:
        opciones = ", ".join(sorted(DEM_CATALOGO))
        raise ValueError(f"DEM desconocido: {dem}. Usa {opciones} (o glo80 = glo90).")
    return clave


def _descargar_fichero(url: str, destino: Path, timeout: int | tuple = (30, 180)) -> bool:
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists() and destino.stat().st_size > 2048:
        return True
    try:
        with _sesion_http().get(url, stream=True, timeout=timeout) as resp:
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
            "No se descargó ninguna tesela Copernicus. Prueba glo90, fabdem o anadem."
        )
    return rutas


def _fabdem_esquina(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}"


def _fabdem_zip(lat_sw: int, lon_sw: int) -> str:
    lat0 = math.floor(lat_sw / 10) * 10
    lon0 = math.floor(lon_sw / 10) * 10
    sw = _fabdem_esquina(lat0, lon0)
    ne = _fabdem_esquina(lat0 + 10, lon0 + 10)
    return f"{sw}-{ne}_FABDEM_{FABDEM_VERSION}.zip"


def _http_rango(url: str, inicio: int, fin: int) -> bytes:
    resp = _sesion_http().get(
        url,
        headers={"Range": f"bytes={inicio}-{fin}"},
        timeout=(30, 180),
        stream=True,
    )
    resp.raise_for_status()
    if resp.status_code != 206:
        raise RuntimeError(f"El servidor no admite descarga parcial (HTTP {resp.status_code}).")
    return resp.content


def _fabdem_indice_zip(url: str) -> dict[str, dict]:
    cabeza = _sesion_http().head(url, allow_redirects=True, timeout=30)
    cabeza.raise_for_status()
    tamano = int(cabeza.headers.get("content-length") or 0)
    if tamano <= 0:
        raise RuntimeError("No se pudo saber el tamaño del ZIP de FABDEM.")
    cola = min(tamano, 131072)
    cola_bytes = _http_rango(url, tamano - cola, tamano - 1)
    eocd = cola_bytes.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise RuntimeError("No se encontró el índice del ZIP remoto de FABDEM.")
    (
        _sig,
        disco,
        disco_cd,
        _entradas_disco,
        total,
        tam_cd,
        offset_cd,
        _comentario,
    ) = struct.unpack_from("<4s4H2LH", cola_bytes, eocd)
    if disco or disco_cd or offset_cd == 0xFFFFFFFF:
        raise RuntimeError("ZIP FABDEM no soportado (multidisco o ZIP64).")
    directorio = _http_rango(url, offset_cd, offset_cd + tam_cd - 1)
    entradas: dict[str, dict] = {}
    pos = 0
    while pos < len(directorio):
        if directorio[pos : pos + 4] != b"PK\x01\x02":
            break
        (
            _sig,
            _ver_m,
            _ver_n,
            _flags,
            metodo,
            _mt,
            _md,
            _crc,
            tam_comp,
            tam_raw,
            n_nombre,
            n_extra,
            n_comentario,
            _disco,
            _int_attr,
            _ext_attr,
            offset_local,
        ) = struct.unpack_from("<4s6H3L5H2L", directorio, pos)
        nombre = directorio[pos + 46 : pos + 46 + n_nombre].decode("utf-8", errors="replace")
        entradas[nombre] = {
            "metodo": metodo,
            "tam_comp": tam_comp,
            "offset_local": offset_local,
        }
        pos += 46 + n_nombre + n_extra + n_comentario
        if len(entradas) >= total:
            break
    return entradas


def _fabdem_extraer_tesela(url: str, tif_name: str, destino: Path, indice: dict[str, dict]) -> bool:
    miembro = next(
        (n for n in indice if Path(n).name.lower() == tif_name.lower()),
        None,
    )
    if miembro is None:
        print(f"  No está {tif_name} en el ZIP remoto.")
        return False
    info = indice[miembro]
    cab = _http_rango(url, info["offset_local"], info["offset_local"] + 29)
    if cab[:4] != b"PK\x03\x04":
        raise RuntimeError("Cabecera ZIP local inválida.")
    _sig, _ver, _flags, metodo, _mt, _md, _crc, _cs, _us, n_nombre, n_extra = struct.unpack(
        "<4s5H3L2H", cab
    )
    inicio = info["offset_local"] + 30 + n_nombre + n_extra
    fin = inicio + info["tam_comp"] - 1
    print(f"  Extrayendo {tif_name} ({info['tam_comp'] / 1e6:.1f} MB del ZIP remoto)…")
    comprimido = _http_rango(url, inicio, fin)
    if info["metodo"] == 0:
        datos = comprimido
    elif info["metodo"] == 8:
        datos = zlib.decompress(comprimido, -zlib.MAX_WBITS)
    else:
        raise RuntimeError(f"Compresión ZIP no soportada: {info['metodo']}")
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(destino.suffix + ".part")
    tmp.write_bytes(datos)
    tmp.replace(destino)
    return True


def _extraer_tif_de_zip(zip_path: Path, tif_name: str, destino: Path) -> bool:
    if destino.exists() and destino.stat().st_size > 2048:
        return True
    try:
        with zipfile.ZipFile(zip_path) as zf:
            miembro = next(
                (n for n in zf.namelist() if Path(n).name.lower() == tif_name.lower()),
                None,
            )
            if miembro is None:
                print(f"  No está {tif_name} dentro de {zip_path.name}")
                return False
            destino.parent.mkdir(parents=True, exist_ok=True)
            tmp = destino.with_suffix(destino.suffix + ".part")
            with zf.open(miembro) as src, open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 256)
                    if not chunk:
                        break
                    dst.write(chunk)
            tmp.replace(destino)
            return True
    except zipfile.BadZipFile:
        print(f"  ZIP corrupto: {zip_path}. Bórralo y vuelve a lanzar.")
        return False


def descargar_dem_fabdem(
    south: float,
    west: float,
    north: float,
    east: float,
    cache_dir: Path,
) -> list[Path]:
    cfg = DEM_CATALOGO["fabdem"]
    teselas = _teselas_para_bbox(south, west, north, east)
    print(
        f"DEM {cfg['nombre']}: {len(teselas)} tesela(s) de 1°. "
        "Se extraen del ZIP remoto (no se baja el archivo de ~2 GB)."
    )
    rutas: list[Path] = []
    indices: dict[str, dict[str, dict]] = {}
    for lat_sw, lon_sw in teselas:
        tif_name = f"{_fabdem_esquina(lat_sw, lon_sw)}_FABDEM_{FABDEM_VERSION}.tif"
        destino = cache_dir / "fabdem" / tif_name
        if destino.exists() and destino.stat().st_size > 2048:
            rutas.append(destino)
            continue
        zip_name = _fabdem_zip(lat_sw, lon_sw)
        url = f"{cfg['url']}/{zip_name}"
        try:
            if zip_name not in indices:
                print(f"  Leyendo índice de {zip_name}…")
                indices[zip_name] = _fabdem_indice_zip(url)
            if _fabdem_extraer_tesela(url, tif_name, destino, indices[zip_name]):
                rutas.append(destino)
                continue
        except (requests.RequestException, OSError, RuntimeError, struct.error) as exc:
            print(f"  Aviso: extracción parcial falló ({exc}). Intentando ZIP completo…")
        zip_path = cache_dir / "fabdem" / zip_name
        if zip_path.exists() and not zipfile.is_zipfile(zip_path):
            zip_path.unlink()
        if _descargar_fichero(url, zip_path, timeout=(30, 300)):
            if _extraer_tif_de_zip(zip_path, tif_name, destino):
                rutas.append(destino)
                continue
        print(f"  Tesela FABDEM no disponible: {tif_name}")
    if not rutas:
        raise RuntimeError(
            "No se obtuvo ninguna tesela FABDEM. Prueba --dem anadem en Sudamérica."
        )
    return rutas


def _mgrs_banda(lat: float) -> str:
    if lat < -80 or lat > 84:
        raise ValueError("Latitud fuera del sistema MGRS.")
    idx = int(math.floor((lat + 80.0) / 8.0))
    idx = min(max(idx, 0), len(MGRS_BANDAS) - 1)
    return MGRS_BANDAS[idx]


def _utm_zona(lon: float) -> int:
    zona = int((lon + 180) // 6) + 1
    return min(max(zona, 1), 60)


def _anadem_gzds(south: float, west: float, north: float, east: float) -> list[str]:
    gzds: set[str] = set()
    n_lat = max(2, int(math.ceil((north - south) / 2.0)) + 1)
    n_lon = max(2, int(math.ceil((east - west) / 2.0)) + 1)
    for i in range(n_lat):
        lat = south + (north - south) * i / (n_lat - 1)
        for j in range(n_lon):
            lon = west + (east - west) * j / (n_lon - 1)
            gzds.add(f"{_utm_zona(lon)}{_mgrs_banda(lat)}")
    return sorted(gzds)


def descargar_dem_anadem(
    south: float,
    west: float,
    north: float,
    east: float,
    cache_dir: Path,
) -> list[Path]:
    cfg = DEM_CATALOGO["anadem"]
    gzds = _anadem_gzds(south, west, north, east)
    print(f"DEM {cfg['nombre']}: teselas MGRS {', '.join(gzds)}.")
    rutas: list[Path] = []
    for gzd in gzds:
        nombre = ANADEM_NOMBRE.format(gzd=gzd)
        url = f"{cfg['url']}/{nombre}"
        destino = cache_dir / "anadem" / nombre
        if _descargar_fichero(url, destino, timeout=1200):
            rutas.append(destino)
        else:
            print(f"  Tesela ANADEM no disponible: {nombre} (¿fuera de Sudamérica?)")
    if not rutas:
        raise RuntimeError(
            "No se descargó ANADEM. Cubre Sudamérica; en Europa usa glo30, glo90 o fabdem."
        )
    return rutas


def descargar_dem(
    south: float,
    west: float,
    north: float,
    east: float,
    cache_dir: Path,
    dem: str,
) -> list[Path]:
    tipo = DEM_CATALOGO[dem]["tipo"]
    if tipo == "copernicus":
        return descargar_dem_copernicus(south, west, north, east, cache_dir, dem=dem)
    if tipo == "fabdem":
        return descargar_dem_fabdem(south, west, north, east, cache_dir)
    if tipo == "anadem":
        return descargar_dem_anadem(south, west, north, east, cache_dir)
    raise ValueError(f"Tipo de DEM no implementado: {tipo}")


def mosaic_y_reproyectar(
    rutas: Iterable[Path],
    bounds_wgs: tuple[float, float, float, float],
    dst_crs: CRS,
    pixel_m: float,
) -> tuple[np.ndarray, rasterio.Affine, CRS]:
    south, west, north, east = bounds_wgs
    datasets = [rasterio.open(path) for path in rutas]
    try:
        src_crs = datasets[0].crs or CRS.from_epsg(4326)
        src_nodata = datasets[0].nodata
        left, bottom, right, top = transform_bounds(
            CRS.from_epsg(4326), src_crs, west, south, east, north, densify_pts=21
        )
        mosaic, transform = merge(
            datasets,
            bounds=(left, bottom, right, top),
            nodata=np.nan,
            dtype="float32",
        )
    finally:
        for ds in datasets:
            ds.close()

    dem = mosaic[0].astype(np.float32)
    if src_nodata is not None and np.isfinite(src_nodata):
        dem[dem == src_nodata] = np.nan
    dem[dem <= -1000] = np.nan
    height, width = dem.shape
    left, bottom, right, top = array_bounds(height, width, transform)
    dst_transform, dst_width, dst_height = calculate_default_transform(
        src_crs,
        dst_crs,
        width,
        height,
        left,
        bottom,
        right,
        top,
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


def suavizar_dem(dem: np.ndarray, radio_px: int) -> np.ndarray:
    """Media local ignorando nodata. radio_px=1 usa una ventana 3×3."""
    if radio_px <= 0:
        return dem
    r = int(radio_px)
    valid = np.isfinite(dem)
    vals = np.pad(np.where(valid, dem.astype(np.float64), 0.0), 0)
    cnt = np.pad(valid.astype(np.float64), 0)
    # Prefijo con fila/columna extra de ceros.
    ny, nx = dem.shape
    I = np.zeros((ny + 1, nx + 1), dtype=np.float64)
    C = np.zeros((ny + 1, nx + 1), dtype=np.float64)
    I[1:, 1:] = np.cumsum(np.cumsum(vals, axis=0), axis=1)
    C[1:, 1:] = np.cumsum(np.cumsum(cnt, axis=0), axis=1)
    ii = np.arange(ny)[:, None]
    jj = np.arange(nx)[None, :]
    r0 = np.clip(ii - r, 0, ny)
    c0 = np.clip(jj - r, 0, nx)
    r1 = np.clip(ii + r + 1, 0, ny)
    c1 = np.clip(jj + r + 1, 0, nx)
    s = I[r1, c1] - I[r0, c1] - I[r1, c0] + I[r0, c0]
    n = C[r1, c1] - C[r0, c1] - C[r1, c0] + C[r0, c0]
    out = np.full(dem.shape, np.nan, dtype=np.float64)
    ok = (n > 0) & valid
    out[ok] = s[ok] / n[ok]
    return out


def flowdir_d8(dem: np.ndarray, dx: float, dy: float, lamina_m: float = 0.0) -> np.ndarray:
    """Dirección de flujo D8 (código 0-7). 255 = nodata / sin salida.

    lamina_m: el agua puede rebasar resaltos de hasta esa altura (metros).
    """
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
    lamina = max(0.0, float(lamina_m))

    for code, (di, dj) in enumerate(DIRMAP_D8):
        vecino = padded[1 + di : 1 + di + ny, 1 + dj : 1 + dj + nx]
        pendiente = (dem + lamina - vecino) / dist[code]
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


def _flecha_norte(ax) -> None:
    ax.annotate(
        "N",
        xy=(0.93, 0.90),
        xytext=(0.93, 0.78),
        xycoords="axes fraction",
        textcoords="axes fraction",
        arrowprops=dict(arrowstyle="-|>", color="k", lw=1.6, mutation_scale=16),
        ha="center",
        va="center",
        fontsize=12,
        fontweight="bold",
        zorder=10,
    )


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
    extent = (west, east, south, north)
    a_utm = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    x0, y0 = a_utm(lon, lat)
    xs, ys = a_utm(lon_snap, lat_snap)

    # El ráster GIS tiene la fila 0 al norte. Se voltea para dibujar con
    # origin="lower" (Y creciente hacia arriba = norte arriba).
    dem_plot = np.flipud(np.array(dem, dtype=float))
    mask_plot = np.flipud(np.asarray(mask))
    acc_plot = np.flipud(np.asarray(acc))
    med = float(np.nanmedian(dem_plot))

    fig, ax = plt.subplots(figsize=(10, 9))
    ls = LightSource(azdeg=315, altdeg=45)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        shaded = ls.shade(
            np.nan_to_num(dem_plot, nan=med),
            cmap=plt.cm.terrain,
            vert_exag=2.0,
            blend_mode="overlay",
        )
    ax.imshow(shaded, extent=extent, origin="lower")
    overlay = np.zeros((*mask_plot.shape, 4), dtype=float)
    overlay[mask_plot] = (0.05, 0.35, 0.85, 0.38)
    ax.imshow(overlay, extent=extent, origin="lower")
    if mask_plot.any():
        umbral = max(80.0, float(np.nanpercentile(acc_plot[mask_plot], 92)))
        streams = mask_plot & (acc_plot >= umbral)
        stream_rgba = np.zeros((*mask_plot.shape, 4), dtype=float)
        stream_rgba[streams] = (0.02, 0.12, 0.55, 0.9)
        ax.imshow(stream_rgba, extent=extent, origin="lower")
        ax.contour(
            mask_plot.astype(float),
            levels=[0.5],
            colors="white",
            linewidths=1.15,
            extent=extent,
            origin="lower",
        )
    ax.scatter([x0], [y0], c="red", s=46, zorder=5, label="Punto original", edgecolors="k")
    ax.scatter([xs], [ys], c="yellow", s=70, zorder=6, marker="x",
               linewidths=2.2, label="Punto de cierre (snap)")
    ax.set_xlim(west, east)
    ax.set_ylim(south, north)
    ax.set_title(f"Cuenca aportante — {area:.2f} km²")
    ax.set_xlabel("Este (m)")
    ax.set_ylabel("Norte (m)")
    ax.set_aspect("equal", adjustable="box")
    _flecha_norte(ax)
    ax.legend(loc="lower left", framealpha=0.9)
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
    lamina_m: float = 0.0,
    suavizado_m: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int, bool]:
    trabajo = dem
    if suavizado_m > 0:
        radio = max(1, int(round(suavizado_m / pixel_m)))
        print(f"Suavizando rugosidad (ventana {2 * radio + 1}×{2 * radio + 1} celdas)…")
        trabajo = suavizar_dem(trabajo, radio)
    print("Rellenando depresiones…")
    filled = fill_depressions(trabajo)
    print(
        "Calculando dirección y acumulación de flujo D8"
        + (f" (lámina {lamina_m:g} m)…" if lamina_m > 0 else "…")
    )
    fdir = flowdir_d8(filled, pixel_m, pixel_m, lamina_m=lamina_m)
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
    lamina_m: float = 0.0,
    suavizado_m: float = 0.0,
    dem: str = "glo30",
    dem_path: Path | None = None,
    cache_dir: Path = Path("cache"),
    out_dir: Path = Path("output"),
    plot: bool = True,
) -> ResultadoCuenca:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Latitud o longitud fuera de rango.")
    dem = normalizar_dem(dem)
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
            tiles = descargar_dem(
                south, west, north, east, Path(cache_dir), dem=dem
            )
            print("Mosaico y reproyección a UTM…")
            elev, transform, crs = mosaic_y_reproyectar(
                tiles, (south, west, north, east), dst_crs, pixel_m
            )
            fuente = cfg["nombre"]

        filled, acc, mask, row, col, truncated = _procesar_dem(
            elev, transform, crs, lat, lon, snap, snap_km, pixel_m,
            lamina_m=lamina_m, suavizado_m=suavizado_m,
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

    clave_salida = "local" if dem_path is not None else dem
    snap_etiqueta = snap_km if snap else 0.0
    stem = (
        f"cuenca_{lat:.5f}_{lon:.5f}_{clave_salida}"
        f"_buf{buffer_km:g}_max{max_buffer_km:g}_snap{snap_etiqueta:g}"
        f"_lam{lamina_m:g}_suav{suavizado_m:g}"
    )
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
        "dem_id": clave_salida,
        "buffer_km": buffer_actual,
        "truncada": truncated,
        "snap_km": snap_km if snap else 0.0,
        "lamina_m": lamina_m,
        "suavizado_m": suavizado_m,
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
        description="Delimita la cuenca hidrográfica aportante a un punto lat/lon."
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
        default="glo30",
        help="Modelo digital: glo30, glo90 (o glo80), fabdem, anadem",
    )
    p.add_argument("--dem-path", type=Path, default=None, help="DEM local (GeoTIFF) en vez de descargar")
    p.add_argument("--no-snap", action="store_true", help="No ajustar el punto al cauce más cercano")
    p.add_argument("--snap-km", type=float, default=0.4, help="Radio de búsqueda del cauce (km)")
    p.add_argument(
        "--lamina-m",
        type=float,
        default=0.0,
        help="Altura de barreras que el agua puede rebasar (metros). 0 = estricto",
    )
    p.add_argument(
        "--suavizado-m",
        type=float,
        default=0.0,
        help="Radio de suavizado del DEM (metros). Reduce rugosidad de pocas celdas",
    )
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
            lamina_m=args.lamina_m,
            suavizado_m=args.suavizado_m,
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
