# Acceso vial a establecimientos de salud resolutivos en el Perú

Mide **cuánto tarda la población peruana, por carretera, en llegar a un
establecimiento de salud con capacidad resolutiva** (categoría II-1 o superior,
operativo), y dónde están las peores brechas.

El alcance por defecto es **nacional**: los 25 departamentos, con cada punto de
demanda etiquetado además por región natural, de modo que el contraste
costa–sierra–selva se obtiene como un corte del resultado nacional. El alcance
se cambia con un parámetro, sin tocar código.

| | |
|---|---|
| **Puntos de demanda** | 5 000, con pesos de diseño que representan ~27,8 millones de habitantes |
| **Oferta** | Establecimientos resolutivos de RENIPRESS 2026 (II-1, II-2, II-E, III-1, III-2, III-E) |
| **Motor de ruteo** | OSRM en Docker sobre la red vial de OpenStreetMap (perfiles auto, pie, bicicleta) |
| **Dashboard** | `streamlit run app.py` — funciona sin Docker y sin conexión |
| **Informe** | `report/main.tex` + `report/main.pdf` |

---

## Inicio rápido

Lo único que hace falta para explorar los resultados es Python. **No se necesita
Docker**: la matriz de ruteo está versionada en el repositorio.

```bash
git clone https://github.com/luyov-belli/HW_02_2026_02.git
cd HW_02_2026_02

python -m venv .venv
# Windows:        .venv\Scripts\activate
# Linux / macOS:  source .venv/bin/activate

pip install -r requirements.txt
streamlit run app.py
```

## Regenerar todo desde cero

```bash
python -m src.acquisition     # descarga los insumos y escribe data/raw/manifest.json
python -m src.validation      # Fase 1: valida, corrige y muestrea → data/processed/
python -m src.routing         # Fase 2: matriz OD (requiere motor de ruteo, ver abajo)
python -m src.metrics         # Fase 3: métricas ponderadas → data/outputs/
python -m src.models          # 2SFCA y optimización de cobertura (MCLP)
python -m src.export          # Fase 5: figuras y tablas LaTeX → report/
cd report && pdflatex main.tex && pdflatex main.tex
```

Cada paso es idempotente y reutiliza lo que ya está en disco. La matriz de ruteo
se cachea con una clave que es el hash de (motor, perfil, coordenadas), así que
una segunda corrida **no vuelve a consultar el motor de ruteo**.

### El motor de ruteo

La corrida oficial usa **OSRM en Docker**, que corre en GitHub Actions
(`.github/workflows/routing.yml`). No hay que instalar nada: para lanzarla basta
con editar `.github/routing.trigger` y hacer push, o usar *Actions → Fase 2 —
OSRM en Docker → Run workflow*. El detalle está en
[`docker/README.md`](docker/README.md).

Si se quiere correr el ruteo en local:

```bash
# Con Docker (Linux/macOS, o Windows con WSL2 y permisos de administrador):
python -m src.acquisition --with-osm-pbf
bash scripts/osrm_build_and_route.sh car

# Sin Docker — grafo vial propio resuelto con scipy:
#   editar config.md → [routing].engine = "graph"
python -m src.acquisition
python -m src.validation
python -m src.routing --engine graph
```

El motor `graph` descarga las vías de OpenStreetMap acotadas al alcance activo
vía Overpass. Es práctico en el alcance `regions` o `dev`; a escala nacional
conviene OSRM. Sus limitaciones frente a OSRM están documentadas en `config.md`
y en el informe: trata la red como no dirigida y no reporta distancia de red.

---

## Cambiar el alcance del análisis

Todos los parámetros viven en [`config.md`](config.md), en un bloque `toml` que
`src/config.py` parsea con `tomllib`. **Ningún módulo tiene rutas, umbrales,
categorías, velocidades ni listas de departamentos escritos en el código.**

Para pasar del análisis nacional al de tres departamentos (uno por región
natural), se edita una línea:

```toml
[scope]
mode = "regions"        # "national" | "regions" | "dev"

[scope.regions]
costa  = "PIURA"
sierra = "HUANCAVELICA"
selva  = "LORETO"
```

