# OSRM en Docker

El motor de ruteo de este trabajo es **OSRM** (Open Source Routing Machine), la
opción recomendada por el enunciado. Aquí está todo lo necesario para levantarlo,
tanto en la nube (que es como se produjo la corrida oficial) como en una máquina
local.

## Qué hace OSRM y por qué se eligió

OSRM preprocesa la red vial de OpenStreetMap en un grafo de contracción y expone
una API HTTP. Los dos servicios que usa el pipeline son:

| Servicio | Uso en este proyecto |
| --- | --- |
| `/table` | Matriz origen × destino completa (5 000 × 644) en lotes. Devuelve duración **y** distancia de red. |
| `/nearest` | Verificación de disponibilidad del servidor. |

La respuesta de `/table` incluye, para cada waypoint, la **distancia de enganche**
(*snapping*) entre la coordenada declarada y el punto de la red al que se ancló.
El pipeline la aprovecha para el análisis de snapping de la Fase 2 sin peticiones
adicionales.

Frente a las alternativas: OpenRouteService impone cuotas y no permite una matriz
de 3,2 millones de pares; un grafo en NetworkX puro no respeta sentidos únicos ni
restricciones de giro. OSRM sí, y corre offline.

## Cómo se ejecuta la corrida oficial (sin instalar nada)

En `.github/workflows/routing.yml`. El runner de Ubuntu ya trae Docker. Para
lanzarla basta con editar `.github/routing.trigger` y hacer push, o usar
*Actions → Fase 2 — OSRM en Docker → Run workflow*.

El workflow construye **un perfil a la vez** y borra el grafo antes del siguiente:
los tres perfiles ocupan ~9 GB y el runner tiene ~14 GB libres. La secuencia por
perfil está en `scripts/osrm_build_and_route.sh`.

## Cómo levantarlo en local (requiere Docker)

En Windows, Docker Desktop necesita WSL2 y permisos de administrador. En Linux o
macOS basta con Docker.

```bash
# 1) insumo vial
python -m src.acquisition --with-osm-pbf

# 2) construir y rutear el perfil car
bash scripts/osrm_build_and_route.sh car
```

O manualmente, con `docker compose`:

```bash
export OSM_PBF_DIR=$PWD/osrm-car
mkdir -p "$OSM_PBF_DIR" && cp data/raw/peru-latest.osm.pbf "$OSM_PBF_DIR/peru.osm.pbf"

docker compose -f docker/docker-compose.yml run --rm extract-car
docker compose -f docker/docker-compose.yml run --rm partition
docker compose -f docker/docker-compose.yml run --rm customize
docker compose -f docker/docker-compose.yml up -d routed-car

python -m src.routing --engine osrm --stage check     # verifica disponibilidad
python -m src.routing --engine osrm --stage primary   # matriz completa
```

## Parámetros que importan

* `--algorithm mld` (multi-level Dijkstra). Frente a CH, el preprocesamiento es
  varias veces más rápido y admite `/table` grande, que es lo que necesitamos.
* `--max-table-size 10000`. El valor por defecto es 100 coordenadas, insuficiente
  para lotes de 50 orígenes × 100 destinos.
* Perfiles: `/opt/car.lua`, `/opt/foot.lua`, `/opt/bicycle.lua` dentro de la imagen.

`osrm-routed` **no acepta POST** en `/table`, así que las coordenadas viajan en la
URL. Por eso `config.md` limita a `osrm_max_coords_per_request = 150`: mantiene la
URL bien por debajo de 8 KB. Contra un servidor local, el costo de hacer más
peticiones es de milisegundos.

## Si OSRM no está disponible

`config.md` permite `engine = "graph"`: un grafo vial propio construido desde OSM
(Overpass o el shapefile de Geofabrik) y resuelto con Dijkstra multiorigen de
`scipy`. No requiere Docker. Sus limitaciones frente a OSRM están documentadas en
el informe: trata la red como no dirigida y no reporta distancia de red, solo
tiempo.
