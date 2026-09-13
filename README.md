# Delimitar cuencas

Este programa calcula **la cuenca hidrográfica que aporta agua hacia un punto** (por ejemplo una estación, un puente o una presa).

Solo hace falta la **latitud** y la **longitud** de ese punto. El programa descarga un mapa de elevaciones, sigue el terreno cuesta arriba y dibuja el polígono de la cuenca.

Hace falta **conexión a internet** la primera vez (y cada vez que se trabaje en una zona nueva).

---

## Modelos de terreno disponibles

Hay cuatro modelos. La diferencia importante es **si el mapa “ve” la vegetación o el suelo**.

Copernicus (`glo30` y `glo90`) es un **modelo de superficie**: mide la altura de copas de árboles y tejados. En llanuras con bosque (por ejemplo Paraguay) el agua parece fluir por las copas y la cuenca sale mal.

FABDEM y ANADEM parten de Copernicus pero **han quitado la vegetación** (y FABDEM también los edificios). Queda un mapa del **terreno**, más adecuado para delimitar cuencas.

| Valor en `DEM` | Nombre | Resolución | ¿Incluye vegetación? | Cobertura | Cuándo usarlo |
|---|---|---|---|---|---|
| `glo30` | Copernicus GLO-30 | ~30 m | Sí (árboles y tejados) | Casi todo el mundo | Europa o zonas con poco bosque |
| `glo90` | Copernicus GLO-90 | ~90 m (a veces se dice 80 m) | Sí | Todo el mundo | Cuencas muy grandes; más rápido |
| `fabdem` | FABDEM v1.2 | ~30 m | **No** (vegetación y edificios eliminados) | Todo el mundo | Llanuras o bosque, en cualquier continente |
| `anadem` | ANADEM v1 | ~30 m | **No** (vegetación eliminada) | Solo Sudamérica | Paraguay y el resto de Sudamérica |

Para Paraguay: usa **`anadem`** o **`fabdem`**.

---

## Cómo usar `lanzar_cuenca.py`

Esta es la forma recomendada. No hace falta escribir comandos largos: se editan tres líneas y se ejecuta el archivo.

### 1. Abrir el proyecto

La carpeta es `DelimitarCuencas` (en el Escritorio / OneDrive).

En Cursor: abre `lanzar_cuenca.py` y luego **Terminal → New Terminal**. Esa terminal ya está en la carpeta correcta.

### 2. Activar el entorno (cada vez que abras una terminal nueva)

```powershell
.\.venv\Scripts\Activate.ps1
```

Si a la izquierda aparece `(.venv)`, está listo.  
Si dice que no se pueden ejecutar scripts:

```powershell
.\.venv\Scripts\python.exe lanzar_cuenca.py
```

(La primera vez en un ordenador nuevo, instala las librerías con los pasos de la sección 4.)

### 3. Editar el punto, el modelo y (si hace falta) la rugosidad

Abre `lanzar_cuenca.py` y cambia estas líneas (están cerca del principio):

```python
LAT = -25.634282
LON = -56.272815
DEM = "anadem"
```

- **LAT** y **LON**: coordenadas del punto de cierre (desagüe, estación, presa). En Google Maps, al hacer clic, la primera cifra es la latitud y la segunda la longitud.
- **DEM**: uno de `glo30`, `glo90`, `fabdem` o `anadem`.

Si la cuenca es grande, puedes subir también:

```python
BUFFER_KM = 20.0
MAX_BUFFER_KM = 80.0
```

(`BUFFER_KM` es el radio de terreno que se descarga al inicio, en kilómetros.)

Si la cuenca sale **demasiado pequeña**, ajusta `LAMINA_M` y `SUAVIZADO_M` (ver la sección siguiente).

Guarda el archivo (Ctrl+S). También puedes lanzar sin editar, por ejemplo:

```powershell
python lanzar_cuenca.py --lamina-m 3 --suavizado-m 90
```

### 4. Lanzar

```powershell
python lanzar_cuenca.py
```

Tardará uno o varios minutos. La primera vez descarga el modelo a la carpeta `cache`. Con `fabdem` esa primera descarga es más pesada (un ZIP grande que luego se reutiliza).

Los resultados aparecen en **`output/`**, por ejemplo:

`output/cuenca_-25.63482_-56.27406_anadem_buf20_max80_snap0.8_lam2_suav60`

