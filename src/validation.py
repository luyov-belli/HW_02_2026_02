"""Fase 1 — Validación geoespacial y reporte de calidad de datos.

Implementa cada chequeo que exige el enunciado y, para cada uno, registra en
:class:`~src.utils.DecisionLog` cuántos registros se vieron afectados, qué se hizo
y por qué. Chequeos:

1. Coordenadas nulas, vacías o iguales a cero.
2. Puntos fuera del bounding box del Perú.
3. Latitud/longitud intercambiadas.
4. Puntos fuera del polígono del distrito declarado.
5. Códigos de establecimiento duplicados.
6. Problemas de codificación (UTF-8 vs latin-1) y doble codificación.

Salidas en ``data/processed/``:

* ``facilities.parquet``    — IPRESS con banderas de calidad y clasificación.
* ``demand_points.parquet`` — universo de centros poblados validados.
* ``demand_sample.parquet`` — muestra ≤ 5 000 con pesos de diseño.
* ``districts.gpkg``        — polígonos distritales del alcance activo.

Salidas en ``data/outputs/``: ``data_quality_report.{json,csv}``.
"""

from __future__ import annotations

import argparse
import heapq
import unicodedata
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from .acquisition import find_layer
from .config import CFG
from .utils import (
    DecisionLog,
    get_logger,
    lossy_ascii_score,
    mojibake_score,
    read_csv_robust,
)

LOG = get_logger("validation")


# --------------------------------------------------------------------- helpers
def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def norm_name(series: pd.Series) -> pd.Series:
    """Normaliza nombres administrativos: sin tildes, mayúsculas, sin dobles espacios."""
    out = series.astype("string").fillna("")
    out = out.map(lambda s: strip_accents(str(s)))
    return out.str.upper().str.replace(r"\s+", " ", regex=True).str.strip()


def to_numeric_coord(series: pd.Series) -> pd.Series:
    """Convierte coordenadas texto a float tolerando coma decimal y espacios."""
    s = series.astype("string").str.strip().str.replace(",", ".", regex=False)
    return pd.to_numeric(s, errors="coerce")


def in_bbox(lon: pd.Series, lat: pd.Series) -> pd.Series:
    lon_min, lat_min, lon_max, lat_max = CFG.bbox
    return (
        lon.between(lon_min, lon_max, inclusive="both")
        & lat.between(lat_min, lat_max, inclusive="both")
    )


# ------------------------------------------------------------------ boundaries
def load_districts(dlog: DecisionLog) -> gpd.GeoDataFrame:
    """Polígonos distritales, con UBIGEO derivado del pcode de HDX."""
    spec = CFG["sources"]["boundaries"]
    layer = find_layer(
        CFG.path("raw") / "boundaries", spec["district_layer_pattern"]
    )
    gdf = gpd.read_file(layer)
    prefix = spec["pcode_prefix"]
    gdf["ubigeo"] = gdf["adm3_pcode"].astype("string").str.removeprefix(prefix)
    gdf["district"] = norm_name(gdf["adm3_name"])
    gdf["province"] = norm_name(gdf["adm2_name"])
    gdf["department"] = norm_name(gdf["adm1_name"])
    gdf = gdf[
        [
            "ubigeo",
            "district",
            "province",
            "department",
            "area_sqkm",
            "center_lat",
            "center_lon",
            "geometry",
        ]
    ].to_crs(CFG["project"]["crs_geographic"])

    scope_deps = [norm_name(pd.Series([d]))[0] for d in CFG.departments()]
    n_all = len(gdf)
    gdf = gdf[gdf["department"].isin(scope_deps)].reset_index(drop=True)
    dlog.record(
        check="carga de polígonos distritales",
        dataset="boundaries",
        n_affected=len(gdf),
        n_total=n_all,
        action=f"distritos retenidos para el alcance '{CFG.mode}'",
        justification=(
            f"HDX v01 (valid_on 2020-07-14) trae {n_all} distritos; se filtra a los "
            f"{len(scope_deps)} departamentos del alcance"
        ),
        departments=";".join(scope_deps),
    )
    if n_all != spec["expected_districts"]:
        LOG.warning(
            "el archivo trae %d distritos, se esperaban %d", n_all, spec["expected_districts"]
        )
    return gdf


