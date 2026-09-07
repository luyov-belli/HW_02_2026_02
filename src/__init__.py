"""Pipeline de accesibilidad vial a establecimientos de salud resolutivos (HW_02).

Módulos por fase:

* :mod:`src.acquisition` — Fase 1, descarga y verificación de insumos.
* :mod:`src.validation`  — Fase 1, validación geoespacial y reporte de calidad.
* :mod:`src.routing`     — Fase 2, motores de ruteo (OSRM / grafo local / ORS) y caché.
* :mod:`src.metrics`     — Fase 3, métricas ponderadas por población.
* :mod:`src.models`      — Innovación, 2SFCA y optimización de ubicación (MCLP).
* :mod:`src.export`      — Fase 5, figuras y tablas LaTeX.
* :mod:`src.pipeline`    — orquestador de una corrida completa.
"""

__version__ = "1.0.0"
