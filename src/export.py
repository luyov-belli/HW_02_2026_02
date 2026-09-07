"""Fase 5 — Figuras y tablas para el informe LaTeX.

Todas las figuras salen en PDF vectorial y todas las tablas se generan
programáticamente con ``df.to_latex(..., booktabs)``. No hay capturas de pantalla
ni números tecleados a mano: ``report/main.tex`` cita las cifras a través de
macros de ``report/tables/kpis.tex``, de modo que recompilar el informe después
de una corrida nueva actualiza el texto solo.

Paleta: se usa la paleta de referencia validada (tres pendientes categóricas como
máximo por figura, que es el límite que pasa la verificación de daltonismo en
todas las combinaciones de pares; rampa secuencial de un solo tono para el mapa).

Uso::

    python -m src.export                 # figuras + tablas + kpis
    python -m src.export --only tables
"""

from __future__ import annotations

import argparse
import json

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from .config import CFG  # noqa: E402
from .utils import get_logger, read_output_csv  # noqa: E402

LOG = get_logger("export")

# --- paleta de referencia (modo claro) --------------------------------------
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#fcfcfb"
# Categóricas: solo los tres primeros slots, que son los que pasan la
# verificación de daltonismo con todos los pares en juego.
CAT = ["#2a78d6", "#eb6834", "#1baf7a"]
# Rampa secuencial ordinal (azul). El paso más claro no baja de 250 para que
# siga teniendo contraste sobre el fondo claro.
SEQ = ["#86b6ef", "#3987e5", "#256abf", "#0d366b"]
NODATA = "#f0efec"
CRITICAL = "#d03b3b"


def _style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "font.family": "sans-serif",
            "font.size": 9,
            "axes.labelcolor": INK_2,
            "axes.edgecolor": AXIS,
            "axes.titlesize": 10.5,
            # "semibold" no es un peso que matplotlib resuelva con DejaVu Sans:
            # cae a 700 y emite un aviso en cada figura. Se usa 700 directamente.
            "axes.titleweight": "bold",
            "axes.titlecolor": INK,
            "axes.grid": True,
            "axes.axisbelow": True,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelcolor": INK_2,
            "ytick.labelcolor": INK_2,
            "legend.frameon": False,
            "lines.linewidth": 2.0,
            "lines.markersize": 5.0,
            "pdf.fonttype": 42,
        }
    )


def _despine(ax: "plt.Axes", left: bool = False) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    if left:
        ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.spines["left"].set_color(AXIS)


def _save(fig: "plt.Figure", name: str) -> None:
    path = CFG.path("figures") / f"{name}.{CFG['report']['figure_format']}"
    fig.savefig(path, bbox_inches="tight", dpi=int(CFG["report"]["figure_dpi"]))
    plt.close(fig)
    LOG.info("figura %s", path.name)


def _read(name: str) -> pd.DataFrame | None:
    """Lee un CSV de salida preservando los ceros a la izquierda de los ubigeo."""
    path = CFG.path("outputs") / f"{name}_{CFG.mode}.csv"
    return read_output_csv(path) if path.exists() else None


