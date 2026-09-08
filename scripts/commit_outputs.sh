#!/usr/bin/env bash
# Commitea los artefactos generados por el pipeline a la rama en curso.
#
#   bash scripts/commit_outputs.sh "<mensaje>" <ruta> [<ruta>...]
#
# Existe separado del YAML por tres razones concretas, todas aprendidas fallando:
#
# 1. **Rutas explícitas, nunca `git add -f report`.** Los artefactos del informe
#    están en .gitignore (para que una corrida local no los versione) y los
#    workflows los fuerzan con `-f`. Pero `git add -f report` fuerza *todo* el
#    directorio, incluidos los intermedios de LaTeX (main.aux, main.log, main.out)
#    y cualquier directorio de aislamiento que haya quedado. Se pasan rutas.
#
# 2. **`git pull --rebase` no sirve aquí.** `actions/checkout` puede dejar el
#    repositorio en HEAD desacoplado, y en ese estado `git pull --rebase` aborta
#    con "fatal: You are not currently on a branch" (exit 128) sin explicar nada.
#    Se usa fetch + rebase contra la referencia remota explícita.
#
# 3. **Diagnóstico público.** Los logs de Actions exigen sesión iniciada; las
#    anotaciones no. Si algo falla, se publica el motivo como anotación.

set -uo pipefail

MENSAJE="${1:?uso: commit_outputs.sh <mensaje> <rutas...>}"
shift
RUTAS=("$@")
RAMA="${GITHUB_REF_NAME:?falta GITHUB_REF_NAME}"

fallar() {
  local detalle="$1"
  echo "::error title=Commit de resultados::${detalle}"
  {
    echo "## ❌ No se pudo commitear los resultados"
    echo
    echo "$detalle"
    echo
    echo '```'
    git status --short | head -n 40
    echo '```'
  } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
  exit 1
}

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# La acción de LaTeX corre en Docker como root y deja los archivos que genera con
# ese dueño; `git add` del usuario del runner falla al leerlos.
sudo chown -R "$(id -u):$(id -g)" report data logs 2>/dev/null || true

EXISTENTES=()
for ruta in "${RUTAS[@]}"; do
  [ -e "$ruta" ] && EXISTENTES+=("$ruta")
done
if [ "${#EXISTENTES[@]}" -eq 0 ]; then
  echo "ninguna de las rutas pedidas existe; nada que commitear"
  exit 0
fi

git add -f -- "${EXISTENTES[@]}" || fallar "git add falló sobre: ${EXISTENTES[*]}"

if git diff --cached --quiet; then
  echo "sin cambios que commitear"
  exit 0
fi

echo "--- se commitea ---"
git diff --cached --stat | tail -n 30

git commit -m "$MENSAJE" || fallar "git commit falló"

# Si otro workflow empujó mientras corría este, se rebasa encima y se reintenta.
for intento in 1 2 3; do
  if git push origin "HEAD:refs/heads/${RAMA}"; then
    echo "push correcto en el intento ${intento}"
    exit 0
  fi
  echo "push rechazado (intento ${intento}); se rebasa sobre origin/${RAMA}"
  git fetch origin "${RAMA}" || fallar "git fetch falló"
  git rebase "FETCH_HEAD" || {
    git rebase --abort 2>/dev/null || true
    fallar "el rebase sobre origin/${RAMA} entró en conflicto"
  }
done

fallar "el push siguió siendo rechazado tras tres intentos"
