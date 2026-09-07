"""Fase 3 — Métricas de accesibilidad, todas ponderadas por población.

Métricas exigidas por el enunciado:

1. ``t_min(i)``: tiempo al establecimiento resolutivo más cercano, en minutos, en auto.
2. Bandas de cobertura: % de población dentro de 30 / 60 / 120 minutos y más allá.
3. Media de acceso ponderada por población (distrito, provincia, departamento,
   región natural, nacional).
4. Lista de brechas críticas: peores distritos, rankeados.
5. Desigualdad: Gini y curva de Lorenz sobre el tiempo de acceso.
6. Contraste urbano/rural con la regla declarada en ``config.md``.

Y el análisis cruzado con altitud, ruralidad, tamaño, densidad, dispersión del
poblamiento y oferta de primer nivel, con una lectura explícita de si la relación
es causal, correlacional o ninguna de las dos.

**Ponderación.** Cada punto de la muestra trae un ``design_weight`` (Fase 1) que
es la población que representa. Todo agregado usa ese peso; nunca el promedio
simple sobre puntos, que sobrerrepresentaría los centros poblados pequeños.

Salidas en ``data/outputs/``: ``metrics_<nivel>_<alcance>.csv``,
``coverage_bands_<alcance>.csv``, ``worst_districts_<alcance>.csv``,
``inequality_<alcance>.csv``, ``lorenz_<alcance>.csv``,
``cross_analysis_<alcance>.csv``, ``urban_rural_<alcance>.csv``.
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from .acquisition import fetch_elevation
from .config import CFG
from .utils import DecisionLog, get_logger, safe_row_idxmin

LOG = get_logger("metrics")

LEVEL_KEYS: dict[str, list[str]] = {
    "district": ["department", "province", "district", "ubigeo"],
    "province": ["department", "province"],
    "department": ["department"],
    "natural_region": ["natural_region"],
    "national": [],
}


# ------------------------------------------------------------------ utilidades
def weighted_mean(values: pd.Series, weights: pd.Series) -> float:
    ok = values.notna() & weights.notna() & (weights > 0)
    if not ok.any():
        return float("nan")
    return float(np.average(values[ok], weights=weights[ok]))


def weighted_quantile(values: pd.Series, weights: pd.Series, q: float) -> float:
    """Cuantil ponderado por población (interpolación sobre la CDF empírica)."""
    ok = values.notna() & weights.notna() & (weights > 0)
    if not ok.any():
        return float("nan")
    v = values[ok].to_numpy(dtype=float)
    w = weights[ok].to_numpy(dtype=float)
    order = np.argsort(v)
    v, w = v[order], w[order]
    cw = np.cumsum(w)
    cutoff = q * cw[-1]
    return float(np.interp(cutoff, cw, v))


def gini(values: pd.Series, weights: pd.Series) -> float:
    """Gini ponderado del tiempo de acceso.

    Se aplica al **tiempo de acceso**, no al ingreso: mide cuán desigualmente se
    reparte la carga de desplazamiento entre la población. 0 = todos tardan lo
    mismo; valores altos = una minoría soporta tiempos desproporcionados. Se
    prefiere Gini sobre Theil porque su lectura gráfica (curva de Lorenz) es
    directa para una audiencia no técnica, que es el público del dashboard.
    """
    ok = values.notna() & weights.notna() & (weights > 0) & (values >= 0)
    if ok.sum() < 2:
        return float("nan")
    v = values[ok].to_numpy(dtype=float)
    w = weights[ok].to_numpy(dtype=float)
    order = np.argsort(v)
    v, w = v[order], w[order]
    cum_w = np.cumsum(w)
    cum_vw = np.cumsum(v * w)
    if cum_vw[-1] <= 0:
        return float("nan")
    # Fórmula de Gini ponderado por unidades de tamaño desigual.
    num = np.sum(w * (cum_vw - v * w / 2.0))
    return float(1.0 - 2.0 * num / (cum_w[-1] * cum_vw[-1]))


def lorenz_curve(values: pd.Series, weights: pd.Series, n: int = 101) -> pd.DataFrame:
    ok = values.notna() & weights.notna() & (weights > 0)
    v = values[ok].to_numpy(dtype=float)
    w = weights[ok].to_numpy(dtype=float)
    order = np.argsort(v)
    v, w = v[order], w[order]
    pop = np.concatenate([[0.0], np.cumsum(w) / w.sum()])
    load = np.concatenate([[0.0], np.cumsum(v * w) / (v * w).sum()])
    grid = np.linspace(0, 1, n)
    return pd.DataFrame(
        {"share_poblacion": grid, "share_tiempo_acumulado": np.interp(grid, pop, load)}
    )


# ------------------------------------------------------------------ carga base
def load_access(profile: str | None = None) -> pd.DataFrame:
    """Une la muestra de demanda con su tiempo al resolutivo más cercano."""
    profile = profile or CFG["routing"]["primary_profile"]
    proc, out = CFG.path("processed"), CFG.path("outputs")

    demand = pd.read_parquet(proc / CFG.scoped_name("demand_sample", "parquet"))
    mpath = out / f"od_{profile}_matrix_{CFG.mode}.parquet"
    if not mpath.exists():
        raise FileNotFoundError(
            f"falta {mpath.name}. Ejecutar la Fase 2 "
            "(`python -m src.routing --stage primary`) o esperar al workflow de CI."
        )
    mat = pd.read_parquet(mpath).set_index("demand_id")

    t_min = mat.min(axis=1).rename("t_min")
    nearest = safe_row_idxmin(mat).rename("nearest_facility")
    df = demand.merge(
        pd.concat([t_min, nearest], axis=1), left_on="demand_id", right_index=True,
        how="left",
    )

    dpath = out / f"od_dist_{profile}_matrix_{CFG.mode}.parquet"
    if dpath.exists():
        dist = pd.read_parquet(dpath).set_index("demand_id").reindex(mat.index)
        nearest_aligned = nearest.reindex(mat.index)
        pos = mat.columns.get_indexer(nearest_aligned)
        # get_indexer devuelve -1 para las etiquetas que no encuentra, y eso
        # incluye los orígenes cuya fila es toda NaN (sin ruta a ningún
        # establecimiento), donde idxmin no puede devolver nada. Indexar con -1
        # tomaría silenciosamente la última columna, así que se anulan.
        valid = pos >= 0
        km_values = np.full(len(mat), np.nan)
        if valid.any():
            km_values[valid] = dist.to_numpy()[np.flatnonzero(valid), pos[valid]]
        df = df.merge(
            pd.Series(km_values, index=mat.index, name="km_red"),
            left_on="demand_id", right_index=True, how="left",
        )
    else:
        # El motor 'graph' no reporta distancia de red, solo tiempo.
        df["km_red"] = np.nan

    df["unreachable"] = df["t_min"].isna()
    return df


def enrich_context(access: pd.DataFrame, dlog: DecisionLog) -> pd.DataFrame:
    """Agrega altitud y refina la región natural de cada punto de demanda."""
    cache = CFG.path("processed") / CFG.scoped_name("elevation", "parquet")
    elev = fetch_elevation(
        access["lat"].astype(float).tolist(),
        access["lon"].astype(float).tolist(),
        cache,
    )
    keys = access.apply(lambda r: f"{float(r['lat']):.5f},{float(r['lon']):.5f}", axis=1)
    access = access.copy()
    access["altitude_m"] = keys.map(elev).astype(float)
    dlog.record(
        check="altitud de los puntos de demanda",
        dataset="metrics",
        n_affected=int(access["altitude_m"].notna().sum()),
        n_total=len(access),
        action="SRTM 30 m consultado vía OpenTopoData y versionado en data/processed",
        justification=(
            "la altitud es uno de los dos proxies estándar de pobreza y aislamiento "
            "en el Perú; se cachea para que ni CI ni una segunda corrida golpeen la API"
        ),
    )
    return refine_natural_region(access, dlog)


def refine_natural_region(access: pd.DataFrame, dlog: DecisionLog) -> pd.DataFrame:
    """Reasigna la región natural por altitud (etapa 2 de ``[natural_region]``).

    El proxy departamental que asigna la Fase 1 es grueso: nueve departamentos
    abarcan más de una región natural, así que las provincias altas de Lima
    contaban como costa y las tierras bajas de Cusco como sierra. Con la altitud
    SRTM de cada centro poblado la asignación es directa y no cuesta nada extra,
    porque el dato ya se descargó para el análisis cruzado.
    """
    nr = CFG["natural_region"]
    umbral = float(nr["sierra_min_elev_m"])
    selva_deps = set(nr["selva_departments"])

    access = access.copy()
    access["natural_region_proxy"] = access["natural_region"]
    elev = access["altitude_m"]

    refined = np.where(
        elev.isna(),
        access["natural_region_proxy"],  # sin altitud se conserva el proxy
        np.where(
            elev >= umbral,
            "sierra",
            np.where(access["department"].isin(selva_deps), "selva", "costa"),
        ),
    )
    access["natural_region"] = refined

    changed = access["natural_region"] != access["natural_region_proxy"]
    pop_changed = float(access.loc[changed, "design_weight"].sum())
    dlog.record(
        check="refinamiento de la región natural por altitud",
        dataset="metrics",
        n_affected=int(changed.sum()),
        n_total=len(access),
        action=(
            f"{int(changed.sum())} puntos reclasificados "
            f"({pop_changed:,.0f} habitantes representados)"
        ),
        justification=(
            f"umbral de {umbral:.0f} m s. n. m. sobre la altitud SRTM del centro "
            "poblado. El proxy departamental asignaba una sola región a cada "
            "departamento, y nueve de los 25 abarcan más de una: sin este paso las "
            "provincias altas de Lima contaban como costa y las tierras bajas de "
            "Cusco como sierra. Se conserva natural_region_proxy para auditar el "
            "cambio"
        ),
        distribucion=";".join(
            f"{k}={v}" for k, v in access["natural_region"].value_counts().items()
        ),
    )
    LOG.info(
        "región natural refinada: %d puntos reclasificados; distribución %s",
        int(changed.sum()),
        access["natural_region"].value_counts().to_dict(),
    )
    return access


# -------------------------------------------------------------------- agregados
def aggregate(access: pd.DataFrame, level: str) -> pd.DataFrame:
    """Agregados ponderados por población en un nivel administrativo."""
    bands = list(CFG["metrics"]["time_bands_min"])
    keys = LEVEL_KEYS[level]
    grouped = [("PERÚ", access)] if not keys else list(access.groupby(keys, sort=True))

    rows = []
    for name, g in grouped:
        w = g["design_weight"]
        pop = float(w.sum())
        reachable = g["t_min"].notna()
        row: dict[str, object] = {}
        if keys:
            values = name if isinstance(name, tuple) else (name,)
            row.update(dict(zip(keys, values, strict=True)))
        else:
            row["ambito"] = "PERÚ"
        row.update(
            {
                "poblacion": pop,
                "n_puntos": len(g),
                "poblacion_sin_ruta": float(w[~reachable].sum()),
                "t_medio_min": weighted_mean(g["t_min"], w),
                "t_mediano_min": weighted_quantile(g["t_min"], w, 0.5),
                "t_p90_min": weighted_quantile(g["t_min"], w, 0.90),
                "km_medio_red": weighted_mean(g["km_red"], w),
                "gini_tiempo": gini(g["t_min"], w),
            }
        )
        prev = 0.0
        for band in bands:
            share = float(w[reachable & (g["t_min"] <= band)].sum()) / pop if pop else np.nan
            row[f"pob_hasta_{band}min"] = share * pop if pop else np.nan
            row[f"share_hasta_{band}min"] = share
            row[f"share_banda_{prev:.0f}_{band}min"] = share - (
                row.get(f"share_hasta_{prev:.0f}min", 0.0) if prev else 0.0
            )
            prev = band
        last = bands[-1]
        row[f"share_mas_{last}min"] = 1.0 - row[f"share_hasta_{last}min"]
        row[f"pob_mas_{last}min"] = pop * row[f"share_mas_{last}min"]
        rows.append(row)

    out = pd.DataFrame(rows)
    if keys:
        out = out.sort_values(keys).reset_index(drop=True)
    return out


def urban_rural_contrast(access: pd.DataFrame, dlog: DecisionLog) -> pd.DataFrame:
    """Contraste urbano/rural, y por región natural dentro de cada uno."""
    bands = list(CFG["metrics"]["time_bands_min"])
    rows = []
    for (area, region), g in access.groupby(["area_type", "natural_region"], sort=True):
        w = g["design_weight"]
        pop = float(w.sum())
        row = {
            "area_type": area,
            "natural_region": region,
            "poblacion": pop,
            "n_puntos": len(g),
            "t_medio_min": weighted_mean(g["t_min"], w),
            "t_mediano_min": weighted_quantile(g["t_min"], w, 0.5),
            "t_p90_min": weighted_quantile(g["t_min"], w, 0.90),
            "gini_tiempo": gini(g["t_min"], w),
            "altitud_mediana_m": weighted_quantile(g["altitude_m"], w, 0.5),
        }
        for band in bands:
            row[f"share_hasta_{band}min"] = (
                float(w[g["t_min"] <= band].sum()) / pop if pop else np.nan
            )
        rows.append(row)
    rep = pd.DataFrame(rows)

    urb = access[access["area_type"] == "URBANO"]
    rur = access[access["area_type"] == "RURAL"]
    t_u = weighted_mean(urb["t_min"], urb["design_weight"])
    t_r = weighted_mean(rur["t_min"], rur["design_weight"])
    dlog.record(
        check="contraste urbano/rural",
        dataset="urban_rural",
        n_affected=len(rur),
        n_total=len(access),
        action=f"media rural {t_r:.1f} min vs. urbana {t_u:.1f} min",
        justification=(
            "la clasificación es la del INEI que trae el propio listado de centros "
            f"poblados (regla '{CFG['metrics']['urban_rule']}'), no un umbral "
            "inventado; el umbral de población solo actúa como respaldo para los "
            "registros sin ese campo, y su uso está contado en el reporte de calidad"
        ),
        ratio_rural_urbano=round(t_r / t_u, 3) if t_u else None,
    )
    return rep


def worst_districts(access: pd.DataFrame, district: pd.DataFrame) -> pd.DataFrame:
    """Lista de brechas críticas: peores distritos por población desatendida.

    El ranking primario **no** es el tiempo medio sino la población fuera del
    umbral de 60 minutos: un distrito de 300 habitantes a 5 horas es un problema
    distinto de uno de 80 000 habitantes a 90 minutos, y la decisión de política
    (dónde invertir) se ordena por el segundo. Se reportan ambos criterios.
    """
    n = int(CFG["metrics"]["worst_districts_n"])
    band = CFG["metrics"]["time_bands_min"][1]
    col_pop_out = f"pob_mas_{CFG['metrics']['time_bands_min'][-1]}min"

    flags = (
        access.groupby("ubigeo")
        .agg(
            high_variance=("high_variance_district", "max"),
            n_puntos_muestra=("demand_id", "count"),
            altitud_mediana_m=("altitude_m", "median"),
        )
        .reset_index()
    )
    df = district.merge(flags, on="ubigeo", how="left")
    df["pob_fuera_umbral"] = df["poblacion"] * (1.0 - df[f"share_hasta_{band}min"])
    df["rank_por_poblacion"] = df["pob_fuera_umbral"].rank(ascending=False, method="min")
    df["rank_por_tiempo"] = df["t_medio_min"].rank(ascending=False, method="min")

    cols = [
        "ubigeo", "department", "province", "district", "poblacion", "n_puntos_muestra",
        "t_medio_min", "t_mediano_min", "t_p90_min", f"share_hasta_{band}min",
        "pob_fuera_umbral", col_pop_out, "altitud_mediana_m", "gini_tiempo",
        "high_variance", "rank_por_poblacion", "rank_por_tiempo",
    ]
    cols = [c for c in cols if c in df.columns]
    return df.sort_values("pob_fuera_umbral", ascending=False)[cols].head(n).reset_index(
        drop=True
    )


# --------------------------------------------------------------- análisis cruzado
def build_district_context(
    access: pd.DataFrame, dlog: DecisionLog
) -> pd.DataFrame:
    """Construye las variables distritales del análisis cruzado."""
    import geopandas as gpd

    proc = CFG.path("processed")
    universe = pd.read_parquet(proc / CFG.scoped_name("demand_points", "parquet"))
    facilities = pd.read_parquet(proc / CFG.scoped_name("facilities", "parquet"))
    districts = gpd.read_file(proc / CFG.scoped_name("districts", "gpkg"))

    pop = universe.groupby("ubigeo")["population"].sum().rename("population")
    rural = (
        universe[universe["area_type"] == "RURAL"]
        .groupby("ubigeo")["population"].sum()
        .rename("rural_pop")
    )
    capital = (
        universe[universe["is_capital"]]
        .groupby("ubigeo")["population"].sum()
        .rename("capital_pop")
    )
    ccpp = universe.groupby("ubigeo").size().rename("ccpp_count")

    first_level = facilities[
        facilities["is_operational"].fillna(False)
        & facilities["CATEGORIA"].isin(["I-1", "I-2", "I-3", "I-4"])
    ]
    prim = first_level.groupby("ubigeo").size().rename("primary_care_n")

    ctx = pd.concat([pop, rural, capital, ccpp, prim], axis=1).fillna(
        {"rural_pop": 0, "capital_pop": 0, "primary_care_n": 0}
    )
    ctx = ctx.join(
        districts.set_index("ubigeo")[["area_sqkm", "department", "province", "district"]],
        how="left",
    )
    ctx["rural_share"] = ctx["rural_pop"] / ctx["population"].replace(0, np.nan)
    ctx["settlement_dispersion"] = 1.0 - ctx["capital_pop"] / ctx["population"].replace(0, np.nan)
    ctx["population_density"] = ctx["population"] / ctx["area_sqkm"].replace(0, np.nan)
    ctx["primary_care_per_10k"] = (
        ctx["primary_care_n"] / ctx["population"].replace(0, np.nan) * 10_000
    )
    ctx["altitude_m"] = access.groupby("ubigeo")["altitude_m"].median()

    dlog.record(
        check="variables del análisis cruzado",
        dataset="cross_analysis",
        n_affected=int(ctx.notna().all(axis=1).sum()),
        n_total=len(ctx),
        action=f"construidas {len(CFG['metrics']['cross_variables'])} variables distritales",
        justification=(
            "no se incluye pobreza monetaria ni población menor de 5 años a nivel "
            "distrital: el portal del INEI no respondió en la fecha de acceso y la "
            "versión vigente de las estadísticas de población de HDX solo llega a "
            "nivel departamental (verificado). Se usan en su lugar ruralidad y "
            "altitud, los dos proxies estándar en la literatura peruana, y la "
            "ausencia se declara en Limitaciones"
        ),
    )
    return ctx.reset_index()


def cross_analysis(
    district_metrics: pd.DataFrame, ctx: pd.DataFrame, dlog: DecisionLog
) -> pd.DataFrame:
    """Correlaciones de Spearman entre acceso y contexto, ponderadas por población.

    Se usa Spearman y no Pearson porque ninguna de estas variables es lineal ni
    simétrica: la densidad y la altitud tienen colas largas, y el tiempo de acceso
    está acotado por abajo en cero. Se reporta también la diferencia entre el
    quintil superior e inferior de cada variable, que es lo que una dirección
    regional puede leer sin saber qué es un coeficiente de correlación.
    """
    thr = float(CFG["metrics"]["correlation_report_threshold"])
    variables = list(CFG["metrics"]["cross_variables"])
    df = district_metrics.merge(
        ctx.drop(columns=["department", "province", "district"], errors="ignore"),
        on="ubigeo", how="left", suffixes=("", "_ctx"),
    )

    min_n = int(CFG.get("metrics", "cross_min_districts", default=30))
    rows = []
    for var in variables:
        if var not in df.columns:
            LOG.warning("variable de contexto ausente: %s", var)
            continue
        pair = df[[var, "t_medio_min", "poblacion"]].dropna()
        if len(pair) < min_n:
            LOG.info(
                "variable '%s' omitida: %d distritos con dato, mínimo %d",
                var, len(pair), min_n,
            )
            continue
        rho = float(pair[var].corr(pair["t_medio_min"], method="spearman"))
        q = pair[var].quantile([0.2, 0.8])
        low = pair[pair[var] <= q.iloc[0]]
        high = pair[pair[var] >= q.iloc[1]]
        rows.append(
            {
                "variable": var,
                "n_distritos": len(pair),
                "spearman_rho": rho,
                "t_medio_quintil_inferior": weighted_mean(
                    low["t_medio_min"], low["poblacion"]
                ),
                "t_medio_quintil_superior": weighted_mean(
                    high["t_medio_min"], high["poblacion"]
                ),
                "reportable": abs(rho) >= thr,
                "interpretacion": _interpret(var, rho),
            }
        )
    if not rows:
        # Ocurre en alcances pequeños (modo dev): con una decena de distritos una
        # correlación no significa nada, así que se omite en lugar de reportarse.
        LOG.warning(
            "análisis cruzado omitido: ninguna variable alcanza %d distritos", min_n
        )
        dlog.record(
            check="análisis cruzado acceso × contexto",
            dataset="cross_analysis",
            n_affected=0,
            n_total=len(df),
            action="omitido",
            justification=(
                f"ninguna variable de contexto tiene datos en al menos {min_n} "
                "distritos; una correlación sobre menos casos no sería informativa"
            ),
        )
        return pd.DataFrame(
            columns=[
                "variable", "n_distritos", "spearman_rho",
                "t_medio_quintil_inferior", "t_medio_quintil_superior",
                "reportable", "interpretacion",
            ]
        )

    rep = pd.DataFrame(rows).sort_values("spearman_rho", key=abs, ascending=False)
    dlog.record(
        check="análisis cruzado acceso × contexto",
        dataset="cross_analysis",
        n_affected=int(rep["reportable"].sum()),
        n_total=len(rep),
        action=f"asociaciones con |rho| ≥ {thr} marcadas como reportables",
        justification=(
            "Spearman sobre distritos, no sobre puntos de demanda, para no darle "
            "más peso a los distritos con más centros poblados muestreados. Ninguna "
            "de estas asociaciones identifica un efecto causal: el diseño es "
            "transversal y sin variación exógena"
        ),
    )
    return rep


def _interpret(var: str, rho: float) -> str:
    """Lectura causal / correlacional explícita, como pide el enunciado."""
    signo = "mayor" if rho > 0 else "menor"
    base = {
        "altitude_m": (
            f"A mayor altitud, {signo} tiempo de acceso. **Correlacional con un "
            "mecanismo físico plausible**: la altitud determina pendiente y "
            "sinuosidad de la vía, que sí causan tiempo de viaje. Pero la altitud "
            "también correlaciona con pobreza y dispersión, así que el coeficiente "
            "no aísla el efecto topográfico."
        ),
        "rural_share": (
            f"A mayor ruralidad, {signo} tiempo de acceso. **Correlacional, no "
            "causal**: la ruralidad no causa distancia, ambas son consecuencia del "
            "patrón histórico de poblamiento y de dónde el Estado ubicó hospitales."
        ),
        "population": (
            f"Los distritos más poblados tienen {signo} tiempo de acceso. "
            "**Correlacional con causalidad inversa probable**: los "
            "establecimientos resolutivos se instalan donde hay demanda, así que la "
            "población explica la oferta y no al revés."
        ),
        "population_density": (
            f"A mayor densidad, {signo} tiempo de acceso. **Correlacional**, con la "
            "misma causalidad inversa que la población, y además mecánica: densidad "
            "alta implica distancias cortas por construcción."
        ),
        "settlement_dispersion": (
            f"A mayor población fuera de la capital distrital, {signo} tiempo. "
            "**Correlacional con componente mecánico**: el establecimiento suele "
            "estar en la capital, de modo que la dispersión es casi una definición "
            "de distancia interna, no una causa independiente."
        ),
        "ccpp_count": (
            f"A más centros poblados, {signo} tiempo de acceso. **Ni causal ni "
            "informativo por sí solo**: el conteo confunde tamaño del distrito con "
            "dispersión; se incluye solo como control."
        ),
        "primary_care_per_10k": (
            f"A mayor oferta de primer nivel per cápita, {signo} tiempo hasta un "
            "resolutivo. **Correlacional y probablemente de selección**: el primer "
            "nivel se despliega justamente donde no hay hospital, así que un signo "
            "positivo indica sustitución, no que los puestos de salud alejen "
            "hospitales."
        ),
    }
    return base.get(var, f"Asociación {signo}; relación no interpretada.")


# ---------------------------------------------------------- comparación temporal
def temporal_comparison(access: pd.DataFrame, dlog: DecisionLog) -> pd.DataFrame:
    """Innovación: cobertura reconstruida año a año con ``INICIO_ACTIVIDAD``.

    No requiere volver a rutear: la matriz OD ya contiene todas las resolutivas
    actuales, así que restringir el conjunto de destinos a las que ya operaban en
    un año dado da la cobertura de ese año.

    Limitación declarada: RENIPRESS publicado no registra cierres (``CONDICION``
    es prácticamente constante), de modo que la serie asume que ningún
    establecimiento resolutivo dejó de operar. Es un supuesto optimista sobre el
    pasado, y por tanto **subestima** la mejora real de cobertura.
    """
    spec = CFG["sources"]["renipress_historic"]
    profile = CFG["routing"]["primary_profile"]
    bands = list(CFG["metrics"]["time_bands_min"])

    fac = pd.read_parquet(
        CFG.path("processed") / CFG.scoped_name("facilities", "parquet")
    )
    fac = fac[fac["is_resolutive"].fillna(False)].copy()
    fac["opened"] = pd.to_datetime(
        fac[spec["date_column"]], format=spec["date_format"], errors="coerce"
    )
    mat = pd.read_parquet(
        CFG.path("outputs") / f"od_{profile}_matrix_{CFG.mode}.parquet"
    ).set_index("demand_id")
    weights = access.set_index("demand_id")["design_weight"].reindex(mat.index)

    rows = []
    for year in spec["snapshot_years"]:
        active = fac.loc[fac["opened"].dt.year <= year, "COD_IPRESS"]
        cols = [c for c in mat.columns if c in set(active)]
        if not cols:
            continue
        t = mat[cols].min(axis=1)
        pop = float(weights.sum())
        row = {
            "anio": year,
            "n_resolutivas": len(cols),
            "poblacion": pop,
            "t_medio_min": weighted_mean(t, weights),
            "t_mediano_min": weighted_quantile(t, weights, 0.5),
        }
        for band in bands:
            row[f"share_hasta_{band}min"] = float(weights[t <= band].sum()) / pop
        rows.append(row)

    rep = pd.DataFrame(rows)
    if len(rep) >= 2:
        band = bands[1]
        delta = rep[f"share_hasta_{band}min"].iloc[-1] - rep[f"share_hasta_{band}min"].iloc[0]
        added = int(rep["n_resolutivas"].iloc[-1] - rep["n_resolutivas"].iloc[0])
        dlog.record(
            check="comparación temporal de cobertura",
            dataset="temporal",
            n_affected=added,
            n_total=int(rep["n_resolutivas"].iloc[-1]),
            action=(
                f"{added} establecimientos resolutivos nuevos entre "
                f"{rep['anio'].iloc[0]} y {rep['anio'].iloc[-1]}; la cobertura a "
                f"{band} min varía {delta:+.2%}"
            ),
            justification=(
                "serie reconstruida con INICIO_ACTIVIDAD, sin costo adicional de "
                "ruteo. Supone que ningún resolutivo cerró, porque RENIPRESS "
                "publicado no permite detectar cierres; por eso subestima la mejora"
            ),
        )
    return rep


# ------------------------------------------------------------------------ main
def run(save: bool = True) -> dict[str, pd.DataFrame]:
    dlog = DecisionLog(stage="metrics")
    access = enrich_context(load_access(), dlog)

    dlog.set_counter("demand_points", len(access))
    dlog.set_counter("population_represented", int(access["design_weight"].sum()))
    dlog.set_counter("unreachable_points", int(access["unreachable"].sum()))

    out: dict[str, pd.DataFrame] = {"access_points": access}
    for level in CFG["metrics"]["aggregation_levels"]:
        out[f"metrics_{level}"] = aggregate(access, level)

    out["urban_rural"] = urban_rural_contrast(access, dlog)
    out["worst_districts"] = worst_districts(access, out["metrics_district"])

    w = access["design_weight"]
    out["inequality"] = pd.DataFrame(
        [
            {
                "ambito": "nacional" if CFG.mode == "national" else CFG.mode,
                "medida": CFG["metrics"]["inequality_measure"],
                "gini_tiempo_acceso": gini(access["t_min"], w),
                "gini_urbano": gini(
                    access.loc[access.area_type == "URBANO", "t_min"],
                    access.loc[access.area_type == "URBANO", "design_weight"],
                ),
                "gini_rural": gini(
                    access.loc[access.area_type == "RURAL", "t_min"],
                    access.loc[access.area_type == "RURAL", "design_weight"],
                ),
                "p90_p50_ratio": (
                    weighted_quantile(access["t_min"], w, 0.90)
                    / max(weighted_quantile(access["t_min"], w, 0.50), 1e-9)
                ),
            }
        ]
    )
    out["lorenz"] = lorenz_curve(access["t_min"], w)

    ctx = build_district_context(access, dlog)
    out["district_context"] = ctx
    out["cross_analysis"] = cross_analysis(out["metrics_district"], ctx, dlog)

    if CFG.get("innovation", "run_temporal", default=False):
        out["temporal"] = temporal_comparison(access, dlog)

    if save:
        o = CFG.path("outputs")
        for key, df in out.items():
            if df is None or df.empty:
                continue
            path = o / f"{key}_{CFG.mode}.csv"
            df.to_csv(path, index=False, encoding="utf-8")
            LOG.info("escrito %s (%d filas)", path.name, len(df))
        dlog.save("metrics_log")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fase 3 — métricas ponderadas")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    out = run(save=not args.dry_run)

    nat = out["metrics_national"].iloc[0]
    bands = CFG["metrics"]["time_bands_min"]
    print(f"\nPoblación representada: {nat['poblacion']:,.0f}")
    print(f"Tiempo medio ponderado: {nat['t_medio_min']:.1f} min (mediana {nat['t_mediano_min']:.1f})")
    for b in bands:
        print(f"  ≤ {b:3d} min: {nat[f'share_hasta_{b}min']:.2%}")
    print(f"  > {bands[-1]} min: {nat[f'share_mas_{bands[-1]}min']:.2%}")
    print(f"Gini del tiempo de acceso: {out['inequality'].iloc[0]['gini_tiempo_acceso']:.3f}")
    print("\nPeores distritos por población fuera de umbral:")
    print(
        out["worst_districts"]
        .head(10)[["department", "district", "poblacion", "t_medio_min", "pob_fuera_umbral"]]
        .to_string(index=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