# ------------------------------------------------------------------- figuras
def fig_choropleth(bands: list[int]) -> None:
    """Mapa distrital del tiempo medio de acceso, en clases de cobertura."""
    import geopandas as gpd

    metrics = _read("metrics_district")
    if metrics is None:
        return
    gdf = gpd.read_file(CFG.path("processed") / CFG.scoped_name("districts", "gpkg"))
    gdf = gdf.merge(
        metrics[["ubigeo", "t_medio_min", "poblacion"]], on="ubigeo", how="left"
    )

    edges = [0, *bands, np.inf]
    labels = [f"≤ {bands[0]}"] + [
        f"{a}–{b}" for a, b in zip(bands[:-1], bands[1:], strict=True)
    ] + [f"> {bands[-1]}"]
    gdf["clase"] = pd.cut(gdf["t_medio_min"], bins=edges, labels=labels, right=True)

    fig, ax = plt.subplots(figsize=(6.0, 7.6))
    # geopandas deriva el aspecto de la extensión de los datos, así que dibujar
    # un subconjunto vacío produce un aspecto NaN y aborta.
    nodata = gdf[gdf["clase"].isna()]
    if not nodata.empty:
        nodata.plot(ax=ax, color=NODATA, edgecolor="white", linewidth=0.12)
    for label, color in zip(labels, SEQ, strict=True):
        sub = gdf[gdf["clase"] == label]
        if not sub.empty:
            sub.plot(ax=ax, color=color, edgecolor="white", linewidth=0.12)

    ax.set_axis_off()
    ax.set_title(
        "Tiempo medio en auto hasta un establecimiento resolutivo\n"
        f"por distrito, ponderado por población ({CFG.mode})",
        loc="left", color=INK,
    )
    handles = [
        Patch(facecolor=c, edgecolor="white", label=f"{lab} min")
        for lab, c in zip(labels, SEQ, strict=True)
    ]
    if not nodata.empty:
        handles.append(Patch(facecolor=NODATA, edgecolor="white", label="sin dato"))
    # La leyenda va fuera del eje: dentro del mapa taparía territorio, y qué
    # esquina queda libre depende del alcance de la corrida.
    ax.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.0),
        ncol=min(len(handles), 3),
        title="Minutos en auto",
        title_fontsize=8.5,
        fontsize=8.5,
        labelcolor=INK_2,
    )
    _save(fig, "fig1_choropleth_acceso")


def fig_coverage_ecdf(bands: list[int]) -> None:
    """Población acumulada por tiempo de acceso, urbano vs. rural."""
    access = _read("access_points")
    if access is None:
        return
    fig, ax = plt.subplots(figsize=(6.4, 3.9))

    for (label, color) in zip(["URBANO", "RURAL"], CAT[:2], strict=True):
        g = access[access["area_type"] == label].dropna(subset=["t_min"])
        if g.empty:
            continue
        order = g.sort_values("t_min")
        share = order["design_weight"].cumsum() / order["design_weight"].sum()
        ax.plot(order["t_min"], share * 100, color=color, label=label.capitalize())
        # Etiqueta directa, para que la identidad no dependa solo del color.
        ax.annotate(
            label.capitalize(),
            xy=(order["t_min"].iloc[int(len(order) * 0.72)], share.iloc[int(len(order) * 0.72)] * 100),
            xytext=(4, 3), textcoords="offset points", fontsize=8.5, color=INK_2,
        )

    for band in bands:
        ax.axvline(band, color=AXIS, linewidth=0.9, linestyle=(0, (4, 3)), zorder=0)
        ax.annotate(f"{band} min", xy=(band, 3), xytext=(3, 0),
                    textcoords="offset points", fontsize=8, color=MUTED)

    ax.set_xlim(0, min(300, float(access["t_min"].quantile(0.995))))
    ax.set_ylim(0, 100)
    ax.set_xlabel("Minutos en auto hasta el establecimiento resolutivo más cercano")
    ax.set_ylabel("% de la población acumulada")
    ax.set_title("Cobertura acumulada por tiempo de acceso", loc="left")
    ax.legend(loc="lower right", fontsize=8.5, labelcolor=INK_2)
    _despine(ax)
    _save(fig, "fig2_cobertura_acumulada")