También se puede sobrescribir por entorno sin editar el archivo:

```bash
HW02_SCOPE=regions python -m src.validation      # Linux/macOS
$env:HW02_SCOPE="regions"; python -m src.validation   # PowerShell
```

Los archivos de salida llevan el alcance en el nombre
(`metrics_district_national.csv`, `metrics_district_regions.csv`), así que las
dos corridas coexisten sin pisarse.

El modo `dev` usa un solo departamento pequeño (Tumbes) y sirve para probar el
pipeline completo en segundos.

---

## Estructura del repositorio

```
├── config.md                  ← todos los parámetros (única fuente de verdad)
├── requirements.txt
├── app.py                     ← Fase 4: dashboard Streamlit
├── src/
│   ├── config.py              ← parser de config.md
│   ├── utils.py               ← logging y registro estructurado de decisiones
│   ├── acquisition.py         ← Fase 1: descarga, manifiesto, hashes, altitud
│   ├── validation.py          ← Fase 1: validación geoespacial y muestreo
│   ├── routing.py             ← Fase 2: OSRM / grafo local / ORS, caché, fallback
│   ├── metrics.py             ← Fase 3: métricas ponderadas por población
│   ├── models.py              ← 2SFCA y cobertura máxima (MCLP)
│   └── export.py              ← Fase 5: figuras PDF y tablas LaTeX
├── scripts/
│   └── osrm_build_and_route.sh
├── docker/
│   ├── README.md              ← qué es OSRM en Docker y cómo levantarlo
│   └── docker-compose.yml
├── data/
│   ├── raw/                   ← insumos crudos (no versionados; ver manifest.json)
│   ├── raw_cache/             ← copia versionada de las fuentes peruanas (ver abajo)
│   ├── processed/             ← establecimientos, demanda, muestra, distritos
│   └── outputs/               ← matriz OD, métricas, reportes de calidad
├── report/
│   ├── main.tex               ← informe (usa tables/kpis.tex; sin cifras a mano)
│   ├── figures/
│   └── tables/
├── logs/                      ← logs de ejecución de cada fase
└── .github/workflows/
    └── routing.yml            ← OSRM en Docker, corrida oficial
```

---

## Fuentes de datos

| Insumo | Fuente | Licencia |
|---|---|---|
| Establecimientos de salud | RENIPRESS / SUSALUD, `RENIPRESS_2026_v6.csv` (act. 21-ago-2026) | Datos Abiertos del Estado Peruano |
| Centros poblados (demanda) | MTC sobre base INEI | Datos Abiertos del Estado Peruano |
| Límites administrativos | OCHA/HDX *Common Operational Datasets*, base INEI | CC-BY |
| Red vial | OpenStreetMap, extracto de Perú de Geofabrik | ODbL 1.0 |
| Altitud | SRTM 30 m vía OpenTopoData | Dominio público (NASA) |

Fechas de acceso, tamaños y SHA-256 de cada archivo en `data/raw/manifest.json`.

### Dos sustituciones documentadas

1. **Centros poblados: MTC en lugar de SIGMED.** El enunciado sugería el visor
   SIGMED del MINEDU. Se usa el listado del MTC, construido sobre la misma base
   del INEI, porque SIGMED es una aplicación legada sin extremo de descarga
   directa estable; depender de ella rompería la reproducibilidad. El archivo del
   MTC además trae población, viviendas y la clasificación urbano/rural del INEI,
   que SIGMED no expone.

2. **Copia versionada de las fuentes peruanas (`data/raw_cache/`).** El portal de
   datos abiertos de SUSALUD solo responde por HTTP —su extremo HTTPS está
   caído— y no es alcanzable desde la red de los runners de GitHub Actions. Sin
   respaldo, el pipeline no sería reproducible fuera del Perú. `src/acquisition.py`
   intenta siempre la descarga primero y solo cae al respaldo si falla,
   registrando en el manifiesto qué origen usó realmente y el SHA-256 del
   archivo, de modo que la sustitución es auditable.

---

## Qué encontró la validación (Fase 1)

Hallazgos reales del reporte de calidad
(`data/outputs/data_quality_report.json`), no ejemplos ilustrativos:

