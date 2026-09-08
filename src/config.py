"""Lectura de parámetros desde ``config.md``.

Todos los parámetros del proyecto viven en el bloque ``toml`` de ``config.md``.
Este módulo lo extrae y lo parsea con ``tomllib`` (librería estándar desde 3.11),
de modo que el archivo siga siendo legible como documentación y a la vez sea la
única fuente de verdad para el código.

Uso típico::

    from src.config import CFG
    CFG["metrics"]["time_bands_min"]
    CFG.departments()        # departamentos según el modo de alcance activo
    CFG.path("processed")    # ruta absoluta, creada si no existe
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = PROJECT_ROOT / "config.md"

_FENCE = re.compile(r"```toml\s*\n(.*?)\n```", re.DOTALL)


class Config:
    """Vista de solo lectura sobre el bloque TOML de ``config.md``."""

    def __init__(self, path: Path | None = None) -> None:
        self.config_path = Path(path) if path else CONFIG_FILE
        self._data = self._load(self.config_path)

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        blocks = _FENCE.findall(text)
        if not blocks:
            raise ValueError(
                f"{path} no contiene un bloque ```toml con los parámetros."
            )
        # Si alguien agrega más bloques, se concatenan en orden de aparición.
        return tomllib.loads("\n".join(blocks))

    # ------------------------------------------------------------------ acceso
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, *keys: str, default: Any = None) -> Any:
        """``CFG.get("routing", "osrm", "timeout_s")`` con default seguro."""
        node: Any = self._data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def as_dict(self) -> dict[str, Any]:
        return self._data

    # ---------------------------------------------------------------- alcance
    @property
    def mode(self) -> str:
        """``"national"`` o ``"regions"``. Sobrescribible con ``HW02_SCOPE``."""
        return os.environ.get("HW02_SCOPE", self._data["scope"]["mode"]).lower()

    def departments(self) -> list[str]:
        """Departamentos incluidos en la corrida, según el modo activo."""
        if self.mode == "regions":
            preset = self._data["scope"]["regions"]
            return [preset["costa"], preset["sierra"], preset["selva"]]
        if self.mode == "dev":
            return list(self._data["scope"]["dev"]["departments"])
        return list(self._data["scope"]["national"]["departments"])

    def region_of_department(self, department: str) -> str:
        """Etiqueta costa/sierra/selva de un departamento (proxy INEI)."""
        nr = self._data["natural_region"]
        dep = department.strip().upper()
        for label in ("costa", "sierra", "selva"):
            if dep in nr[f"{label}_departments"]:
                return label
        return "no_determinada"

    # ------------------------------------------------------------------ rutas
    #: Directorios cuyos archivos NO llevan el alcance en el nombre y que sí se
    #: versionan, así que una corrida de prueba los sobrescribiría: las figuras y
    #: tablas porque el informe LaTeX las referencia con nombre fijo
    #: (``fig1_choropleth_acceso``), y los logs porque son un entregable y
    #: ``logs/<etapa>.log`` se abre en modo *append*. El aislamiento tiene que ser
    #: por directorio, no por nombre de archivo. ``outputs`` entra por lo mismo:
    #: los registros de decisiones (``data_quality_report.json``,
    #: ``routing_log_car.csv``, …) llevan nombre fijo porque el dashboard y el
    #: informe los buscan así.
    _SCOPE_ISOLATED = ("figures", "tables", "logs", "outputs")

    @property
    def artifact_suffix(self) -> str:
        """Sufijo de directorio para las corridas que no son el estudio.

        Devuelve ``"_dev"`` en el alcance de prueba, ``"_test"`` bajo pytest y
        cadena vacía en las corridas reales. Sin esto, dos atajos cómodos
        corrompen artefactos versionados en silencio:

        * ``HW02_SCOPE=dev python -m src.export`` sobrescribe las figuras
          nacionales con las de un solo departamento, y como el informe las
          referencia por nombre fijo, nada cambia de nombre para avisar. Ya
          ocurrió una vez.
        * ``pytest`` corre sin alcance declarado, así que ejercitar las funciones
          reales de validación deja líneas de prueba dentro de
          ``logs/validation.log``, que es un entregable.
        """
        if "pytest" in sys.modules:
            return "_test"
        if self.mode == "dev":
            return "_dev"
        return ""

    def path(self, key: str, *parts: str, create: bool = True) -> Path:
        """Ruta absoluta a partir de ``[paths]``; crea el directorio si falta."""
        base = PROJECT_ROOT / self._data["paths"][key]
        suffix = self.artifact_suffix
        if suffix and key in self._SCOPE_ISOLATED:
            base = base.with_name(base.name + suffix)
        if create:
            base.mkdir(parents=True, exist_ok=True)
        return base.joinpath(*parts) if parts else base

    def scoped_name(self, stem: str, ext: str) -> str:
        """Nombre de archivo con sufijo de alcance, p. ej. ``metrics_national.csv``."""
        return f"{stem}_{self.mode}.{ext.lstrip('.')}"

    # ------------------------------------------------------------------ varios
    @property
    def seed(self) -> int:
        return int(self._data["project"]["random_seed"])

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """``(lon_min, lat_min, lon_max, lat_max)`` del Perú continental."""
        v = self._data["validation"]
        return (v["lon_min"], v["lat_min"], v["lon_max"], v["lat_max"])

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Config {self.config_path.name} mode={self.mode} "
            f"n_dep={len(self.departments())}>"
        )


CFG = Config()
