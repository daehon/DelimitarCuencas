#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lanza la delimitación de la cuenca aportante a un punto.

1. Edita LAT, LON, DEM y, si hace falta, LAMINA_M / SUAVIZADO_M.
2. Activa el entorno:  .venv\\Scripts\\Activate.ps1
3. Ejecuta:            python lanzar_cuenca.py

También puedes sobreescribir un valor sin editar el archivo, por ejemplo:
    python lanzar_cuenca.py --lamina-m 3 --suavizado-m 90
"""

from __future__ import annotations

import argparse
from pathlib import Path

from delimitar_cuenca import delimitar_cuenca

# Punto de cierre (desagüe / estación / presa).
LAT = -25.635004
LON = -56.274458

# Radio inicial del DEM alrededor del punto (km).
BUFFER_KM = 20.0
MAX_BUFFER_KM = 80.0

# Modelo digital del terreno. Elige uno:
#   "glo30"   Copernicus GLO-30 (~30 m). Superficie: incluye árboles y tejados.
#   "glo90"   Copernicus GLO-90 (~90 m; a veces se cita como 80 m). Más rápido.
#   "fabdem"  Copernicus sin bosque ni edificios (~30 m). Mejor en llanuras.
#   "anadem"  Terreno de Sudamérica (~30 m). Recomendado en Paraguay.
DEM = "glo30"

# Ajusta el punto al cauce más cercano dentro de SNAP_KM.
SNAP = True
SNAP_KM = 0.8

# --- Rugosidad del DEM ------------------------------------------------------
# D8 manda toda el agua de cada celda a UN vecino (el de mayor pendiente).
# Un lomo de pocos metros (ruido, vegetación) aísla el punto y la cuenca
# sale minúscula. LAMINA_M ya no “deja subir” el agua: recorta esos lomos
# en el mapa, que es lo que realmente conecta el drenaje.
#
# LAMINA_M: altura máxima (m) de lomos a rebajar. 0 = no recortar.
#   En terreno: 2–5. Si usas glo30/glo90 (incluye árboles): prueba 10–20.
# SUAVIZADO_M: radio (m) de la ventana (media + ancho de lomo a recortar).
#   En DEM de 30 m, 60 ≈ 2 celdas. En glo90 (~90 m), 120 ≈ 1–2 celdas.
LAMINA_M = 10.0
SUAVIZADO_M = 120.0

CARPETA = Path(__file__).resolve().parent
SALIDA = CARPETA / "output"
CACHE = CARPETA / "cache"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Delimita la cuenca aportante. Los valores del archivo son el valor por defecto.",
    )
    p.add_argument("--lat", type=float, default=LAT)
    p.add_argument("--lon", type=float, default=LON)
    p.add_argument("--buffer-km", type=float, default=BUFFER_KM)
    p.add_argument("--max-buffer-km", type=float, default=MAX_BUFFER_KM)
    p.add_argument("--dem", default=DEM, choices=("glo30", "glo90", "fabdem", "anadem"))
    p.add_argument("--snap-km", type=float, default=SNAP_KM)
    p.add_argument("--no-snap", action="store_true", help="No ajustar el punto al cauce")
    p.add_argument(
        "--lamina-m",
        type=float,
        default=LAMINA_M,
        help="Altura de lomos a recortar (m). 0 = no recortar",
    )
    p.add_argument(
        "--suavizado-m",
        type=float,
        default=SUAVIZADO_M,
        help="Radio de suavizado del DEM (m). 0 = sin suavizar",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    snap = SNAP and not args.no_snap
    print(
        f"Parámetros: DEM={args.dem}  lamina={args.lamina_m:g} m  "
        f"suavizado={args.suavizado_m:g} m  snap={args.snap_km:g} km"
    )
    resultado = delimitar_cuenca(
        lat=args.lat,
        lon=args.lon,
        buffer_km=args.buffer_km,
        max_buffer_km=args.max_buffer_km,
        snap=snap,
        snap_km=args.snap_km,
        lamina_m=args.lamina_m,
        suavizado_m=args.suavizado_m,
        dem=args.dem,
        cache_dir=CACHE,
        out_dir=SALIDA,
        plot=True,
    )
    print()
    print(f"DEM:        {resultado.dem_fuente}")
    print(f"Área:       {resultado.area_km2:.3f} km²")
    print(f"Cierre:     {resultado.lat_snap:.5f}, {resultado.lon_snap:.5f}")
    print(f"Lámina:     {args.lamina_m:g} m")
    print(f"Suavizado:  {args.suavizado_m:g} m")
    print(f"GeoJSON:    {resultado.rutas['geojson']}")
    print(f"Mapa:       {resultado.rutas['png']}")
    if resultado.truncated:
        print("La cuenca puede estar cortada: sube MAX_BUFFER_KM.")