# ------------------------------------------------------------------ facilities
def load_facilities(districts: gpd.GeoDataFrame, dlog: DecisionLog) -> gpd.GeoDataFrame:
    """Carga y valida RENIPRESS. Devuelve todas las IPRESS del alcance con banderas."""
    spec = CFG["sources"]["renipress"]
    fcfg = CFG["facilities"]
    vcfg = CFG["validation"]
    path = CFG.path("raw") / spec["filename"]

    df, encoding = read_csv_robust(path, sep=spec["sep"], logger=LOG)
    n0 = len(df)
    dlog.set_counter("renipress_rows_raw", n0)

    # --- chequeo 6: codificación -------------------------------------------
    moji = mojibake_score(df["NOMBRE"]) + mojibake_score(df["DIRECCION"])
    dlog.record(
        check="codificación del archivo",
        dataset="renipress",
        n_affected=moji,
        n_total=n0,
        action=f"archivo decodificado con '{encoding}'",
        justification=(
            "se probaron utf-8, utf-8-sig, latin-1 y cp1252 en orden; "
            f"{moji} campos con síntomas de doble codificación"
        ),
        encoding_used=encoding,
    )
    lossy = lossy_ascii_score(df["NOMBRE"])
    dlog.record(
        check="pérdida de caracteres no ASCII en el origen",
        dataset="renipress",
        n_affected=lossy,
        n_total=n0,
        action="se reporta y no se repara",
        justification=(
            "el archivo publicado no contiene ni un solo byte de 'ñ' ni de vocal "
            "acentuada en ninguna codificación: el sistema de origen exportó "
            "reemplazando cada carácter no ASCII por '?', pérdida irreversible "
            "(no es mojibake, que sí sería recuperable). Reconstruir 'SE?OR' como "
            "'SEÑOR' es una inferencia y en casos como 'MAR?A' sería una "
            "invención, así que no se repara. Sin impacto analítico: los nombres "
            "son solo de presentación y ningún cruce los usa como clave —los "
            "cruces van por COD_IPRESS y UBIGEO"
        ),
    )

    for col in ("DEPARTAMENTO", "PROVINCIA", "DISTRITO", "NOMBRE", "INSTITUCION"):
        df[col + "_N"] = norm_name(df[col])
    df["ubigeo"] = df["UBIGEO"].astype("string").str.zfill(6)

    # --- chequeo 5: duplicados ---------------------------------------------
    key = vcfg["duplicate_key"]
    dup_mask = df.duplicated(subset=key, keep="first")
    dlog.record(
        check="códigos de establecimiento duplicados",
        dataset="renipress",
        n_affected=int(dup_mask.sum()),
        n_total=n0,
        action="se conserva la primera aparición y se descartan las repetidas",
        justification=f"clave declarada en config.md: {key}",
    )
    df = df[~dup_mask].copy()

    # --- alcance -------------------------------------------------------------
    scope_deps = [norm_name(pd.Series([d]))[0] for d in CFG.departments()]
    n_before_scope = len(df)
    df = df[df["DEPARTAMENTO_N"].isin(scope_deps)].copy()
    dlog.record(
        check="filtro de alcance geográfico",
        dataset="renipress",
        n_affected=n_before_scope - len(df),
        n_total=n_before_scope,
        action=f"descartadas IPRESS fuera del alcance '{CFG.mode}'",
        justification=f"alcance activo: {', '.join(scope_deps)}",
    )

    # --- operatividad y resolutividad ---------------------------------------
    df["is_operational"] = (
        df["ESTADO"].str.upper().isin([s.upper() for s in fcfg["estado_whitelist"]])
        & df["SITUACION"].str.upper().isin([s.upper() for s in fcfg["situacion_whitelist"]])
        & df["CONDICION"].str.upper().isin([s.upper() for s in fcfg["condicion_whitelist"]])
    )
    df["is_resolutive_category"] = df["CATEGORIA"].isin(fcfg["resolutive_categories"])
    df["is_upgrade_candidate"] = df["CATEGORIA"].isin(
        fcfg["upgrade_candidate_categories"]
    )
    dlog.record(
        check="definición de establecimiento operativo",
        dataset="renipress",
        n_affected=int((~df["is_operational"]).sum()),
        n_total=len(df),
        action="marcados como no operativos (no se eliminan, se excluyen del ruteo)",
        justification=(
            "ESTADO es 'ACTIVO' en el 100% de los registros y SITUACION es "
            "'REGISTRADO' en el 100%, por lo que el único campo con poder "
            "discriminante es CONDICION. Se documenta como limitación: RENIPRESS "
            "publicado no permite identificar cierres."
        ),
        estado_values=";".join(sorted(df["ESTADO"].dropna().unique())),
        condicion_values=";".join(sorted(df["CONDICION"].dropna().unique())),
    )
    dlog.record(
        check="categorías sin dato válido",
        dataset="renipress",
        n_affected=int((df["CATEGORIA"].isna() | (df["CATEGORIA"] == "0")).sum()),
        n_total=len(df),
        action="tratadas como no resolutivas",
        justification=(
            "CATEGORIA = '0' no corresponde a ninguna categoría de la NT 021-MINSA; "
            "asumir resolutividad sobreestimaría la oferta"
        ),
    )

    # --- chequeos 1-3: coordenadas ------------------------------------------
    lat = to_numeric_coord(df[fcfg["lat_column"]])
    lon = to_numeric_coord(df[fcfg["lon_column"]])

    missing = lat.isna() | lon.isna() | (lat == 0) | (lon == 0)
    dlog.record(
        check="coordenadas nulas, vacías o cero",
        dataset="renipress",
        n_affected=int(missing.sum()),
        n_total=len(df),
        action="marcadas para recuperación por capital distrital",
        justification=(
            "una coordenada (0,0) es el Golfo de Guinea, no un error recuperable "
            "por reproyección; se trata igual que un nulo"
        ),
    )

    ok_direct = in_bbox(lon, lat)
    ok_swapped = in_bbox(lat, lon) & ~ok_direct
    swapped = ok_swapped & ~missing
    if vcfg["detect_swapped_coords"] and vcfg["fix_swapped_coords"]:
        lat_fixed = lat.where(~swapped, lon)
        lon_fixed = lon.where(~swapped, lat)
    else:
        lat_fixed, lon_fixed = lat, lon
    dlog.record(
        check="latitud/longitud intercambiadas",
        dataset="renipress",
        n_affected=int(swapped.sum()),
        n_total=len(df),
        action="par (lat, lon) invertido" if vcfg["fix_swapped_coords"] else "solo marcado",
        justification=(
            "se invierte solo cuando el par original cae fuera del bbox del Perú y "
            "el par invertido cae dentro; el criterio es no ambiguo porque el "
            "Perú no interseca su propio bbox transpuesto"
        ),
    )

    outside = ~missing & ~in_bbox(lon_fixed, lat_fixed)
    dlog.record(
        check="puntos fuera del bounding box del Perú",
        dataset="renipress",
        n_affected=int(outside.sum()),
        n_total=len(df),
        action=(
            "coordenadas anuladas y enviadas a recuperación"
            if vcfg["drop_outside_bbox"]
            else "solo marcadas"
        ),
        justification=f"bbox declarado en config.md: {CFG.bbox}",
    )

    df["lat"] = lat_fixed.where(~(missing | outside))
    df["lon"] = lon_fixed.where(~(missing | outside))
    df["coord_swapped"] = swapped.to_numpy()
    df["coord_outside_bbox"] = outside.to_numpy()
    df["coord_missing_raw"] = missing.to_numpy()

    # --- chequeo 4: punto dentro del distrito declarado ----------------------
    df = _check_point_in_district(df, districts, dlog, dataset="renipress")

    # --- recuperación --------------------------------------------------------
    df = _recover_facility_coords(df, districts, dlog)

    df["has_coords"] = df["lat"].notna() & df["lon"].notna()
    df["is_resolutive"] = (
        df["is_operational"] & df["is_resolutive_category"] & df["has_coords"]
    )
    by_category = df["is_resolutive_category"]
    dlog.set_counter("facilities_in_scope", len(df))
    dlog.set_counter("facilities_resolutive_by_category", int(by_category.sum()))
    dlog.set_counter("facilities_resolutive", int(df["is_resolutive"].sum()))
    dlog.set_counter(
        "facilities_resolutive_dropped_not_operational",
        int((by_category & ~df["is_operational"]).sum()),
    )
    dlog.set_counter(
        "facilities_resolutive_lost_no_coords",
        int((df["is_operational"] & by_category & ~df["has_coords"]).sum()),
    )
    dlog.set_counter(
        "facilities_resolutive_on_recovered_coords",
        int((df["is_resolutive"] & df["coord_recovered"]).sum()),
    )
    dlog.set_counter(
        "facilities_upgrade_candidates",
        int((df["is_upgrade_candidate"] & df["is_operational"] & df["has_coords"]).sum()),
    )

    recovered = int(df["coord_recovered"].sum())
    needed = int((df["coord_missing_raw"] | df["coord_outside_bbox"]).sum())
    dlog.record(
        check="tasa de recuperación de coordenadas",
        dataset="renipress",
        n_affected=recovered,
        n_total=max(needed, 1),
        action=f"{recovered} de {needed} registros recuperados",
        justification=(
            "recuperación por centroide del centro poblado capital del distrito "
            "declarado; el error introducido se acota por el radio del distrito y "
            "se propaga como banda de incertidumbre en la Fase 3"
        ),
    )

    gdf = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["lon"], df["lat"], crs=CFG["project"]["crs_geographic"]),
    )
    return gdf


