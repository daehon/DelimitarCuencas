#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lanza la delimitación de la cuenca aportante a un punto.

1. Edita LAT, LON y DEM.
2. Activa el entorno:  .venv\\Scripts\\Activate.ps1
3. Ejecuta:            python lanzar_cuenca.py
"""

from pathlib import Path

from delimitar_cuenca import delimitar_cuenca

# Punto de cierre (desagüe / estación / presa).
LAT = -25.634282
LON = -56.272815

# Radio inicial del DEM alrededor del punto (km).
BUFFER_KM = 20.0
MAX_BUFFER_KM = 80.0

# Modelo digital del terreno. Elige uno:
#   "glo30"   Copernicus GLO-30 (~30 m). Superficie: incluye árboles y tejados.
#   "glo90"   Copernicus GLO-90 (~90 m; a veces se cita como 80 m). Más rápido.
#   "fabdem"  Copernicus sin bosque ni edificios (~30 m). Mejor en llanuras.
#   "anadem"  Terreno de Sudamérica (~30 m). Recomendado en Paraguay.
DEM = "fabdem"

# Ajusta el punto al cauce más cercano dentro de SNAP_KM.
SNAP = True
SNAP_KM = 0.1

CARPETA = Path(__file__).resolve().parent
SALIDA = CARPETA / "output"
CACHE = CARPETA / "cache"


if __name__ == "__main__":
    resultado = delimitar_cuenca(
        lat=LAT,
        lon=LON,
        buffer_km=BUFFER_KM,
        max_buffer_km=MAX_BUFFER_KM,
        snap=SNAP,
        snap_km=SNAP_KM,
        dem=DEM,
        cache_dir=CACHE,
        out_dir=SALIDA,
        plot=True,
    )
    print()
    print(f"DEM:      {resultado.dem_fuente}")
    print(f"Área:     {resultado.area_km2:.3f} km²")
    print(f"Cierre:   {resultado.lat_snap:.5f}, {resultado.lon_snap:.5f}")
    print(f"GeoJSON:  {resultado.rutas['geojson']}")
    print(f"Mapa:     {resultado.rutas['png']}")
    if resultado.truncated:
        print("La cuenca puede estar cortada: sube MAX_BUFFER_KM.")
