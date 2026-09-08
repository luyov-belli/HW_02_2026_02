#!/usr/bin/env bash
# Publica el diagnóstico de un fallo de pdflatex como anotaciones de Actions.
#
#   bash scripts/latex_diag.sh
#
# Por qué existe: los logs de Actions exigen sesión iniciada en el repositorio y
# las anotaciones no. En la corrida 9 el paso de diagnóstico anterior no encontró
# nada porque leía `report/main.log` **antes** de arreglar el dueño: la acción de
# LaTeX corre en Docker como root y deja sus archivos con ese dueño, así que el
# `grep` del usuario del runner fallaba y el `2>/dev/null` se comía el motivo.
#
# Además el error de LaTeX no siempre está en una línea que empiece con "!"
# (p. ej. un paquete ausente lo reporta latexmk, no pdflatex), de modo que se
# publica la cola del log completo troceada en varias anotaciones.

set -uo pipefail

LOG="report/main.log"

# 1. Recuperar la propiedad de lo que dejó el contenedor.
sudo chown -R "$(id -u):$(id -g)" report 2>/dev/null || true

# 2. Inventario: si el log no existe, el fallo fue antes de invocar a pdflatex.
INVENTARIO="$(ls -la report 2>&1 | head -n 25)"
{
  echo "## ❌ La compilación del informe falló"
  echo
  echo "### Contenido de report/"
  echo '```'
  echo "$INVENTARIO"
  echo '```'
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"

if [ ! -f "$LOG" ]; then
  echo "::error title=LaTeX::no se generó ${LOG}. Contenido de report/: $(echo "$INVENTARIO" | tr '\n' ' ' | tr -s ' ' | tail -c 700)"
  exit 0
fi

# 3. Las líneas que empiezan con "!" son el error de LaTeX propiamente dicho.
ERRORES="$(grep -n -A4 '^!' "$LOG" | head -n 60)"
if [ -n "$ERRORES" ]; then
  {
    echo "### Errores de LaTeX"
    echo '```'
    echo "$ERRORES"
    echo '```'
  } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
  echo "::error title=LaTeX::$(echo "$ERRORES" | tr '\n' ' ' | tr -s ' ' | head -c 900)"
fi

# 4. La cola del log, troceada: una anotación por bloque de 800 caracteres.
{
  echo "### Cola de $LOG"
  echo '```'
  tail -n 80 "$LOG"
  echo '```'
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"

COLA="$(tail -n 60 "$LOG" | tr '\n' ' ' | tr -s ' ')"
TOTAL=${#COLA}
BLOQUE=800
INDICE=0
POS=0
while [ "$POS" -lt "$TOTAL" ] && [ "$INDICE" -lt 6 ]; do
  INDICE=$((INDICE + 1))
  echo "::error title=main.log ${INDICE}::${COLA:$POS:$BLOQUE}"
  POS=$((POS + BLOQUE))
done