def fig_lorenz() -> None:
    """Curva de Lorenz del tiempo de acceso."""
    lz = _read("lorenz")
    ineq = _read("inequality")
    if lz is None:
        return
    fig, ax = plt.subplots(figsize=(4.5, 4.3))
    ax.plot([0, 1], [0, 1], color=AXIS, linewidth=1.2, linestyle=(0, (4, 3)))
    ax.plot(lz["share_poblacion"], lz["share_tiempo_acumulado"], color=CAT[0])
    ax.fill_between(
        lz["share_poblacion"], lz["share_tiempo_acumulado"], lz["share_poblacion"],
        color=CAT[0], alpha=0.12, linewidth=0,
    )
    ax.annotate("Igualdad perfecta", xy=(0.62, 0.62), xytext=(-2, 8),
                textcoords="offset points", fontsize=8.5, color=MUTED, rotation=38)
    if ineq is not None and not ineq.empty:
        g = float(ineq.iloc[0]["gini_tiempo_acceso"])
        ax.annotate(f"Gini = {g:.3f}", xy=(0.05, 0.88), fontsize=11, color=INK,
                    fontweight="bold")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Proporción acumulada de población\n(ordenada de menor a mayor tiempo)")
    ax.set_ylabel("Proporción acumulada del tiempo total de viaje")
    ax.set_title("Concentración de la carga de desplazamiento", loc="left")
    _despine(ax)
    _save(fig, "fig3_lorenz")


def fig_access_vs_altitude() -> None:
    """Dispersión distrital: acceso frente a altitud, por región natural."""
    metrics = _read("metrics_district")
    ctx = _read("district_context")
    if metrics is None or ctx is None:
        return
    access = _read("access_points")
    region = (
        access.groupby("ubigeo")["natural_region"].agg(lambda s: s.mode().iloc[0])
        if access is not None else None
    )
    df = metrics.merge(
        ctx[["ubigeo", "altitude_m", "rural_share"]], on="ubigeo", how="left"
    )
    if region is not None:
        df = df.merge(region.rename("natural_region"), on="ubigeo", how="left")

    fig, ax = plt.subplots(figsize=(6.4, 4.1))
    regions = ["costa", "sierra", "selva"]
    for reg, color in zip(regions, CAT, strict=True):
        g = df[df.get("natural_region") == reg].dropna(subset=["altitude_m", "t_medio_min"])
        if g.empty:
            continue
        ax.scatter(
            g["altitude_m"], g["t_medio_min"],
            s=np.clip(g["poblacion"] / 900.0, 8, 260),
            color=color, alpha=0.55, linewidths=0.6, edgecolors="white",
            label=reg.capitalize(),
        )
    ax.set_xlabel("Altitud del distrito (m s. n. m., mediana de sus centros poblados)")
    ax.set_ylabel("Tiempo medio de acceso (min)")
    ax.set_title(
        "Acceso frente a altitud. El área del punto es la población del distrito",
        loc="left",
    )
    ax.legend(loc="upper left", fontsize=8.5, labelcolor=INK_2, title="Región natural",
              title_fontsize=8.5)
    _despine(ax)
    _save(fig, "fig4_acceso_vs_altitud")


def fig_mclp() -> None:
    """Población adicional cubierta por cada ascenso, en el orden que elige el MCLP."""
    seq = _read("mclp_sequence")
    if seq is None or seq.empty:
        return
    top = seq.head(20)
    fig, ax = plt.subplots(figsize=(6.4, 4.4))
    ax.barh(
        np.arange(len(top)), top["poblacion_marginal"] / 1000.0,
        color=CAT[0], height=0.66,
    )
    labels = [
        f"{r.orden}. {str(r.NOMBRE)[:34].title()} ({r.DEPARTAMENTO.title()})"
        for r in top.itertuples()
    ]
    ax.set_yticks(np.arange(len(top)), labels, fontsize=7.6)
    ax.invert_yaxis()
    ax.set_xlabel("Población adicional cubierta dentro de 60 min (miles de habitantes)")
    ax.set_title(
        "Ascensos de I-3/I-4 a resolutivo, en orden de rendimiento marginal",
        loc="left",
    )
    ax.grid(axis="y", visible=False)
    _despine(ax, left=True)
    _save(fig, "fig5_mclp_secuencia")


