Tocar este archivo dispara .github/workflows/routing.yml.

Existe para que la reconstrucción del grafo de OSRM (~40 minutos) no se ejecute
en cada commit de código, pero siga siendo reproducible con un solo cambio
rastreable en el historial.

corridas:
- 2026-09-06 — primera corrida nacional (25 departamentos, perfiles car/foot/bike)
corridas:
- 2026-09-06 22:15 UTC-5 — primera corrida nacional. Fallo en Fase 1: Geofabrik
  devolvia 502/503 y el espejo configurado no existia. Se dividio la Fase 1 en
  tres pasos, se agrego volcado de diagnostico al step summary (publico) y se
  quito el espejo inexistente.
- 2026-09-06 22:45 UTC-5 — segunda corrida nacional (25 departamentos,
  perfiles car/foot/bike).
