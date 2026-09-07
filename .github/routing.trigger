Tocar este archivo dispara .github/workflows/routing.yml.

Existe para que la reconstrucción del grafo de OSRM (~40 minutos) no se ejecute
en cada commit de código, pero siga siendo reproducible con un solo cambio
rastreable en el historial.

corridas:
- 2026-09-06 — primera corrida nacional (25 departamentos, perfiles car/foot/bike)