def fig_temporal(bands: list[int]) -> None:
    """Cobertura reconstruida por año con INICIO_ACTIVIDAD."""
    tmp = _read("temporal")
    if tmp is None or tmp.empty:
        return
    fig, ax = plt.subplots(figsize=(6.0, 3.7))
    for band, color in zip(bands, CAT, strict=True):
        col = f"share_hasta_{band}min"
        if col not in tmp:
            continue
        ax.plot(tmp["anio"], tmp[col] * 100, color=color, marker="o", label=f"≤ {band} min")
        ax.annotate(
            f"≤ {band} min", xy=(tmp["anio"].iloc[-1], tmp[col].iloc[-1] * 100),
            xytext=(6, -2), textcoords="offset points", fontsize=8.5, color=INK_2,
        )
    ax.set_xticks(tmp["anio"].tolist())
    ax.set_xlabel("Año (conjunto de establecimientos resolutivos ya operativos)")
    ax.set_ylabel("% de la población cubierta")
    ax.set_title(
        "Evolución de la cobertura con la red vial actual\n"
        "Solo varía el conjunto de establecimientos, no las carreteras",
        loc="left",
    )
    ax.legend(loc="lower right", fontsize=8.5, labelcolor=INK_2)
    ax.margins(x=0.12)
    _despine(ax)
    _save(fig, "fig6_temporal_cobertura")


def fig_straight_vs_network() -> None:
    """Cuánto subestima la línea recta frente a la red vial."""
    comp = _read("straight_vs_network")
    if comp is None or comp.empty:
        return
    vals = dict(zip(comp["metrica"], comp["valor"], strict=True))
    bands = list(CFG["metrics"]["time_bands_min"])
    pobl = [vals.get(f"poblacion_reclasificada_{b}min", 0.0) / 1000.0 for b in bands]

    fig, ax = plt.subplots(figsize=(5.6, 3.5))
    bars = ax.bar([f"{b} min" for b in bands], pobl, color=CRITICAL, width=0.52)
    for bar, v in zip(bars, pobl, strict=True):
        ax.annotate(
            f"{v:,.0f}k", xy=(bar.get_x() + bar.get_width() / 2, v),
            xytext=(0, 4), textcoords="offset points", ha="center",
            fontsize=9, color=INK,
        )
    ax.set_ylabel("Miles de habitantes")
    ax.set_xlabel("Umbral de cobertura")
    idx = vals.get("indice_desvio_distancia_mediano", float("nan"))
    ax.set_title(
        "Población que un análisis con distancia en línea recta\n"
        f"declararía cubierta y no lo está (índice de desvío mediano: {idx:.2f}×)",
        loc="left",
    )
    ax.grid(axis="x", visible=False)
    _despine(ax)
    _save(fig, "fig7_linea_recta_vs_red")


def fig_cross_mode() -> None:
    """Relación de tiempos entre modos y cambio del establecimiento más cercano."""
    cm = _read("cross_mode")
    if cm is None or cm.empty:
        return
    fig, ax = plt.subplots(figsize=(5.6, 3.4))
    x = np.arange(len(cm))
    ax.bar(x, cm["ratio_mediano"], color=CAT[0], width=0.5, label="Mediana")
    ax.errorbar(
        x, cm["ratio_mediano"],
        yerr=[np.zeros(len(cm)), (cm["ratio_p90"] - cm["ratio_mediano"]).clip(lower=0)],
        fmt="none", ecolor=INK_2, elinewidth=1.4, capsize=4,
    )
    for i, row in enumerate(cm.itertuples()):
        ax.annotate(
            f"{row.ratio_mediano:.1f}×", xy=(i, row.ratio_mediano), xytext=(0, 5),
            textcoords="offset points", ha="center", fontsize=9, color=INK,
        )
    ax.set_xticks(x, [p.capitalize() for p in cm["profile"]])
    ax.set_ylabel("Tiempo respecto del auto")
    ax.set_title(
        "Cuánto más tarda cada modo que el auto\nLa barra de error llega al percentil 90",
        loc="left",
    )
    ax.grid(axis="x", visible=False)
    ax.legend(
        handles=[Line2D([0], [0], color=INK_2, lw=1.4, label="Mediana a p90")],
        loc="upper left", fontsize=8.5, labelcolor=INK_2,
    )
    _despine(ax)
    _save(fig, "fig8_multimodal")


