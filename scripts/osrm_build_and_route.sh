#!/usr/bin/env bash
# Construye el grafo de OSRM para un perfil, levanta osrm-routed y ejecuta la
# etapa de ruteo correspondiente. Al terminar borra el grafo, porque los tres
# perfiles juntos no caben en el disco de un runner de GitHub Actions (~14 GB
# libres frente a ~3 GB por perfil).
#
#   ./scripts/osrm_build_and_route.sh car
#   ./scripts/osrm_build_and_route.sh foot
#
# Requiere Docker y el archivo data/raw/peru-latest.osm.pbf.

set -euo pipefail

PROFILE="${1:?uso: osrm_build_and_route.sh <car|foot|bike>}"
IMAGE="${OSRM_IMAGE:-ghcr.io/project-osrm/osrm-backend:latest}"
PBF="${OSM_PBF:-data/raw/peru-latest.osm.pbf}"
MAX_TABLE="${OSRM_MAX_TABLE_SIZE:-10000}"
WORKDIR="osrm-${PROFILE}"

case "$PROFILE" in
  car)  LUA=/opt/car.lua;      PORT=5000 ;;
  foot) LUA=/opt/foot.lua;     PORT=5001 ;;
  bike) LUA=/opt/bicycle.lua;  PORT=5002 ;;
  *) echo "perfil desconocido: $PROFILE" >&2; exit 2 ;;
esac

[ -f "$PBF" ] || { echo "no existe $PBF" >&2; exit 1; }

cleanup() {
  docker rm -f "osrm-$PROFILE" >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "::group::OSRM $PROFILE — preprocesamiento"
mkdir -p "$WORKDIR"
cp "$PBF" "$WORKDIR/peru.osm.pbf"
df -h . | tail -1

docker run --rm -v "$PWD/$WORKDIR:/data" "$IMAGE" \
  osrm-extract -p "$LUA" /data/peru.osm.pbf
docker run --rm -v "$PWD/$WORKDIR:/data" "$IMAGE" \
  osrm-partition /data/peru.osrm
docker run --rm -v "$PWD/$WORKDIR:/data" "$IMAGE" \
  osrm-customize /data/peru.osrm
# El .pbf ya no hace falta y libera 256 MB antes de levantar el servidor.
rm -f "$WORKDIR/peru.osm.pbf"
du -sh "$WORKDIR"
echo "::endgroup::"

echo "::group::OSRM $PROFILE — servidor en :$PORT"
docker run -d --name "osrm-$PROFILE" -p "$PORT:5000" \
  -v "$PWD/$WORKDIR:/data" "$IMAGE" \
  osrm-routed --algorithm mld --max-table-size "$MAX_TABLE" /data/peru.osrm

for i in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$PORT/nearest/v1/$PROFILE/-77.0428,-12.0464" >/dev/null; then
    echo "osrm-routed listo tras ${i}s"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "osrm-routed no respondió en 60 s" >&2
    docker logs "osrm-$PROFILE" || true
    exit 1
  fi
  sleep 1
done
echo "::endgroup::"

echo "::group::Ruteo $PROFILE"
if [ "$PROFILE" = "${PRIMARY_PROFILE:-car}" ]; then
  python -m src.routing --engine osrm --stage primary
else
  python -m src.routing --engine osrm --stage secondary --profile "$PROFILE"
fi
echo "::endgroup::"
