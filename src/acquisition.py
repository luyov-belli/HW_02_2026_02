"""Fase 1 — Adquisición de insumos.

Descarga cada fuente declarada en ``[sources]`` de ``config.md``, verifica el
archivo, calcula su SHA-256 y escribe un manifiesto (``data/raw/manifest.json``)
con URL, licencia, fecha de acceso, tamaño y hash. El manifiesto es lo que la
Fase 5 cita como "fecha de acceso" de cada fuente.

Notas de operación relevantes para el informe:

* El portal de datos abiertos de SUSALUD **solo responde por HTTP**; su endpoint
  HTTPS está caído a la fecha de acceso. Se documenta y se usa HTTP con
  verificación por hash en lugar de sustituir la fuente.
* El listado de centros poblados se toma del MTC (base INEI) publicado en
  datosabiertos.gob.pe. Se eligió sobre el visor SIGMED de MINEDU porque este
  último es una aplicación legada sin endpoint de descarga directa estable, lo
  que rompería la reproducibilidad del pipeline. Se declara como sustitución
  documentada.

Uso::

    python -m src.acquisition                # todo salvo el shapefile de OSM
    python -m src.acquisition --with-osm-shp # incluye los 618 MB de Geofabrik
    python -m src.acquisition --only renipress ccpp
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

import requests

from .config import CFG, PROJECT_ROOT
from .utils import DecisionLog, file_sha256, get_logger, utcnow

LOG = get_logger("acquisition")

#: Fuentes que no se descargan por defecto por su tamaño.
HEAVY = {"osm_shp", "osm_pbf"}

CHUNK = 1 << 20


def _download(
    url: str,
    dest: Path,
    timeout: int = 120,
    mirrors: list[str] | None = None,
    attempts_per_url: int = 4,
) -> dict[str, Any]:
    """Descarga con streaming, reintentos y espejos.

    Geofabrik devuelve 502/503 de forma intermitente, así que la política es:
    varios intentos con espera creciente sobre la URL principal y, si sigue
    fallando, los espejos declarados en ``config.md``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": "HW02-accesibilidad-salud/1.0 (curso Data Science)"}

    last: Exception | None = None
    for candidate in [url, *(mirrors or [])]:
        for attempt in range(1, attempts_per_url + 1):
            try:
                with requests.get(
                    candidate, stream=True, timeout=timeout, headers=headers
                ) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length") or 0)
                    written = 0
                    with open(tmp, "wb") as fh:
                        for block in resp.iter_content(chunk_size=CHUNK):
                            if block:
                                fh.write(block)
                                written += len(block)
                    if total and written != total:
                        raise OSError(f"descarga incompleta: {written} de {total} bytes")
                tmp.replace(dest)
                return {
                    "bytes": dest.stat().st_size,
                    "sha256": file_sha256(dest),
                    "downloaded_at_utc": utcnow(),
                    "attempts": attempt,
                    "source_url": candidate,
                }
            except Exception as err:  # noqa: BLE001 - se reintenta y se reporta
                last = err
                LOG.warning("intento %d falló para %s: %s", attempt, candidate, err)
                time.sleep(min(60, 10 * attempt))
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"no se pudo descargar {url} ni sus espejos: {last}")


def fetch_source(key: str, spec: dict[str, Any], force: bool = False) -> dict[str, Any]:
    """Descarga (o reutiliza) una fuente y devuelve su entrada de manifiesto.

    Si la descarga falla y la fuente declara ``fallback_path``, se usa la copia
    versionada en el repositorio. Esto implementa la recomendación explícita del
    enunciado ("si un portal cae, cachea y explica"): el portal de datos abiertos
    de SUSALUD solo responde por HTTP y no es alcanzable desde la red de los
    runners de GitHub Actions, de modo que sin respaldo el pipeline no sería
    reproducible fuera del Perú. El manifiesto registra qué origen se usó
    realmente y el SHA-256, para que la sustitución sea auditable.
    """
    dest = CFG.path("raw") / spec["filename"]
    entry: dict[str, Any] = {
        "key": key,
        "url": spec["url"],
        "filename": spec["filename"],
        "license": spec.get("license", "no declarada"),
        "note": spec.get("note"),
        "path": str(dest.relative_to(PROJECT_ROOT)),
    }
    if dest.exists() and not force:
        LOG.info("%s ya presente (%.1f MB), se reutiliza", dest.name, dest.stat().st_size / 1e6)
        entry.update(
            bytes=dest.stat().st_size,
            sha256=file_sha256(dest),
            downloaded_at_utc="reutilizado",
            attempts=0,
            origin="cache_local",
        )
        return entry

    LOG.info("descargando %s -> %s", spec["url"], dest.name)
    has_fallback = bool(spec.get("fallback_path"))
    try:
        entry.update(
            _download(
                spec["url"],
                dest,
                mirrors=spec.get("mirrors"),
                # Con respaldo disponible no tiene sentido gastar diez minutos
                # de CI reintentando contra un portal que no responde.
                timeout=45 if has_fallback else 120,
                attempts_per_url=2 if has_fallback else 4,
            )
        )
        entry["origin"] = "descarga"
    except Exception as err:  # noqa: BLE001
        fallback = spec.get("fallback_path")
        if not fallback:
            raise
        src = PROJECT_ROOT / fallback
        if not src.exists():
            raise FileNotFoundError(
                f"la descarga de '{key}' falló ({err}) y no existe el respaldo {src}"
            ) from err
        LOG.warning(
            "descarga de '%s' fallida (%s); se usa la copia versionada %s",
            key, str(err)[:120], src.name,
        )
        # Algunas fuentes se respaldan en otro formato que el original (los
        # límites vienen como .shp.zip y se respaldan como GeoPackage, que ocupa
        # la mitad), así que el destino puede ser distinto del nombre publicado.
        target = spec.get("fallback_target")
        dest = (PROJECT_ROOT / target) if target else dest
        _restore_fallback(src, dest)
        entry["path"] = str(dest.relative_to(PROJECT_ROOT))
        entry.update(
            bytes=dest.stat().st_size,
            sha256=file_sha256(dest),
            downloaded_at_utc="respaldo versionado",
            attempts=0,
            origin="fallback_repositorio",
            fallback_path=fallback,
            download_error=str(err)[:200],
        )
    LOG.info("%s listo (%.1f MB, origen: %s)", dest.name, entry["bytes"] / 1e6, entry["origin"])
    return entry