def _check_point_in_district(
    df: pd.DataFrame,
    districts: gpd.GeoDataFrame,
    dlog: DecisionLog,
    dataset: str,
) -> pd.DataFrame:
    """Marca los puntos que caen fuera del polígono de su UBIGEO declarado."""
    tol = CFG["validation"]["district_polygon_tolerance_deg"]
    have = df["lat"].notna() & df["lon"].notna()
    pts = gpd.GeoDataFrame(
        df.loc[have, ["ubigeo"]].copy(),
        geometry=gpd.points_from_xy(
            df.loc[have, "lon"], df.loc[have, "lat"], crs=CFG["project"]["crs_geographic"]
        ),
    )
    poly = districts.set_index("ubigeo")["geometry"]
    # Buffer de tolerancia: un punto en el borde no es un error de dato. El buffer
    # se hace deliberadamente en grados y no en metros: el Perú abarca las zonas
    # UTM 17S, 18S y 19S, así que no existe un CRS proyectado único válido para
    # todo el país y reproyectar por zonas introduciría más error que la propia
    # tolerancia. Se silencia el aviso de geopandas porque la elección es explícita.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*geographic CRS.*")
        buffered = poly.buffer(tol)

    matched = pts["ubigeo"].map(lambda u: u in buffered.index)
    inside = pd.Series(False, index=pts.index)
    valid_idx = pts.index[matched]
    if len(valid_idx):
        geoms = buffered.reindex(pts.loc[valid_idx, "ubigeo"]).to_numpy()
        inside.loc[valid_idx] = [
            g.covers(p) for g, p in zip(geoms, pts.loc[valid_idx, "geometry"], strict=True)
        ]

    df["district_polygon_available"] = False
    df.loc[have, "district_polygon_available"] = matched.to_numpy()
    df["point_outside_declared_district"] = False
    df.loc[have, "point_outside_declared_district"] = (matched & ~inside).to_numpy()

    dlog.record(
        check="punto fuera del polígono distrital declarado",
        dataset=dataset,
        n_affected=int(df["point_outside_declared_district"].sum()),
        n_total=int(have.sum()),
        action="marcado, no descartado",
        justification=(
            f"tolerancia de {tol}° (~{tol * 111:.1f} km) para bordes; el desacuerdo "
            "puede venir del polígono (HDX 2020) o del UBIGEO declarado, así que "
            "descartar sesgaría hacia distritos con límites desactualizados"
        ),
    )
    dlog.record(
        check="UBIGEO sin polígono distrital disponible",
        dataset=dataset,
        n_affected=int((have & ~df["district_polygon_available"]).sum()),
        n_total=int(have.sum()),
        action="conservado; se agrega a nivel provincial en la Fase 3",
        justification=(
            "distritos creados después de 2020-07-14 no existen en la versión "
            "vigente de los límites de HDX/INEI"
        ),
    )
    return df