* **Latitud y longitud invertidas en el encabezado** del listado de centros
  poblados: la columna rotulada `Latitud (coord X)` contiene la longitud. El
  pipeline no confía en la etiqueta y decide por el rango de valores,
  aprovechando que en el Perú los intervalos de latitud y longitud son disjuntos.
* **7 067 establecimientos (26 %) sin coordenada** o con `(0,0)`, que no es un
  error de proyección sino el golfo de Guinea. Se recuperaron 7 061 imputando el
  centroide del distrito declarado; 58 establecimientos *resolutivos* descansan
  en una coordenada imputada y eso se declara como limitación.
* **`ESTADO` y `SITUACION` no discriminan**: valen `ACTIVO` y `REGISTRADO` en el
  100 % de los registros. El filtro de operatividad se apoya en `CONDICION`, y la
  consecuencia —RENIPRESS publicado no permite detectar cierres— se reporta como
  limitación en lugar de ocultarse.
* **4 894 registros con `CATEGORIA = "0"`**, que no corresponde a ninguna
  categoría de la NT 021-MINSA; se tratan como no resolutivos.

---

## Diseño muestral

El análisis está limitado a 5 000 puntos ruteables y el universo nacional tiene
87 504 centros poblados válidos, así que el muestreo es explícito y con pesos:

1. **Estrato de certeza** (π = 1): toda capital de distrito y todo centro poblado
   con ≥ 2 000 habitantes.
2. **Estratos de muestreo**: el resto por `distrito × ámbito`, con muestra
   proporcional al tamaño (PPS) y cupos repartidos por el método de divisores de
   Sainte-Laguë, que asigna los 5 000 cupos exactos sin sobrantes.
3. **Peso de diseño** de un punto PPS: `P_estrato / n_estrato`, el estimador de
   Horvitz–Thompson para `π ∝ población`.

Resultado: 5 000 puntos, los 1 853 distritos del alcance representados, y pesos
que suman exactamente la población del universo. Los distritos cuya estimación
descansa en pocos puntos se marcan como *alta varianza* y el dashboard permite
excluirlos del ranking.

---

## Más allá del mínimo

* **Corrida nacional** en lugar de tres departamentos, con estrategia de muestreo
  documentada.
* **Optimización de cobertura (MCLP)**: el simulador de escenarios no solo deja
  elegir establecimientos I-3/I-4 a mano, sino que resuelve *cuáles* conviene
  ascender, con garantía de al menos 63,2 % del óptimo y una cota superior que
  acota la brecha real.
* **2SFCA**: accesibilidad con capacidad, que revela distritos cercanos a un
  establecimiento saturado y que la métrica de tiempo declara bien atendidos.
* **Comparación temporal** reconstruida con `INICIO_ACTIVIDAD`, sin costo
  adicional de ruteo, y con el supuesto (que ningún resolutivo cerró) declarado.
* **Pipeline automatizado en CI** con OSRM en Docker, un perfil a la vez para
  caber en el disco del runner, y volcado de diagnóstico al resumen público del
  job.
* **Informe sin cifras a mano**: `src/export.py` genera `report/tables/kpis.tex`
  con macros LaTeX, así que recompilar tras una corrida nueva actualiza el texto
  del informe automáticamente.

---

## Limitaciones principales

La lista completa y ordenada por severidad está en la sección de Limitaciones del
informe. Las tres que más pueden mover las conclusiones:

1. **La selva no se recorre por carretera.** En Loreto, Ucayali y parte de
   Amazonas el transporte real es fluvial y aéreo. El pipeline marca los puntos
   sin acceso vial pero **no modela la red fluvial**: se prefirió declararlo antes
   que producir cifras con apariencia de precisión sin sustento.
2. **La oferta medida es un límite superior**, porque el registro no permite
   detectar cierres.
3. **Categoría no es capacidad efectiva.** Un II-1 con quirófano cerrado aparece
   igual que uno funcionando.

---

## Licencia

Código bajo la licencia MIT (ver `LICENSE`). Los datos conservan la licencia de
su fuente original, listada arriba y en `data/raw/manifest.json`.
