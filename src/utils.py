"""Utilidades transversales: logging, hashes de insumos y registro de decisiones.

El enunciado exige que "el pipeline registre todas las decisiones: correcciones
hechas, registros descartados, tasas de recuperación y justificación". Eso se
implementa con dos piezas:

* :func:`get_logger` — log a consola y a ``logs/<etapa>.log``.
* :class:`DecisionLog` — acumulador estructurado de decisiones que se serializa a
  JSON y a CSV para que el dashboard (Fase 4) y el informe (Fase 5) lean lo mismo
  que se imprimió en consola.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .config import CFG

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"


def get_logger(stage: str) -> logging.Logger:
    """Logger con salida a consola y a ``logs/<stage>.log`` (append)."""
    logger = logging.getLogger(f"hw02.{stage}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(_LOG_FORMAT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    logfile = CFG.path("logs") / f"{stage}.log"
    fh = logging.FileHandler(logfile, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.propagate = False
    return logger


def file_sha256(path: str | Path, chunk: int = 1 << 20) -> str:
    """SHA-256 de un archivo, para trazabilidad de los insumos crudos."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def environment_stamp() -> dict[str, str]:
    """Huella del entorno de ejecución, para reproducibilidad."""
    return {
        "timestamp_utc": utcnow(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pandas": pd.__version__,
        "scope_mode": CFG.mode,
    }


@dataclass
class DecisionLog:
    """Registro estructurado de decisiones de calidad de datos.

    Cada entrada responde a: qué se revisó, sobre cuántos registros, cuántos se
    corrigieron, cuántos se descartaron y **por qué**.
    """

    stage: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)

    def record(
        self,
        check: str,
        dataset: str,
        n_affected: int,
        action: str,
        justification: str,
        n_total: int | None = None,
        **extra: Any,
    ) -> None:
        entry: dict[str, Any] = {
            "stage": self.stage,
            "dataset": dataset,
            "check": check,
            "n_affected": int(n_affected),
            "n_total": int(n_total) if n_total is not None else None,
            "share_affected": (
                round(n_affected / n_total, 6) if n_total else None
            ),
            "action": action,
            "justification": justification,
            "timestamp_utc": utcnow(),
        }
        entry.update(extra)
        self.entries.append(entry)

    def set_counter(self, key: str, value: int) -> None:
        self.counters[key] = int(value)

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.entries)

    def save(self, stem: str) -> tuple[Path, Path]:
        """Escribe ``<stem>.json`` y ``<stem>.csv`` en ``data/outputs``."""
        out = CFG.path("outputs")
        payload = {
            "environment": environment_stamp(),
            "counters": self.counters,
            "decisions": self.entries,
        }
        jpath = out / f"{stem}.json"
        cpath = out / f"{stem}.csv"
        jpath.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.to_frame().to_csv(cpath, index=False, encoding="utf-8")
        return jpath, cpath


def read_csv_robust(
    path: str | Path,
    sep: str = ",",
    encodings: list[str] | None = None,
    logger: logging.Logger | None = None,
    **kwargs: Any,
) -> tuple[pd.DataFrame, str]:
    """Lee un CSV probando codificaciones en orden y devuelve la que funcionó.

    El enunciado pide detectar problemas de codificación (UTF-8 vs latin-1); en
    lugar de asumir una, se prueban las declaradas en ``[validation]`` y se
    registra cuál resolvió el archivo.
    """
    encodings = encodings or CFG["validation"]["encodings_to_try"]
    last_err: Exception | None = None
    for enc in encodings:
        try:
            df = pd.read_csv(path, sep=sep, encoding=enc, dtype=str, **kwargs)
        except (UnicodeDecodeError, UnicodeError) as err:
            last_err = err
            if logger:
                logger.info("codificación %s falló en %s", enc, Path(path).name)
            continue
        if logger:
            logger.info(
                "%s leído con codificación %s (%d filas)",
                Path(path).name,
                enc,
                len(df),
            )
        return df, enc
    raise UnicodeDecodeError(  # pragma: no cover
        "hw02", b"", 0, 1, f"ninguna codificación funcionó para {path}: {last_err}"
    )


#: Columnas identificadoras y su ancho con ceros a la izquierda. Al pasar por
#: CSV, pandas las interpreta como enteros y "010109" vuelve como 10109, lo que
#: rompe silenciosamente todos los cruces. Cualquier lectura de un CSV de salida
#: debe pasar por :func:`read_output_csv`.
ID_COLUMNS: dict[str, int] = {
    "ubigeo": 6,
    "UBIGEO": 6,
    "COD_IPRESS": 8,
    "ccpp_code": 10,
    "adm3_pcode": 8,
}


def coerce_id_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Devuelve ``df`` con las columnas identificadoras como texto con relleno."""
    for col, width in ID_COLUMNS.items():
        if col in df.columns:
            df[col] = (
                df[col]
                .astype("string")
                .str.replace(r"\.0$", "", regex=True)
                .str.strip()
                .str.zfill(width)
            )
    return df


def read_output_csv(path: str | Path, **kwargs: Any) -> pd.DataFrame:
    """Lee un CSV de ``data/outputs`` preservando los identificadores."""
    return coerce_id_columns(pd.read_csv(path, **kwargs))


def mojibake_score(series: pd.Series) -> int:
    """Cuenta valores con síntomas típicos de doble codificación."""
    pattern = r"Ã.|Â.|ï¿½|�"
    return int(series.astype("string").str.contains(pattern, regex=True, na=False).sum())


def lossy_ascii_score(series: pd.Series) -> int:
    """Cuenta valores donde un carácter no ASCII fue reemplazado por ``?``.

    Es un daño distinto del mojibake y no se detecta con el patrón anterior: la
    doble codificación deja rastros recuperables (``Ã±`` → ``ñ``), mientras que
    la sustitución por ``?`` es **irreversible**. Ocurre cuando el sistema de
    origen exporta a una codificación que no puede representar el carácter.

    Se cuenta un ``?`` como pérdida solo si está rodeado de letras, para no
    contar signos de interrogación legítimos.
    """
    s = series.astype("string")
    return int(s.str.contains(r"[A-Za-z]\?[A-Za-z]|^\?[A-Za-z]", regex=True, na=False).sum())