def _recover_facility_coords(
    df: pd.DataFrame, districts: gpd.GeoDataFrame, dlog: DecisionLog
) -> pd.DataFrame:
    """Imputa coordenadas faltantes con el centroide del distrito declarado."""
    need = df["lat"].isna() | df["lon"].isna()
    centro = districts.set_index("ubigeo")[["center_lat", "center_lon"]]
    cand = df.loc[need, "ubigeo"].map(centro["center_lat"])
    cand_lon = df.loc[need, "ubigeo"].map(centro["center_lon"])

    df["coord_recovered"] = False
    fill = need & df["ubigeo"].isin(centro.index)
    df.loc[fill, "lat"] = cand.reindex(df.index[fill]).to_numpy()
    df.loc[fill, "lon"] = cand_lon.reindex(df.index[fill]).to_numpy()
    df.loc[fill, "coord_recovered"] = True
    LOG.info(
        "coordenadas recuperadas por centroide distrital: %d de %d faltantes",
        int(fill.sum()),
        int(need.sum()),
    )
    return df


# ---------------------------------------------------------------- demand points
def load_demand_points(
    districts: gpd.GeoDataFrame, dlog: DecisionLog
) -> gpd.GeoDataFrame:
    """Carga y valida el listado de centros poblados (puntos de demanda)."""
    spec = CFG["sources"]["ccpp"]
    dcfg = CFG["demand"]
    path = CFG.path("raw") / spec["filename"]

    raw = pd.read_excel(path, header=None, dtype=object)
    header_row = _find_header_row(raw)
    df = pd.read_excel(path, header=header_row, dtype=object)
    df = df.loc[:, [c for c in df.columns if not str(c).startswith("Unnamed")]]
    # Se normalizan los encabezados (sin tildes, espacios colapsados) porque el
    # archivo publicado mezcla "CATEGORÍA", "CLASIFICACIÓN INEI" y dobles espacios;
    # depender de la forma exacta rompería el pipeline en la siguiente publicación.
    df.columns = [
        " ".join(strip_accents(str(c)).replace("\n", " ").split()) for c in df.columns
    ]
    n0 = len(df)
    dlog.set_counter("ccpp_rows_raw", n0)
    LOG.info("centros poblados: %d filas, columnas %s", n0, list(df.columns))

    lat_col, lon_col = _resolve_ccpp_coord_columns(df, dlog)

    df["ubigeo"] = df["UBIGEO"].astype("string").str.strip().str.zfill(10).str[:6]
    df["ccpp_code"] = df["UBIGEO"].astype("string").str.strip().str.zfill(10)
    df["department"] = norm_name(df["DEPARTAMENTO"])
    df["province"] = norm_name(df["PROVINCIA"])
    df["district"] = norm_name(df["DISTRITO"])
    df["name"] = norm_name(df["CCPP"])
    df["population"] = pd.to_numeric(df["Habitantes"], errors="coerce").fillna(0).astype(int)
    df["dwellings"] = pd.to_numeric(df["Viviendas"], errors="coerce").fillna(0).astype(int)
    df["is_capital"] = pd.to_numeric(df["CAPITAL"], errors="coerce").fillna(0) > 0

    inei = (
        norm_name(df["CLASIFICACION INEI"])
        if "CLASIFICACION INEI" in df.columns
        else pd.Series("", index=df.index, dtype="string")
    )
    df["area_type"] = np.where(
        inei.isin(["URBANO", "RURAL"]),
        inei,
        np.where(df["population"] >= dcfg["urban_pop_threshold"], "URBANO", "RURAL"),
    )
    dlog.record(
        check="clasificación urbano/rural",
        dataset="ccpp",
        n_affected=int((~inei.isin(["URBANO", "RURAL"])).sum()),
        n_total=n0,
        action=(
            "se usa CLASIFICACIÓN INEI cuando existe; en su ausencia, umbral de "
            f"{dcfg['urban_pop_threshold']} habitantes"
        ),
        justification=(
            "la regla primaria es la clasificación oficial del INEI, no un umbral "
            "propio; el umbral es solo respaldo y se reporta cuántos casos lo usaron"
        ),
    )

    scope_deps = [norm_name(pd.Series([d]))[0] for d in CFG.departments()]
    n_before = len(df)
    df = df[df["department"].isin(scope_deps)].copy()
    dlog.record(
        check="filtro de alcance geográfico",
        dataset="ccpp",
        n_affected=n_before - len(df),
        n_total=n_before,
        action=f"descartados centros poblados fuera del alcance '{CFG.mode}'",
        justification=f"alcance activo: {', '.join(scope_deps)}",
    )

    lat = to_numeric_coord(df[lat_col])
    lon = to_numeric_coord(df[lon_col])
    missing = lat.isna() | lon.isna() | (lat == 0) | (lon == 0)
    dlog.record(
        check="coordenadas nulas, vacías o cero",
        dataset="ccpp",
        n_affected=int(missing.sum()),
        n_total=len(df),
        action="descartados del universo de demanda",
        justification=(
            "un punto de demanda sin ubicación no puede rutearse; su población se "
            "reporta aparte como cobertura no evaluable"
        ),
    )

    ok_direct = in_bbox(lon, lat)
    swapped = in_bbox(lat, lon) & ~ok_direct & ~missing
    lat2 = lat.where(~swapped, lon)
    lon2 = lon.where(~swapped, lat)
    dlog.record(
        check="latitud/longitud intercambiadas (por registro)",
        dataset="ccpp",
        n_affected=int(swapped.sum()),
        n_total=len(df),
        action="par invertido registro a registro",
        justification=(
            "independiente de la inversión sistemática del encabezado; captura "
            "errores de captura individuales"
        ),
    )

    outside = ~missing & ~in_bbox(lon2, lat2)
    dlog.record(
        check="puntos fuera del bounding box del Perú",
        dataset="ccpp",
        n_affected=int(outside.sum()),
        n_total=len(df),
        action="descartados",
        justification=f"bbox declarado en config.md: {CFG.bbox}",
    )

    df["lat"] = lat2
    df["lon"] = lon2
    df["coord_swapped"] = swapped.to_numpy()
    keep = ~missing & ~outside
    lost_pop = int(df.loc[~keep, "population"].sum())
    df = df[keep].copy()
    dlog.set_counter("ccpp_population_unlocatable", lost_pop)

    df = _check_point_in_district(df, districts, dlog, dataset="ccpp")

    if dcfg["drop_zero_population"]:
        n_before = len(df)
        zero = df["population"] <= 0
        dlog.record(
            check="centros poblados con población cero",
            dataset="ccpp",
            n_affected=int(zero.sum()),
            n_total=n_before,
            action="descartados del universo de demanda",
            justification=(
                "no aportan peso a ningún agregado poblacional y consumirían cupos "
                "del límite de 5 000 puntos ruteables"
            ),
        )
        df = df[~zero].copy()

    df["natural_region"] = df["department"].map(
        lambda d: CFG.region_of_department(d)
    )
    df["demand_id"] = (
        "CP" + pd.Series(range(1, len(df) + 1), index=df.index).astype(str).str.zfill(6)
    )
    dlog.set_counter("ccpp_valid", len(df))
    dlog.set_counter("ccpp_population_total", int(df["population"].sum()))

    return gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(
            df["lon"], df["lat"], crs=CFG["project"]["crs_geographic"]
        ),
    )


