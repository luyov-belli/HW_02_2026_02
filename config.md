# `config.md` — Parámetros de la corrida

Este archivo es la **única fuente de verdad** de todos los parámetros del proyecto.
Ningún módulo de `src/` contiene rutas, umbrales, códigos de categoría, velocidades o
listas de departamentos escritos en el código: todo se lee desde el bloque `toml` de
abajo mediante `src/config.py` (que usa `tomllib` de la librería estándar).

Para cambiar el alcance del estudio, editar `[scope].mode` y volver a ejecutar
`python -m src.pipeline`. No hay que tocar código.

---

## 1. Alcance del análisis (`[scope]`)

El proyecto soporta **dos modos** y el resultado de todas las fases se adapta solo:

| `mode` | Qué hace | Uso |
| --- | --- | --- |
| `national` | Los 25 departamentos del Perú. Es el modo **por defecto** y el que se reporta. | Corrida principal |
| `regions` | Solo los tres departamentos declarados en `[scope.regions]`, uno por región natural. | Cumplimiento del mínimo del enunciado / desarrollo rápido |

En modo `national` cada punto de demanda además se etiqueta por **región natural**
(costa / sierra / selva) usando la regla de `[natural_region]`, de modo que el análisis
nacional *contiene* el contraste costa–sierra–selva en lugar de sustituirlo.

## 2. Definición de "establecimiento resolutivo" (`[facilities]`)

Un establecimiento es **resolutivo** si y solo si cumple las dos condiciones:

1. Está **operativo**: `ESTADO`, `SITUACION` y `CONDICION` dentro de las listas blancas.
2. Su `CATEGORIA` está en `resolutive_categories`.

> **Nota metodológica.** En `RENIPRESS_2026_v6.csv` **todos** los registros tienen
> `ESTADO = "ACTIVO"`, por lo que ese campo no discrimina. La condición de operatividad
> se construye por tanto con `SITUACION` y `CONDICION`. Esto se documenta en el reporte
> de calidad (Fase 1) y en el informe LaTeX.

Las categorías `II-E` y `III-E` (establecimientos especializados de 2.º y 3.er nivel)
**se incluyen** como resolutivas: por norma (R.M. 546-2011/MINSA) tienen capacidad de
internamiento y especialidades, es decir el atributo que define "resolutivo" en el
enunciado. La lista es un parámetro, así que la decisión es auditable y reversible.

## 3. Muestreo de puntos de demanda (`[demand]`)

El enunciado limita el análisis a 5 000 puntos de demanda. En modo `national` hay
~99 900 centros poblados, así que se aplica un diseño muestral **explícito y con pesos**:

1. **Estrato de certeza** (π = 1): todo centro poblado que sea **capital de distrito** o
   que tenga población ≥ `certainty_pop_threshold`. Peso de diseño = su propia población.
2. **Estratos de muestreo**: el resto se estratifica por `departamento × (urbano/rural)`
   y dentro de cada estrato se toma una muestra **PPS** (probabilidad proporcional al
   tamaño, sin reemplazo) hasta agotar `max_demand_points`. La asignación de cupos entre
   estratos es proporcional a la población no cubierta por el estrato de certeza.
3. **Peso de diseño** de un punto PPS = `P_estrato_restante / n_estrato`, que es el
   estimador de Horvitz–Thompson para π_i ∝ pob_i. Con esto todos los agregados
   poblacionales de la Fase 3 siguen siendo no sesgados.

Implicancia declarada: los centros poblados rurales muy pequeños están representados por
análogos ponderados, no individualmente. Por eso `src/metrics.py` reporta el **número
efectivo de puntos muestreados por distrito** y marca los distritos con menos de
`min_points_per_district` como estimación de alta varianza.

## 4. Motor de ruteo (`[routing]`)

`engine` selecciona el backend en tiempo de ejecución. Los tres están implementados
detrás de la misma interfaz (`src/routing.py`):

| `engine` | Descripción | Dónde corre |
| --- | --- | --- |
| `osrm` | **Recomendado y usado en la corrida oficial.** Cliente HTTP contra `osrm-routed` (servicio `/table` para la matriz, `/route` para geometrías). Grafo de contracción MLD construido desde `peru-latest.osm.pbf`. | Contenedor Docker levantado por `.github/workflows/routing.yml` (o `localhost` si hay Docker) |
| `graph` | Grafo vial propio: `gis_osm_roads_free_1.shp` de Geofabrik → topología por vértices compartidos → Dijkstra multiorigen con `scipy.sparse.csgraph`. | Local, sin Docker. Se usa para desarrollo, como **fallback** y para validación cruzada contra OSRM |
| `ors` | OpenRouteService API con caché en disco y throttling. Solo emergencia. | Requiere `ORS_API_KEY` en el entorno |

