#!/usr/bin/env bash
# Ejecuta un comando y, si falla, publica la cola de su salida como anotación de
# GitHub Actions.
#
# Existe por una razón concreta: los *logs* de Actions exigen sesión iniciada en
# el repositorio, pero las *anotaciones* de un run son públicas. Sin esto, un
# fallo en CI es un "exit code 1" sin diagnóstico para quien no tenga permisos.
#
#   bash scripts/run_step.sh "Fase 1a" python -m src.acquisition

set -uo pipefail

LABEL="${1:?uso: run_step.sh <etiqueta> <comando...>}"
shift

LOGFILE="$(mktemp)"
set +e
"$@" 2>&1 | tee "$LOGFILE"
STATUS="${PIPESTATUS[0]}"
set -e

if [ "$STATUS" -ne 0 ]; then
  # Las anotaciones son de una línea: se colapsan los saltos y se recorta.
  TAIL="$(tail -n 20 "$LOGFILE" | tr '\n' ' ' | tr -s ' ' | tail -c 900)"
  echo "::error title=${LABEL} (exit ${STATUS})::${TAIL}"
  {
    echo "## ❌ ${LABEL} falló (exit ${STATUS})"
    echo '```'
    tail -n 40 "$LOGFILE"
    echo '```'
  } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
fi

rm -f "$LOGFILE"
exit "$STATUS"