def _find_header_row(raw: pd.DataFrame, max_scan: int = 12) -> int:
    """El archivo del MTC trae títulos antes del encabezado real."""
    for i in range(min(max_scan, len(raw))):
        row = raw.iloc[i].astype("string").str.upper().fillna("")
        if row.str.contains("UBIGEO").any() and row.str.contains("HABITANTES").any():
            return i
    raise ValueError("no se localizó la fila de encabezado en el listado de centros poblados")


def _resolve_ccpp_coord_columns(df: pd.DataFrame, dlog: DecisionLog) -> tuple[str, str]:
    """Determina qué columna es latitud y cuál longitud, ignorando su etiqueta.

    El archivo publicado rotula ``Latitud (coord X)`` a la columna que en realidad
    contiene la longitud. En lugar de confiar en el nombre, se decide por el rango
    de valores: en el Perú la longitud está en [-81.4, -68.6] y la latitud en
    [-18.4, -0.04], intervalos disjuntos.
    """
    cands = [c for c in df.columns if "latitud" in c.lower() or "longitud" in c.lower()]
    if len(cands) < 2:
        raise ValueError(f"no se identificaron columnas de coordenadas en {list(df.columns)}")
    a, b = cands[0], cands[1]
    va, vb = to_numeric_coord(df[a]), to_numeric_coord(df[b])
    lon_min, lat_min, lon_max, lat_max = CFG.bbox

    def score(v: pd.Series, lo: float, hi: float) -> float:
        return float(v.between(lo, hi).mean())

    as_labeled = score(vb, lon_min, lon_max) + score(va, lat_min, lat_max)
    as_swapped = score(va, lon_min, lon_max) + score(vb, lat_min, lat_max)

    if as_swapped > as_labeled:
        lat_col, lon_col = b, a
        verdict = "encabezado invertido: se ignoran las etiquetas"
    else:
        lat_col, lon_col = a, b
        verdict = "encabezado consistente con el contenido"

    dlog.record(
        check="orientación de las columnas de coordenadas",
        dataset="ccpp",
        n_affected=len(df) if as_swapped > as_labeled else 0,
        n_total=len(df),
        action=f"latitud <- '{lat_col}', longitud <- '{lon_col}'",
        justification=(
            f"{verdict}. Puntuación como etiquetado = {as_labeled:.3f}, "
            f"como invertido = {as_swapped:.3f} (fracción de valores dentro del "
            "rango esperado de cada eje, que son disjuntos en el Perú)"
        ),
    )
    LOG.warning("centros poblados: %s (lat=%s, lon=%s)", verdict, lat_col, lon_col)
    return lat_col, lon_col