# -------------------------------------------------------------------- tablas
def _to_latex(
    df: pd.DataFrame, name: str, caption: str, label: str, floatfmt: str = "%.1f"
) -> None:
    path = CFG.path("tables") / f"{name}.tex"
    tex = df.to_latex(
        index=False,
        escape=True,
        float_format=lambda v: floatfmt % v,
        caption=caption,
        label=label,
        position="htbp",
        column_format="l" * 1 + "r" * (df.shape[1] - 1),
    )
    # booktabs ya lo emite pandas; solo se ajusta el tamaño para tablas anchas.
    if df.shape[1] > 6:
        tex = tex.replace("\\begin{tabular}", "\\footnotesize\n\\begin{tabular}")
    path.write_text(tex, encoding="utf-8")
    LOG.info("tabla %s (%d filas)", path.name, len(df))


def tables(bands: list[int]) -> None:
    dep = _read("metrics_department")
    if dep is not None:
        cols = ["department", "poblacion", "t_medio_min", "t_mediano_min", "t_p90_min"]
        cols += [f"share_hasta_{b}min" for b in bands]
        t = dep[cols].copy()
        for b in bands:
            t[f"share_hasta_{b}min"] = t[f"share_hasta_{b}min"] * 100
        t.columns = ["Departamento", "Población", "Media", "Mediana", "p90"] + [
            f"≤{b} min (\\%)" for b in bands
        ]
        _to_latex(
            t, "tab_cobertura_departamento",
            "Cobertura y tiempo de acceso por departamento, ponderados por población. "
            "Tiempos en minutos de auto hasta el establecimiento resolutivo más cercano.",
            "tab:cobertura-dep",
        )

    worst = _read("worst_districts")
    if worst is not None:
        band = bands[1]
        cols = ["department", "district", "poblacion", "t_medio_min",
                f"share_hasta_{band}min", "pob_fuera_umbral", "high_variance"]
        cols = [c for c in cols if c in worst.columns]
        t = worst[cols].head(15).copy()
        if f"share_hasta_{band}min" in t:
            t[f"share_hasta_{band}min"] *= 100
        if "high_variance" in t:
            t["high_variance"] = np.where(t["high_variance"].fillna(False), "sí", "no")
        t.columns = ["Departamento", "Distrito", "Población", "Media (min)",
                     f"≤{band} min (\\%)", "Pob. fuera", "Alta var."][: len(t.columns)]
        _to_latex(
            t, "tab_peores_distritos",
            f"Brechas críticas: 15 distritos con más población fuera de {band} minutos "
            "de un establecimiento resolutivo. «Alta var.» marca los distritos cuya "
            "estimación descansa en pocos puntos muestreados.",
            "tab:peores-distritos",
        )

    ur = _read("urban_rural")
    if ur is not None:
        cols = ["area_type", "natural_region", "poblacion", "t_medio_min",
                "t_p90_min", "gini_tiempo", "altitud_mediana_m"]
        t = ur[[c for c in cols if c in ur.columns]].copy()
        t.columns = ["Área", "Región natural", "Población", "Media (min)",
                     "p90 (min)", "Gini", "Altitud (m)"][: len(t.columns)]
        _to_latex(
            t, "tab_urbano_rural",
            "Contraste urbano/rural por región natural. La clasificación es la del "
            "INEI incluida en el listado de centros poblados.",
            "tab:urbano-rural",
        )

    cross = _read("cross_analysis")
    if cross is not None:
        t = cross[["variable", "n_distritos", "spearman_rho",
                   "t_medio_quintil_inferior", "t_medio_quintil_superior"]].copy()
        t.columns = ["Variable", "Distritos", "Spearman $\\rho$",
                     "Quintil inferior", "Quintil superior"]
        _to_latex(
            t, "tab_analisis_cruzado",
            "Asociación entre el tiempo medio de acceso distrital y variables de "
            "contexto. Los quintiles se refieren a la variable de contexto y sus "
            "medias de acceso están ponderadas por población. Ninguna de estas "
            "asociaciones identifica un efecto causal.",
            "tab:analisis-cruzado", floatfmt="%.2f",
        )

    snap = _read("snapping")
    if snap is not None:
        t = snap[["conjunto", "n_puntos", "snap_medio_m", "snap_mediana_m",
                  "snap_p95_m", "snap_max_m", "n_sobre_limite"]].copy()
        t.columns = ["Conjunto", "Puntos", "Media (m)", "Mediana (m)", "p95 (m)",
                     "Máximo (m)", "Sobre límite"]
        _to_latex(
            t, "tab_snapping",
            "Análisis de enganche a la red vial. Distancia entre la coordenada "
            "declarada y el punto de la red al que OSRM ancló cada ubicación.",
            "tab:snapping",
        )

    cm = _read("cross_mode")
    if cm is not None:
        t = cm.copy()
        t.columns = [c.replace("_", " ") for c in t.columns]
        _to_latex(
            t, "tab_multimodal",
            "Comparación multimodal sobre la submuestra. «Cambia establecimiento» "
            "cuenta los orígenes cuyo establecimiento más cercano no es el mismo en "
            "auto que en el modo indicado.",
            "tab:multimodal", floatfmt="%.2f",
        )

    mclp = _read("mclp_frontier")
    if mclp is not None:
        t = mclp[["presupuesto", "ganancia_vs_base", "share_cubierta",
                  "cota_superior_optimo", "brecha_maxima_vs_cota"]].copy()
        t["share_cubierta"] *= 100
        t.columns = ["Ascensos", "Población ganada", "Cobertura (\\%)",
                     "Cota superior", "Brecha máx."]
        _to_latex(
            t, "tab_mclp",
            "Frontera del problema de cobertura máxima: población adicional cubierta "
            "dentro de 60 minutos al ascender $B$ establecimientos I-3/I-4. La cota "
            "superior acota el óptimo por arriba, de modo que la brecha máxima es la "
            "pérdida en el peor caso frente a la solución exacta.",
            "tab:mclp", floatfmt="%.0f",
        )

    tmp = _read("temporal")
    if tmp is not None:
        t = tmp[["anio", "n_resolutivas"] + [f"share_hasta_{b}min" for b in bands]].copy()
        for b in bands:
            t[f"share_hasta_{b}min"] *= 100
        t.columns = ["Año", "Resolutivos"] + [f"≤{b} min (\\%)" for b in bands]
        _to_latex(
            t, "tab_temporal",
            "Cobertura reconstruida con el conjunto de establecimientos resolutivos "
            "operativos en cada año, manteniendo fija la red vial de 2026. Supone que "
            "ningún establecimiento cerró, porque RENIPRESS publicado no lo registra.",
            "tab:temporal", floatfmt="%.1f",
        )

    _table_data_quality()