def _restore_fallback(src: Path, dest: Path) -> None:
    """Copia el respaldo a ``data/raw``, descomprimiéndolo si viene en gzip."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix == ".gz":
        with gzip.open(src, "rb") as fin, open(dest, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    else:
        shutil.copyfile(src, dest)


def extract_zip(key: str, members_contain: str | None = None) -> Path:
    """Extrae un zip de ``data/raw`` a ``data/raw/<key>/`` y devuelve la carpeta."""
    spec = CFG["sources"][key]
    zpath = CFG.path("raw") / spec["filename"]
    outdir = CFG.path("raw") / key
    outdir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        if members_contain:
            names = [n for n in names if members_contain.lower() in n.lower()]
        todo = [n for n in names if not (outdir / n).exists()]
        if todo:
            LOG.info("extrayendo %d miembros de %s", len(todo), zpath.name)
            zf.extractall(outdir, members=todo)
        else:
            LOG.info("%s ya extraído", zpath.name)
    return outdir


def find_layer(
    folder: Path, pattern: str, suffixes: str | tuple[str, ...] = (".shp", ".gpkg")
) -> Path:
    """Busca recursivamente una capa cuyo nombre contenga ``pattern``.

    Se aceptan varios formatos y se devuelve el primero que exista, en el orden
    dado: la fuente publicada es un shapefile, pero el respaldo versionado es un
    GeoPackage, que pesa la mitad.
    """
    if isinstance(suffixes, str):
        suffixes = (suffixes,)
    for suffix in suffixes:
        matches = sorted(
            p for p in folder.rglob(f"*{suffix}") if pattern.lower() in p.name.lower()
        )
        if matches:
            return matches[0]
    raise FileNotFoundError(
        f"no se encontró ninguna capa '{pattern}' con extensión {suffixes} en {folder}"
    )


def fetch_elevation(
    lats: list[float], lons: list[float], cache_path: Path
) -> "pd.Series":
    """Altitud SRTM 30 m para una lista de puntos, con caché en disco.

    Se consulta OpenTopoData en lotes respetando su límite de una petición por
    segundo. El resultado se versiona en ``data/processed``, de modo que ni CI ni
    una segunda corrida vuelven a llamar al servicio. Si el servicio no responde,
    se devuelven NaN y el análisis cruzado reporta la altitud como no disponible
    en lugar de fallar.
    """
    import pandas as pd

    spec = CFG["sources"]["elevation"]
    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        LOG.info("altitud desde caché: %s (%d puntos)", cache_path.name, len(cached))
        return cached.set_index("point_key")["elevation_m"]

    keys = [f"{la:.5f},{lo:.5f}" for la, lo in zip(lats, lons, strict=True)]
    unique_keys = list(dict.fromkeys(keys))
    batch = int(spec["batch"])
    values: dict[str, float] = {}
    LOG.info(
        "consultando altitud de %d puntos únicos en %d lotes",
        len(unique_keys), math.ceil(len(unique_keys) / batch),
    )
    for i in range(0, len(unique_keys), batch):
        chunk = unique_keys[i : i + batch]
        try:
            resp = requests.get(
                spec["url"],
                params={"locations": "|".join(chunk)},
                timeout=90,
                headers={"User-Agent": "HW02-accesibilidad-salud/1.0"},
            )
            resp.raise_for_status()
            for key, item in zip(chunk, resp.json()["results"], strict=True):
                values[key] = item.get("elevation")
        except Exception as err:  # noqa: BLE001
            LOG.warning("lote de altitud %d falló: %s", i // batch, err)
            for key in chunk:
                values.setdefault(key, None)
        time.sleep(float(spec["rate_limit_s"]))

    series = pd.Series(values, name="elevation_m", dtype="float64")
    series.index.name = "point_key"
    series.reset_index().to_parquet(cache_path, index=False)
    ok = int(series.notna().sum())
    LOG.info("altitud resuelta para %d de %d puntos", ok, len(series))
    return series


def probe_wayback_renipress() -> dict[str, Any]:
    """Deja constancia de que no existen snapshots históricos de RENIPRESS.

    La comparación temporal (innovación) se reconstruye con ``INICIO_ACTIVIDAD``
    en :mod:`src.models`; esta función solo documenta por qué no se usó el
    Internet Archive, que es la alternativa obvia.
    """
    spec = CFG["sources"]["renipress_historic"]
    result: dict[str, Any] = {
        "method_used": spec["method"],
        "wayback_snapshots_found": 0,
        "wayback_reachable": False,
    }
    try:
        resp = requests.get(
            spec["wayback_cdx_api"],
            params={
                "url": spec["wayback_probe_url"],
                "output": "json",
                "matchType": "domain",
                "filter": "original:.*RENIPRESS.*csv",
                "collapse": "urlkey",
                "limit": "50",
            },
            timeout=60,
        )
        resp.raise_for_status()
        rows = resp.json()
        result["wayback_reachable"] = True
        result["wayback_snapshots_found"] = max(len(rows) - 1, 0) if rows else 0
    except Exception as err:  # noqa: BLE001
        LOG.warning("Internet Archive no respondió: %s", err)
        result["error"] = str(err)[:200]
    LOG.info(
        "sondeo Wayback: %d snapshots de RENIPRESS; se usa %s",
        result["wayback_snapshots_found"],
        spec["method"],
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fase 1 — descarga de insumos")
    parser.add_argument("--force", action="store_true", help="re-descargar todo")
    parser.add_argument(
        "--with-osm-shp",
        action="store_true",
        help="incluir el shapefile de Geofabrik (618 MB, solo para engine='graph')",
    )
    parser.add_argument(
        "--with-osm-pbf",
        action="store_true",
        help="incluir el .osm.pbf (256 MB, solo para construir OSRM localmente)",
    )
    parser.add_argument("--only", nargs="*", default=None, help="claves de [sources]")
    args = parser.parse_args(argv)

    sources: dict[str, Any] = dict(CFG["sources"])
    sources.pop("renipress_historic", None)

    if args.only:
        keys = [k for k in args.only if k in sources]
    else:
        keys = [k for k in sources if k not in HEAVY]
        if args.with_osm_shp:
            keys.append("osm_shp")
        if args.with_osm_pbf:
            keys.append("osm_pbf")

    dlog = DecisionLog(stage="acquisition")
    manifest: dict[str, Any] = {"generated_at_utc": utcnow(), "sources": []}

    for key in keys:
        spec = sources[key]
        if "url" not in spec:
            continue
        entry = fetch_source(key, spec, force=args.force)
        manifest["sources"].append(entry)
        dlog.record(
            check="descarga de insumo",
            dataset=key,
            n_affected=1,
            n_total=1,
            action=(
                f"archivo {entry['filename']} ({entry['bytes'] / 1e6:.1f} MB), "
                f"origen: {entry['origin']}"
            ),
            justification=(
                f"licencia: {entry['license']}"
                + (
                    "; el portal no respondió y se usó la copia versionada en el "
                    "repositorio, con SHA-256 registrado para auditoría"
                    if entry["origin"] == "fallback_repositorio"
                    else ""
                )
            ),
            sha256=entry["sha256"],
            url=entry["url"],
            origin=entry["origin"],
        )

    if "boundaries" in keys:
        # Si se usó el respaldo, no hay zip que extraer: ya es un GeoPackage.
        used_fallback = any(
            e["key"] == "boundaries" and e["origin"] == "fallback_repositorio"
            for e in manifest["sources"]
        )
        if not used_fallback:
            extract_zip("boundaries")
    if "osm_shp" in keys:
        extract_zip("osm_shp", members_contain="roads")

    if CFG.get("innovation", "run_temporal", default=False):
        probe = probe_wayback_renipress()
        manifest["renipress_historic"] = probe
        dlog.record(
            check="disponibilidad de versiones históricas de RENIPRESS",
            dataset="renipress_historic",
            n_affected=probe["wayback_snapshots_found"],
            n_total=1,
            action=(
                "el Internet Archive no conserva los CSV; la serie histórica se "
                "reconstruye con INICIO_ACTIVIDAD"
            ),
            justification=(
                "innovación: evolución de la oferta resolutiva sin costo adicional "
                "de ruteo, porque la matriz OD cubre todas las resolutivas actuales"
            ),
            wayback_reachable=probe["wayback_reachable"],
        )

    (CFG.path("raw") / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    dlog.save("acquisition_log")
    LOG.info("manifiesto escrito con %d fuentes", len(manifest["sources"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