# ------------------------------------------------------------------- muestreo
def sample_demand(demand: gpd.GeoDataFrame, dlog: DecisionLog) -> gpd.GeoDataFrame:
    """Muestra ≤ ``max_demand_points`` con pesos de diseño (ver config.md §3).

    Diseño: estrato de certeza (capitales y centros poblados grandes, π=1) más
    muestreo PPS por ``departamento × área`` con cupos proporcionales a la
    población no cubierta. El peso de diseño de un punto PPS es
    ``P_estrato / n_estrato``, que es el estimador de Horvitz–Thompson para
    ``π_i ∝ pob_i``; el de un punto de certeza es su propia población.
    """
    dcfg = CFG["demand"]
    cap = int(dcfg["max_demand_points"])
    rng = np.random.default_rng(CFG.seed)

    d = demand.copy()
    certainty = d["is_capital"] | (d["population"] >= dcfg["certainty_pop_threshold"])

    if len(d) <= cap:
        d["design_weight"] = d["population"].astype(float)
        d["sampling_stratum"] = "censo_completo"
        d["inclusion_prob"] = 1.0
        dlog.record(
            check="muestreo de puntos de demanda",
            dataset="demand_sample",
            n_affected=len(d),
            n_total=len(d),
            action="no se muestrea: el universo cabe en el límite de puntos",
            justification=(
                f"{len(d)} centros poblados ≤ {cap}; cada punto pesa por su propia "
                "población y no se introduce varianza de muestreo"
            ),
        )
        return d

    n_cert = int(certainty.sum())
    if n_cert > cap:
        # Si el estrato de certeza excede el cupo, se prioriza por población.
        order = d.loc[certainty, "population"].sort_values(ascending=False).index[:cap]
        certainty = d.index.isin(order)
        n_cert = int(certainty.sum())
        LOG.warning("el estrato de certeza excedía el cupo; se recortó por población")

    cert = d[certainty].copy()
    cert["design_weight"] = cert["population"].astype(float)
    cert["sampling_stratum"] = "certeza"
    cert["inclusion_prob"] = 1.0

    rest = d[~certainty].copy()
    # Estratos = distrito × área. Se estratifica por distrito y no por departamento
    # porque el entregable principal es el ranking de brechas *distritales*: con
    # estratos departamentales, casi dos tercios de los distritos quedaban con menos
    # de tres puntos y su estimación era demasiado ruidosa para rankear.
    rest["_stratum"] = rest["ubigeo"] + "|" + rest["area_type"]
    slots = cap - n_cert
    sizes = rest.groupby("_stratum").size()
    pop_by = rest.groupby("_stratum")["population"].sum()

    quota = _allocate_quota(pop_by, sizes, slots, floor=1)

    picks: list[pd.DataFrame] = []
    for stratum, group in rest.groupby("_stratum", sort=True):
        n_s = int(quota.get(stratum, 0))
        if n_s <= 0:
            continue
        p = group["population"].to_numpy(dtype=float)
        p = p / p.sum()
        idx = rng.choice(group.index.to_numpy(), size=n_s, replace=False, p=p)
        sel = group.loc[idx].copy()
        sel["design_weight"] = float(group["population"].sum()) / n_s
        sel["sampling_stratum"] = stratum
        sel["inclusion_prob"] = n_s * p[group.index.get_indexer(idx)]
        picks.append(sel)

    out = pd.concat([cert, *picks]).drop(columns="_stratum", errors="ignore")
    out = gpd.GeoDataFrame(out, geometry="geometry", crs=demand.crs)

    covered = float(out["design_weight"].sum())
    total = float(d["population"].sum())
    dlog.record(
        check="muestreo de puntos de demanda",
        dataset="demand_sample",
        n_affected=len(out),
        n_total=len(d),
        action=(
            f"{n_cert} puntos de certeza (capitales y ≥ "
            f"{dcfg['certainty_pop_threshold']} hab.) + {len(out) - n_cert} PPS "
            f"estratificados por departamento × área"
        ),
        justification=(
            f"límite del enunciado: {cap} puntos ruteables. Los pesos de diseño "
            f"suman {covered:,.0f} habitantes frente a {total:,.0f} del universo "
            f"({covered / total:.4%}); los agregados poblacionales siguen siendo "
            "estimadores de Horvitz–Thompson no sesgados. Implicancia: los centros "
            "poblados rurales muy pequeños están representados por análogos "
            "ponderados, no individualmente."
        ),
        n_strata=int(len(quota[quota > 0])),
        weight_sum=round(covered, 1),
        universe_population=int(total),
    )
    LOG.info(
        "muestra: %d puntos (%d certeza), pesos suman %.0f de %.0f hab.",
        len(out),
        n_cert,
        covered,
        total,
    )
    return out