def _table_data_quality() -> None:
    path = CFG.path("outputs") / "data_quality_report.json"
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame(payload["decisions"])
    keep = df[df["n_affected"] > 0][["dataset", "check", "n_affected", "share_affected"]]
    keep = keep.copy()
    keep["share_affected"] = keep["share_affected"].astype(float) * 100
    keep.columns = ["Fuente", "Verificación", "Registros", "\\% del total"]
    _to_latex(
        keep, "tab_calidad_datos",
        "Reporte de calidad de datos: verificaciones que detectaron al menos un "
        "registro afectado. El detalle con la acción tomada y su justificación está "
        "en \\texttt{data/outputs/data\\_quality\\_report.json}.",
        "tab:calidad-datos", floatfmt="%.2f",
    )


# ---------------------------------------------------------------------- KPIs
#: Macros que ``report/main.tex`` cita en su prosa. Sirven de contrato: si una
#: corrida no puede calcular alguna, se emite un marcador visible y el informe
#: compila igual, en lugar de que pdflatex aborte con "undefined control
#: sequence" a mitad del documento.
REQUIRED_KPIS: tuple[str, ...] = (
    "PoblacionTotal", "TiempoMedio", "TiempoMediano", "TiempoPnoventa",
    "ShareHasta30", "ShareHasta60", "ShareHasta120",
    "PobHasta30", "PobHasta60", "PobHasta120",
    "ShareMas120", "PobMas120",
    "Gini", "GiniUrbano", "GiniRural",
    "TiempoMedioUrbano", "TiempoMedioRural",
    "PeorDistrito", "PeorDistritoDep", "PeorDistritoPob",
    "IndiceDesvio", "PobReclasificada", "ShareCambiaCercano",
    "MclpPresupuesto", "MclpGanancia", "MclpPresupuestoMax", "MclpGananciaMax",
    "TemporalAnioIni", "TemporalAnioFin", "TemporalDelta", "TemporalNuevos",
    "QRenipressRowsRaw", "QCcppValid", "QFacilitiesResolutive",
    "QFacilitiesResolutiveByCategory", "QFacilitiesResolutiveOnRecoveredCoords",
)


