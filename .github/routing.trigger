Tocar este archivo dispara .github/workflows/routing.yml.

Existe para que la reconstrucción del grafo de OSRM (~40 minutos) no se ejecute
en cada commit de código, pero siga siendo reproducible con un solo cambio
rastreable en el historial.

Corridas
--------
1. 2026-09-06 22:15 — falló en la Fase 1. Causa: el portal de SUSALUD solo
   responde por HTTP y no es alcanzable desde la red de los runners; los
   reintentos consumieron 10 minutos y el espejo de Geofabrik configurado no
   existía. Correcciones: Fase 1 dividida en tres pasos, diagnóstico volcado al
   resumen del job, espejo inexistente eliminado.
2. 2026-09-06 22:40 — mismo fallo, ya con los pasos divididos, lo que confirmó
   que el problema era RENIPRESS y no la descarga de OpenStreetMap.
3. 2026-09-06 23:00 — con respaldo versionado de RENIPRESS y del listado de
   centros poblados. La Fase 1a bajó de 10 min a 2m49s, pero siguió fallando:
   el candidato restante era HDX, que está detrás de Cloudflare y rechaza
   clientes no navegador desde algunas redes.
4. 2026-09-06 23:10 — respaldo versionado también para los límites distritales
   (GeoPackage, la mitad del tamaño del shapefile) y scripts/run_step.sh, que
   publica la cola del error como anotación pública para que un fallo en CI sea
   diagnosticable sin permisos sobre el repositorio.
   Corrida nacional completa: 25 departamentos, perfiles car / foot / bike.
5. 2026-09-06 23:15 - la anotacion publica revelo el error real: el bucle de descarga incluia las fuentes que son servicios (altitud, Overpass) y reventaba con KeyError. HDX descarga bien desde el runner. Corrida nacional completa.
6. 2026-09-06 23:15 — primera corrida en la que OSRM completó los tres perfiles
   (car, foot y bike, 27 minutos de grafo). Falló en el último paso, el análisis
   multimodal: `idxmin(axis=1)` sobre filas todas-NaN. Como el paso de commit no
   tenía `if: !cancelled()`, los 27 minutos de ruteo se descartaron y solo
   sobrevivió el artefacto. Corregidas ambas cosas: el bug y la política de
   commit.
7. 2026-09-07 — recálculo de la matriz nacional con `safe_row_idxmin` y el
   workflow ya extendido a las fases 3 a 5 y la compilación del PDF.
8. 2026-09-07 13:10 (sobre `main`, tras mergear el PR #2) — **el ruteo salió
   entero**: los tres perfiles de OSRM, el análisis multimodal y las fases 3 a 5.
   Fallaron los dos últimos pasos:
   - `Compilar el informe`, exit 12. Causa encontrada por inspección, sin
     necesidad del log: tres encabezados de tabla llevaban `≤` (U+2264), que la
     codificación T1 de LaTeX no puede representar, e `inputenc` aborta.
   - `Commit de resultados`, exit 128, así que los 27 minutos de grafos se
     perdieron otra vez pese al `if: !cancelled()`.
   Correcciones: el commit de datos pasa a ejecutarse **antes** de compilar el
   PDF (lo caro se salva primero), la mecánica de git se movió a
   `scripts/commit_outputs.sh` con rutas explícitas y reintento con rebase, y se
   agregó un paso que publica el error de pdflatex como anotación pública.
9. 2026-09-07 — esta corrida. Debe dejar en `data/outputs/` la matriz OD nacional
   y en `report/` el PDF compilado.