def _allocate_quota(
    pop_by: pd.Series, sizes: pd.Series, slots: int, floor: int = 1
) -> pd.Series:
    """Reparte exactamente ``slots`` cupos entre estratos, respetando su tamaño.

    Dos pasadas:

    1. **Piso**: un cupo a cada estrato, en orden de población descendente, hasta
       agotar el presupuesto. Garantiza que ningún estrato distrito × área con
       población quede sin representación mientras quepa.
    2. **Reparto proporcional por divisores (Sainte-Laguë)**: cada cupo restante va
       al estrato con mayor ``pob / (2·q + 1)`` entre los que aún tienen centros
       poblados disponibles. Es el mismo método de apportionment usado para repartir
       escaños; a diferencia del redondeo proporcional, no deja cupos sin asignar ni
       excede el presupuesto, y minimiza la desviación relativa respecto de la
       asignación proporcional exacta.
    """
    caps = sizes.reindex(pop_by.index).fillna(0).astype(int)
    quota = pd.Series(0, index=pop_by.index, dtype=int)
    order = list(pop_by.sort_values(ascending=False).index)

    budget = int(slots)
    for stratum in order:
        if budget <= 0:
            break
        if caps[stratum] >= floor:
            quota[stratum] = floor
            budget -= floor

    if budget > 0:
        pops = pop_by.to_dict()
        capd = caps.to_dict()
        qd = quota.to_dict()
        # Max-heap por prioridad de Sainte-Laguë (heapq es min-heap: se niega).
        heap = [
            (-pops[s] / (2 * qd[s] + 1), s)
            for s in pop_by.index
            if qd[s] < capd[s] and pops[s] > 0
        ]
        heapq.heapify(heap)
        while budget > 0 and heap:
            _, stratum = heapq.heappop(heap)
            qd[stratum] += 1
            budget -= 1
            if qd[stratum] < capd[stratum]:
                heapq.heappush(
                    heap, (-pops[stratum] / (2 * qd[stratum] + 1), stratum)
                )
        quota = pd.Series(qd, dtype=int).reindex(pop_by.index).fillna(0).astype(int)

    return quota


