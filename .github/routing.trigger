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
   resumen público del job, espejo inexistente eliminado.
2. 2026-09-06 22:40 — mismo fallo, ya con los pasos divididos, lo que confirmó
   que el problema era RENIPRESS y no la descarga de OpenStreetMap.
3. 2026-09-06 23:00 — con copia versionada de las fuentes peruanas en
   data/raw_cache/ como respaldo auditable. Corrida nacional completa:
   25 departamentos, perfiles car / foot / bike.