`straight_line_speed_kmh` **no es un motor de ruteo**: se usa únicamente como último
recurso para puntos irruteables (ver `[routing.fallback]`) y para la comparación
"línea recta vs. red" que exige la Fase 5.

## 5. Umbrales y métricas (`[metrics]`)

Bandas de cobertura en minutos, medida de desigualdad, y regla urbano/rural.
La clasificación urbano/rural **no se inventa**: se toma el campo `CLASIFICACIÓN INEI`
del listado de centros poblados; `urban_rule` documenta el criterio de respaldo
(población del centro poblado ≥ `urban_pop_threshold`) para los registros sin ese campo.

---

## Bloque de parámetros

```toml
[project]
name          = "Accesibilidad vial a establecimientos de salud resolutivos en el Perú"
author        = "Adrián Leo Luyo Vargas"
course        = "Data Science with Python — HW_02 2026-02"
random_seed   = 20260909
crs_geographic = "EPSG:4326"
crs_metric     = "EPSG:32718"   # UTM 18S, para distancias de snapping en metros

[scope]
mode = "national"               # "national" | "regions" | "dev"

[scope.dev]
# Alcance mínimo para probar el pipeline completo en segundos (no se reporta).
# Se activa con la variable de entorno HW02_SCOPE=dev.
departments = ["TUMBES"]

[scope.regions]
# Preset del modo "regions": un departamento por región natural.
costa  = "PIURA"
sierra = "HUANCAVELICA"
selva  = "LORETO"

[scope.national]
# 25 departamentos tal como aparecen escritos en RENIPRESS (sin tildes, mayúsculas).
departments = [
  "AMAZONAS", "ANCASH", "APURIMAC", "AREQUIPA", "AYACUCHO", "CAJAMARCA", "CALLAO",
  "CUSCO", "HUANCAVELICA", "HUANUCO", "ICA", "JUNIN", "LA LIBERTAD", "LAMBAYEQUE",
  "LIMA", "LORETO", "MADRE DE DIOS", "MOQUEGUA", "PASCO", "PIURA", "PUNO",
  "SAN MARTIN", "TACNA", "TUMBES", "UCAYALI",
]

[natural_region]
# Regla para etiquetar región natural. Se aplica en cascada, primero altitud.
# Sin DEM disponible se usa el proxy oficial de INEI: departamentos de costa/sierra/selva
# y, para los departamentos mixtos, la altitud del centro poblado si existe.
costa_max_elev_m  = 500
sierra_min_elev_m = 500
selva_max_elev_m  = 500
# Departamentos íntegramente de una sola región (INEI):
costa_departments  = ["CALLAO", "ICA", "LAMBAYEQUE", "LA LIBERTAD", "TUMBES", "PIURA"]
sierra_departments = ["APURIMAC", "AYACUCHO", "CUSCO", "HUANCAVELICA", "JUNIN", "PASCO", "PUNO", "ANCASH", "AREQUIPA", "CAJAMARCA", "HUANUCO", "MOQUEGUA", "TACNA"]
selva_departments  = ["AMAZONAS", "LORETO", "MADRE DE DIOS", "SAN MARTIN", "UCAYALI"]

[facilities]
resolutive_categories = ["II-1", "II-2", "II-E", "III-1", "III-2", "III-E"]
# Categorías candidatas a "ascenso" en el simulador de escenarios (Fase 4) y en el MCLP.
upgrade_candidate_categories = ["I-3", "I-4"]
estado_whitelist    = ["ACTIVO"]
situacion_whitelist = ["REGISTRADO", "ACTIVO"]
condicion_whitelist = ["ACTIVO"]
lat_column = "NORTE"
lon_column = "ESTE"
id_column  = "COD_IPRESS"
# Capacidad relativa por categoría, usada solo por el 2SFCA (innovación).
# Proxy normativo: número de camas de referencia por categoría (MINSA NT 021).
[facilities.capacity_proxy]
"II-1"  = 30
"II-2"  = 120
"II-E"  = 60
"III-1" = 300
"III-2" = 400
"III-E" = 200

[demand]
max_demand_points        = 5000
certainty_pop_threshold  = 2000
min_points_per_district  = 3
drop_zero_population     = true
urban_pop_threshold      = 2000     # respaldo si falta CLASIFICACIÓN INEI

[validation]
# Bounding box continental del Perú (enunciado).
lon_min = -81.4
lon_max = -68.6
lat_min = -18.4
lat_max = -0.04
# Un par (lat, lon) se considera invertido si (lon, lat) sí cae dentro del bbox.
detect_swapped_coords       = true
fix_swapped_coords          = true
drop_outside_bbox           = true
# Tolerancia para el chequeo punto-en-polígono declarado (grados ~ 2.2 km).
district_polygon_tolerance_deg = 0.02
duplicate_key               = ["COD_IPRESS"]
encodings_to_try            = ["utf-8", "utf-8-sig", "latin-1", "cp1252"]

[routing]
engine   = "osrm"               # "osrm" | "graph" | "ors"
profiles = ["car", "foot", "bike"]
primary_profile = "car"
# Perfiles secundarios solo sobre una submuestra, para acotar el costo de CI.
secondary_profile_sample = 1200
# Destinos de los perfiles secundarios: unión de los k establecimientos más
# cercanos en auto de cada origen de la submuestra (ver src.routing.run_secondary).
secondary_top_k = 15
snap_max_distance_m = 2000
cache_dir           = "data/cache/routing"
# Se pasa a osrm-routed como --max-table-size.
osrm_max_table_size = 10000
# osrm-routed no acepta POST en /table, así que las coordenadas viajan en la URL.
# Este tope mantiene la URL por debajo de 8 KB con margen. Contra un servidor local
# el costo de más peticiones es de milisegundos.
osrm_max_coords_per_request = 150
table_chunk_sources = 50

[routing.osrm]
base_urls = { car = "http://127.0.0.1:5000", foot = "http://127.0.0.1:5001", bike = "http://127.0.0.1:5002" }
timeout_s = 300
retries   = 3

[routing.graph]
roads_source  = "overpass"      # "overpass" | "geofabrik"
roads_layer   = "gis_osm_roads_free_1.shp"
# Se colapsan los vértices intermedios: solo se conservan como nodos del grafo los
# extremos de vía y los vértices compartidos por dos o más vías (cruces). Reduce el
# grafo de ~10^7 a ~10^6 nodos sin perder la longitud real de los arcos.
collapse_interior_vertices = true
coord_snap_decimals        = 6
# Velocidades libres en km/h por clase OSM. Fuente: perfil car.lua de OSRM,
# ajustado hacia abajo para vías no pavimentadas del Perú.
[routing.graph.speeds_kmh]
motorway = 90
trunk = 80
primary = 70
secondary = 60
tertiary = 50
unclassified = 35
residential = 25
living_street = 15
service = 15
track = 20
road = 30
path = 8
footway = 5
pedestrian = 5
steps = 2
cycleway = 12
default = 30
[routing.graph.excluded_classes]
car  = ["footway", "steps", "pedestrian", "path", "cycleway", "bridleway"]
foot = ["motorway", "motorway_link", "trunk", "trunk_link"]
bike = ["motorway", "motorway_link", "steps"]

[routing.fallback]
# Política documentada para puntos irruteables (Fase 2).
# 1) reintentar snapping con radio ampliado
retry_snap_radius_m = 10000
# 2) si sigue fallando, imputar por línea recta penalizada
straight_line_speed_kmh   = 25
straight_line_detour_factor = 1.35
# 3) si el punto queda a más de este umbral del nodo vial más cercano, se marca
#    como "sin acceso vial" y se reporta aparte en lugar de imputarse.
no_road_access_threshold_m = 20000

[metrics]
time_bands_min      = [30, 60, 120]
# Velocidad supuesta del análisis naíf con el que se contrasta la red vial en la
# Fase 5. Es deliberadamente distinta de [routing.fallback].straight_line_speed_kmh:
# aquella penaliza puntos irruteables, esta reproduce lo que asumiría alguien que
# usara Haversine. 40 km/h es el valor habitual en la literatura de accesibilidad
# a servicios de salud para "vehículo en red mixta".
naive_haversine_speed_kmh = 40
inequality_measure  = "gini"        # "gini" | "theil"
aggregation_levels  = ["district", "province", "department", "natural_region", "national"]
worst_districts_n   = 25
urban_rule          = "inei_classification"

[innovation]
run_2sfca            = true
run_mclp             = true
run_temporal         = true
mclp_upgrade_budget  = [5, 10, 20, 50]
mclp_coverage_minutes = 60
sfca_catchment_minutes = 60
sfca_decay          = "gaussian"     # "step" | "gaussian"

[sources]
[sources.renipress]
url       = "http://datos.susalud.gob.pe/sites/default/files/RENIPRESS_2026_v6.csv"
dict_url  = "http://datos.susalud.gob.pe/sites/default/files/Diccionario_Datos_RENIPRESS.xlsx"
filename  = "RENIPRESS_2026_v6.csv"
sep       = ";"
license   = "Datos Abiertos del Estado Peruano (ODC-BY). SUSALUD."
[sources.ccpp]
url       = "https://www.datosabiertos.gob.pe/sites/default/files/ListadoCentroPobladosMTC.xlsx"
filename  = "ListadoCentroPobladosMTC.xlsx"
license   = "Datos Abiertos del Estado Peruano. MTC, sobre base INEI."
note      = "El encabezado del archivo tiene invertidas las etiquetas de latitud y longitud; ver src/validation.py."
[sources.boundaries]
url       = "https://data.humdata.org/dataset/54fc7f4d-f4c0-4892-91f6-2fe7c1ecf363/resource/61faa8d6-fbfa-4d44-a94d-8f3b0241277a/download/per_admin_boundaries.shp.zip"
filename  = "per_admin_boundaries.shp.zip"
district_layer_pattern = "admin3"
# Los pcode de HDX son "PE" + UBIGEO del INEI (PE030101 -> distrito 030101).
pcode_prefix = "PE"
# La versión vigente en HDX es v01, valid_on 2020-07-14, con 1 873 distritos.
# RENIPRESS 2026 reporta 1 890 UBIGEO distritales: los distritos creados después de
# 2020 no tienen polígono. El pipeline lo cuenta y lo reporta en lugar de silenciarlo.
expected_districts = 1873
license   = "OCHA / HDX Common Operational Datasets, fuente INEI."
[sources.osm_pbf]
# Insumo de OSRM (motor oficial). Se descarga dentro del workflow de CI.
url       = "https://download.geofabrik.de/south-america/peru-latest.osm.pbf"
mirrors   = ["https://osm.download.movisda.io/south-america/peru-latest.osm.pbf"]
filename  = "peru-latest.osm.pbf"
license   = "OpenStreetMap contributors, ODbL 1.0."
[sources.osm_shp]
# Insumo opcional del motor "graph" a escala nacional (618 MB).
url       = "https://download.geofabrik.de/south-america/peru-latest-free.shp.zip"
filename  = "peru-latest-free.shp.zip"
license   = "OpenStreetMap contributors, ODbL 1.0."
[sources.overpass]
# Fuente de red vial del motor "graph" acotada al alcance activo. Es la que se usa
# en local (modo "regions"): Overpass devuelve exactamente las vías del bbox pedido,
# lo que evita depender de la disponibilidad de Geofabrik.
endpoints = [
  "https://overpass-api.de/api/interpreter",
  "https://overpass.kumi.systems/api/interpreter",
  "https://overpass.private.coffee/api/interpreter",
]
# Overpass rechaza peticiones sin User-Agent identificable (HTTP 406).
user_agent      = "HW02-accesibilidad-salud/1.0 (curso Data Science; adrianluyo@zest.pe)"
timeout_s       = 600
highway_regex   = "^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|track|road|path|footway|pedestrian|steps|cycleway)(_link)?$"
bbox_pad_deg    = 0.15
tile_size_deg   = 1.0
license         = "OpenStreetMap contributors, ODbL 1.0."
[sources.renipress_historic]
# Innovación (comparación temporal). El Internet Archive no conserva snapshots de
# los CSV de datos.susalud.gob.pe (verificado con la CDX API: 0 resultados), así que
# la serie histórica se reconstruye del propio archivo usando INICIO_ACTIVIDAD, que
# está completo en las 676 IPRESS resolutivas. Ventaja adicional: no requiere
# volver a rutear, porque la matriz OD ya cubre todas las resolutivas.
method             = "inicio_actividad"
date_column        = "INICIO_ACTIVIDAD"
date_format        = "%d/%m/%Y"
snapshot_years     = [2010, 2015, 2020, 2026]
# Se intenta Wayback igualmente y se registra el resultado, para dejar constancia.
wayback_cdx_api    = "http://web.archive.org/cdx/search/cdx"
wayback_probe_url  = "datos.susalud.gob.pe/sites/default/files/*"

[paths]
raw        = "data/raw"
processed  = "data/processed"
outputs    = "data/outputs"
cache      = "data/cache"
logs       = "logs"
figures    = "report/figures"
tables     = "report/tables"

[report]
figure_format = "pdf"
figure_dpi    = 200
table_format  = "booktabs"
```