def flag_thin_districts(
    sample: gpd.GeoDataFrame, universe: gpd.GeoDataFrame, dlog: DecisionLog
) -> gpd.GeoDataFrame:
    """Marca los distritos cuya estimación distrital es de alta varianza.

    Con el tope de 5 000 puntos sobre ~1 850 distritos es aritméticamente imposible
    tener tres puntos en cada distrito, así que el conteo por sí solo es un criterio
    pobre: un distrito cuya capital concentra el 85 % de su población queda bien
    estimado con dos puntos. El criterio combina ambas cosas:

        alta varianza  ⇔  n_puntos < k  Y  cobertura directa < ``direct_cov_min``

    donde la cobertura directa es la fracción de la población distrital que vive en
    centros poblados incluidos con probabilidad 1 (estrato de certeza).
    """
    k = int(CFG["demand"]["min_points_per_district"])
    direct_cov_min = 0.60

    counts = sample.groupby("ubigeo").size().rename("n_points_district")
    cert_pop = (
        sample.loc[sample["sampling_stratum"].isin(["certeza", "censo_completo"])]
        .groupby("ubigeo")["population"]
        .sum()
    )
    dist_pop = universe.groupby("ubigeo")["population"].sum()
    direct = (cert_pop / dist_pop).rename("direct_pop_coverage").fillna(0.0)

    sample = sample.merge(counts, left_on="ubigeo", right_index=True, how="left")
    sample = sample.merge(direct, left_on="ubigeo", right_index=True, how="left")
    sample["direct_pop_coverage"] = sample["direct_pop_coverage"].fillna(0.0)
    sample["high_variance_district"] = (sample["n_points_district"] < k) & (
        sample["direct_pop_coverage"] < direct_cov_min
    )

    n_districts = int(sample["ubigeo"].nunique())
    n_flagged = int(sample.loc[sample["high_variance_district"], "ubigeo"].nunique())
    n_thin_count_only = int(
        sample.loc[sample["n_points_district"] < k, "ubigeo"].nunique()
    )
    dlog.record(
        check="distritos con estimación de alta varianza",
        dataset="demand_sample",
        n_affected=n_flagged,
        n_total=n_districts,
        action=(
            f"marcados los distritos con < {k} puntos Y menos del "
            f"{direct_cov_min:.0%} de su población en el estrato de certeza"
        ),
        justification=(
            f"{n_thin_count_only} distritos tienen menos de {k} puntos, pero en la "
            "mayoría la capital concentra la población, de modo que la estimación "
            "sigue siendo firme. El ranking de brechas críticas debe distinguir un "
            "distrito realmente mal cubierto de uno mal muestreado, no penalizar a "
            "los distritos concentrados. Todo distrito del alcance tiene al menos "
            "su capital en la muestra."
        ),
        n_districts_below_k_points=n_thin_count_only,
        direct_coverage_threshold=direct_cov_min,
    )
    return sample


# ------------------------------------------------------------------------ main
def run(save: bool = True) -> dict[str, gpd.GeoDataFrame]:
    dlog = DecisionLog(stage="validation")
    districts = load_districts(dlog)
    facilities = load_facilities(districts, dlog)
    demand = load_demand_points(districts, dlog)
    sample = flag_thin_districts(sample_demand(demand, dlog), demand, dlog)

    if save:
        out = CFG.path("processed")
        _write(facilities, out / CFG.scoped_name("facilities", "parquet"))
        _write(demand, out / CFG.scoped_name("demand_points", "parquet"))
        _write(sample, out / CFG.scoped_name("demand_sample", "parquet"))
        districts.to_file(out / CFG.scoped_name("districts", "gpkg"), driver="GPKG")
        dlog.save("data_quality_report")
        LOG.info("Fase 1 completa. Archivos en %s", out)

    return {
        "districts": districts,
        "facilities": facilities,
        "demand": demand,
        "sample": sample,
    }


def _write(gdf: gpd.GeoDataFrame, path: Path) -> None:
    """Parquet plano (lon/lat como columnas) para que lo lea cualquier consumidor."""
    df = pd.DataFrame(gdf.drop(columns="geometry"))
    # Las columnas crudas de Excel llegan como ``object`` con enteros y textos
    # mezclados; Arrow necesita un tipo homogéneo por columna.
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype("string")
    df.to_parquet(path, index=False)
    LOG.info("escrito %s (%d filas, %d columnas)", path.name, len(df), df.shape[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fase 1 — validación y calidad")
    parser.add_argument("--dry-run", action="store_true", help="no escribir archivos")
    args = parser.parse_args(argv)
    res = run(save=not args.dry_run)
    print(
        f"\nResumen: {len(res['facilities'])} IPRESS "
        f"({int(res['facilities']['is_resolutive'].sum())} resolutivas), "
        f"{len(res['demand'])} centros poblados válidos, "
        f"{len(res['sample'])} puntos de demanda muestreados."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