| Archivo | Qué es |
|---|---|
| `.geojson` | El polígono de la cuenca. Ábrelo en QGIS, ArcGIS o Google Earth. |
| `.tif` | El mapa de elevación recortado a la cuenca. |
| `.png` | Un dibujo para revisar a simple vista (norte arriba). |
| `_meta.json` | Datos resumidos: área en km², elevaciones, lámina, suavizado, etc. |

El archivo que más se usa después es el **GeoJSON**. El nombre incluye modelo, buffer, snap, lámina y suavizado, para no pisar resultados de otra ejecución.

---

## Cuenca demasiado pequeña: lámina y suavizado

El programa usa **D8**: cada celda manda **toda** su agua a **un** vecino (el de mayor pendiente hacia abajo).

Subir un número “de lámina” en el cálculo de la pendiente **casi no cambia nada**: si hay cualquier bajada local, el agua se va por ahí y no “salta” el lomo. Por eso la cuenca seguía saliendo minúscula. Ahora `LAMINA_M` **recorta** esos lomos en el DEM (los baja al terreno de al lado). Además, al rellenar depresiones se deja una pendiente mínima hacia el desagüe: en llanuras (Paraguay) sin eso todo el llano drena hacia el mismo rumbo y el punto interior queda con unas pocas celdas.

El ajuste al cauce (`SNAP`) sigue el flujo **aguas abajo** hasta un río, en lugar de quedarse en el píxel clicado.

| Parámetro | Qué hace | 0 significa | Valores típicos |
|---|---|---|---|
| `LAMINA_M` / `--lamina-m` | Recorta lomos de hasta esa altura (metros) | No recorta | Terreno: **2–5**. Copernicus (`glo30`/`glo90`, con árboles): **10–20** |
| `SUAVIZADO_M` / `--suavizado-m` | Radio de la ventana (media + ancho máximo del lomo a recortar) | Sin suavizar ni ensanchar el recorte | DEM 30 m: **60–90**. `glo90`: **120** |

Cómo afinarlos:

1. Cuenca minúscula: sube `LAMINA_M`. Con `glo30`/`glo90` en bosque, prueba 15 o 20.
2. Lomos de varias celdas: sube `SUAVIZADO_M` (más ancho de recorte).
3. El polígono se come otra llanura o río: baja `LAMINA_M` o `SUAVIZADO_M`.
4. En Paraguay el mapa de superficie (`glo30`/`glo90`) ve las copas: mejor `anadem` o `fabdem`.

Los valores usados quedan en `_meta.json` (`lamina_m`, `suavizado_m`).

---

## Forma alternativa: línea de comandos

Sirve para no editar `lanzar_cuenca.py`. Con el entorno activado:

```powershell
python delimitar_cuenca.py --lat -25.634 --lon -56.273 --dem anadem --buffer-km 20 --lamina-m 2 --suavizado-m 60
```

Opciones útiles: `--dem`, `--buffer-km`, `--max-buffer-km`, `--lamina-m`, `--suavizado-m`, `--no-snap`, `--no-plot`.  
Lista completa: `python delimitar_cuenca.py --help`.

---

## Preparar el ordenador (solo la primera vez)

1. Instala [Python](https://www.python.org/downloads/) (versión 3.12 o posterior). En Windows, marca **Add python.exe to PATH**.
2. Abre una terminal en la carpeta del proyecto.
3. Copia y pega, una línea cada vez:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

En Linux o macOS los mismos pasos están comentados al inicio de `requirements.txt`.

---

## Si algo sale mal

- **“python no se reconoce”**: Python no está en el PATH. Reinstálalo marcando *Add python.exe to PATH*.
- **La cuenca se corta o parece incompleta**: sube `MAX_BUFFER_KM` y vuelve a lanzar.
- **La cuenca es minúscula / muy sensible a rugosidades**: sube `LAMINA_M` (con `glo90` prueba 15–20 m) y, si hace falta, `SUAVIZADO_M`. Ver la sección *Cuenca demasiado pequeña*. En Paraguay evita `glo30`/`glo90` si puedes usar `anadem` o `fabdem`.
- **El polígono no parece un río**: acerca el punto al cauce, o deja `SNAP = True`.
- **En Paraguay o llanuras con vegetación la cuenca sale rara**: no uses `glo30`; cambia a `anadem` o `fabdem`.
- **ANADEM da error fuera de Sudamérica**: usa `fabdem` o Copernicus.
- **Error de red**: hace falta internet para la primera descarga. Luego se reutiliza `cache`.
