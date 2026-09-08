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

import numpy as np
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


def facility_labels(catalog: pd.DataFrame) -> pd.Series:
    """Etiquetas legibles y **únicas** de establecimientos, tolerantes a nulos.

    Dos trampas, las dos encontradas al pasar del alcance de prueba al nacional:

    * En pandas, ``"texto" + NaN`` devuelve ``NaN``. Con un solo campo vacío la
      etiqueta entera pasaba a ser ``float``, y ordenar la lista resultante
      lanzaba ``TypeError: '<' not supported between 'float' and 'str'``. En un
      solo departamento no había ningún campo vacío, así que el fallo no aparecía.
    * Dos establecimientos pueden compartir nombre, categoría y distrito. Si la
      etiqueta no incluye el código, el diccionario etiqueta → código los colapsa
      en silencio y el simulador de escenarios asciende el establecimiento
      equivocado.
    """
    def parte(col: str, titulo: bool = False) -> pd.Series:
        s = catalog[col].astype("string")
        if titulo:
            s = s.str.title()
        # El relleno va después de capitalizar: en minúscula se lee como
        # marcador, mientras que "Sin Dato" parecería el nombre real del
        # establecimiento.
        return s.fillna("sin dato")

    return (
        parte("NOMBRE", titulo=True)
        + "  ·  " + parte("CATEGORIA")
        + "  ·  " + parte("DISTRITO", titulo=True)
        + ", " + parte("DEPARTAMENTO", titulo=True)
        + "  ·  " + parte("COD_IPRESS")
    )


def safe_row_idxmin(df: pd.DataFrame) -> pd.Series:
    """``df.idxmin(axis=1)`` tolerante a filas completamente NaN.

    Desde pandas 2.1, ``idxmin`` lanza ``ValueError: Encountered all NA values``
    en lugar de devolver NaN cuando una fila no tiene ningún valor. En esta matriz
    eso pasa de forma legítima y esperada: los orígenes marcados como "sin acceso
    vial" (a más de 20 km de cualquier vía) tienen la fila entera en NaN por
    decisión de la política de *fallback*, y a pie hay orígenes sin ningún
    establecimiento alcanzable. La fila sin valores no es un error de datos, así
    que el resultado correcto es NaN en esa posición, no una excepción.

    Devuelve una serie de etiquetas de columna, con ``pd.NA`` donde no hay mínimo.
    """
    arr = df.to_numpy(dtype="float64", na_value=np.nan)
    finite = np.isfinite(arr)
    has_any = finite.any(axis=1)
    pos = np.where(finite, arr, np.inf).argmin(axis=1)

    labels = pd.Series(pd.NA, index=df.index, dtype="object", name="idxmin")
    if has_any.any():
        cols = np.asarray(df.columns, dtype="object")
        labels.iloc[np.flatnonzero(has_any)] = cols[pos[has_any]]
    return labels


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