def kpis() -> dict[str, float]:
    """Macros LaTeX con las cifras clave, para que el texto se actualice solo."""
    bands = list(CFG["metrics"]["time_bands_min"])
    vals: dict[str, object] = {}

    nat = _read("metrics_national")
    if nat is not None and not nat.empty:
        r = nat.iloc[0]
        vals["PoblacionTotal"] = f"{r['poblacion']:,.0f}".replace(",", "\\,")
        vals["TiempoMedio"] = f"{r['t_medio_min']:.1f}"
        vals["TiempoMediano"] = f"{r['t_mediano_min']:.1f}"
        vals["TiempoPnoventa"] = f"{r['t_p90_min']:.1f}"
        for b in bands:
            vals[f"ShareHasta{b}"] = f"{r[f'share_hasta_{b}min'] * 100:.1f}"
            vals[f"PobHasta{b}"] = f"{r[f'pob_hasta_{b}min']:,.0f}".replace(",", "\\,")
        vals[f"ShareMas{bands[-1]}"] = f"{r[f'share_mas_{bands[-1]}min'] * 100:.1f}"
        vals[f"PobMas{bands[-1]}"] = f"{r[f'pob_mas_{bands[-1]}min']:,.0f}".replace(",", "\\,")

    ineq = _read("inequality")
    if ineq is not None and not ineq.empty:
        vals["Gini"] = f"{ineq.iloc[0]['gini_tiempo_acceso']:.3f}"
        vals["GiniUrbano"] = f"{ineq.iloc[0]['gini_urbano']:.3f}"
        vals["GiniRural"] = f"{ineq.iloc[0]['gini_rural']:.3f}"

    ur = _read("urban_rural")
    if ur is not None and not ur.empty:
        for area, key in (("URBANO", "Urbano"), ("RURAL", "Rural")):
            g = ur[ur["area_type"] == area]
            if not g.empty:
                w = g["poblacion"]
                vals[f"TiempoMedio{key}"] = f"{np.average(g['t_medio_min'], weights=w):.1f}"

    worst = _read("worst_districts")
    if worst is not None and not worst.empty:
        r = worst.iloc[0]
        vals["PeorDistrito"] = str(r["district"]).title()
        vals["PeorDistritoDep"] = str(r["department"]).title()
        vals["PeorDistritoPob"] = f"{r['pob_fuera_umbral']:,.0f}".replace(",", "\\,")

    comp = _read("straight_vs_network")
    if comp is not None and not comp.empty:
        d = dict(zip(comp["metrica"], comp["valor"], strict=True))
        vals["IndiceDesvio"] = f"{d.get('indice_desvio_distancia_mediano', float('nan')):.2f}"
        vals["PobReclasificada"] = (
            f"{d.get(f'poblacion_reclasificada_{bands[1]}min', 0):,.0f}".replace(",", "\\,")
        )
        vals["ShareCambiaCercano"] = (
            f"{d.get('share_cambia_establecimiento_mas_cercano', 0) * 100:.1f}"
        )

    mclp = _read("mclp_frontier")
    if mclp is not None and not mclp.empty:
        r = mclp.iloc[0]
        vals["MclpPresupuesto"] = f"{r['presupuesto']:.0f}"
        vals["MclpGanancia"] = f"{r['ganancia_vs_base']:,.0f}".replace(",", "\\,")
        r2 = mclp.iloc[-1]
        vals["MclpPresupuestoMax"] = f"{r2['presupuesto']:.0f}"
        vals["MclpGananciaMax"] = f"{r2['ganancia_vs_base']:,.0f}".replace(",", "\\,")

    tmp = _read("temporal")
    if tmp is not None and len(tmp) >= 2:
        b = bands[1]
        vals["TemporalAnioIni"] = f"{tmp['anio'].iloc[0]:.0f}"
        vals["TemporalAnioFin"] = f"{tmp['anio'].iloc[-1]:.0f}"
        vals["TemporalDelta"] = (
            f"{(tmp[f'share_hasta_{b}min'].iloc[-1] - tmp[f'share_hasta_{b}min'].iloc[0]) * 100:+.1f}"
        )
        vals["TemporalNuevos"] = (
            f"{tmp['n_resolutivas'].iloc[-1] - tmp['n_resolutivas'].iloc[0]:.0f}"
        )

    qpath = CFG.path("outputs") / "data_quality_report.json"
    if qpath.exists():
        counters = json.loads(qpath.read_text(encoding="utf-8"))["counters"]
        for key, value in counters.items():
            macro = "".join(p.capitalize() for p in key.split("_"))
            vals[f"Q{macro}"] = f"{value:,}".replace(",", "\\,")

    lines = [
        "% Generado por src/export.py — no editar a mano.",
        "% Cada macro proviene de data/outputs/; recompilar tras una corrida nueva",
        "% actualiza el texto del informe automáticamente.",
        "",
    ]
    for key, value in vals.items():
        lines.append(f"\\newcommand{{\\kpi{key}}}{{{value}}}")

    # Red de seguridad: si una corrida no produce algún insumo (p. ej. no se
    # ejecutó el MCLP), la macro correspondiente quedaría indefinida y pdflatex
    # abortaría. \providecommand no hace nada si la macro ya existe, así que las
    # que falten se imprimen como marcador visible en lugar de romper el informe.
    lines += [
        "",
        "% Marcadores para las macros que esta corrida no pudo calcular.",
    ]
    for key in REQUIRED_KPIS:
        lines.append(
            f"\\providecommand{{\\kpi{key}}}{{\\textbf{{[sin dato: {key}]}}}}"
        )
        if key not in vals:
            LOG.warning("KPI sin dato en esta corrida: %s", key)

    (CFG.path("tables") / "kpis.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (CFG.path("outputs") / f"kpis_{CFG.mode}.json").write_text(
        json.dumps(vals, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    LOG.info("kpis.tex con %d macros", len(vals))
    return vals  # type: ignore[return-value]


# ------------------------------------------------------------------------ main
def run(what: str = "all") -> None:
    _style()
    bands = list(CFG["metrics"]["time_bands_min"])
    if what in ("all", "figures"):
        fig_choropleth(bands)
        fig_coverage_ecdf(bands)
        fig_lorenz()
        fig_access_vs_altitude()
        fig_mclp()
        fig_temporal(bands)
        fig_straight_vs_network()
        fig_cross_mode()
    if what in ("all", "tables"):
        tables(bands)
        kpis()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fase 5 — figuras y tablas")
    p.add_argument("--only", default="all", choices=["all", "figures", "tables"])
    args = p.parse_args(argv)
    run(args.only)
    print(
        f"figuras en {CFG.path('figures')}\n"
        f"tablas en  {CFG.path('tables')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
