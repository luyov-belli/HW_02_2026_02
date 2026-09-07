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
