"""Fase 4 — Dashboard de accesibilidad a establecimientos de salud resolutivos.

Ejecutar con::

    pip install -r requirements.txt
    streamlit run app.py

La aplicación **solo lee archivos precomputados** de ``data/processed`` y
``data/outputs``: no llama a ningún motor de ruteo al cargar, de modo que
funciona sin Docker, sin OSRM y sin conexión. El simulador de escenarios
recalcula la cobertura con la matriz OD de la Fase 2 que está versionada en el
repositorio.

Público objetivo: una dirección regional de salud. Cada vista responde una
pregunta concreta y el texto evita la jerga del pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from src.config import CFG
from src.metrics import gini, weighted_mean, weighted_quantile
from src.models import recompute_coverage
from src.utils import read_output_csv

# --- paleta de referencia (la misma del informe) ----------------------------
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
CAT = ["#2a78d6", "#eb6834", "#1baf7a"]
SEQ = ["#86b6ef", "#3987e5", "#256abf", "#0d366b"]
NODATA = "#f0efec"
CRITICAL = "#d03b3b"
GOOD = "#0ca30c"

BANDS = list(CFG["metrics"]["time_bands_min"])
OUT = CFG.path("outputs", create=False)
PROC = CFG.path("processed", create=False)

st.set_page_config(
    page_title="Acceso vial a salud resolutiva — Perú",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================== carga de datos
@st.cache_data(show_spinner="Cargando resultados precomputados…")
def load_all(scope: str) -> dict[str, object]:
    def csv(name: str) -> pd.DataFrame:
        path = OUT / f"{name}_{scope}.csv"
        # read_output_csv preserva los ceros a la izquierda de ubigeo y
        # COD_IPRESS, que pandas convertiría en enteros y rompería los cruces.
        return read_output_csv(path) if path.exists() else pd.DataFrame()

    data: dict[str, object] = {
        "access": csv("access_points"),
        "district": csv("metrics_district"),
        "department": csv("metrics_department"),
        "national": csv("metrics_national"),
        "worst": csv("worst_districts"),
        "urban_rural": csv("urban_rural"),
        "inequality": csv("inequality"),
        "lorenz": csv("lorenz"),
        "cross": csv("cross_analysis"),
        "temporal": csv("temporal"),
        "sfca_district": csv("sfca_district"),
        "mclp_frontier": csv("mclp_frontier"),
        "mclp_sequence": csv("mclp_sequence"),
        "cross_mode": csv("cross_mode"),
        "snapping": csv("snapping"),
        "straight": csv("straight_vs_network"),
    }

    fpath = PROC / f"facilities_{scope}.parquet"
    data["facilities"] = pd.read_parquet(fpath) if fpath.exists() else pd.DataFrame()

    cand = OUT / f"od_candidates_{scope}_{CFG['routing']['primary_profile']}.parquet"
    data["candidates"] = pd.read_parquet(cand) if cand.exists() else pd.DataFrame()

    qpath = OUT / "data_quality_report.json"
    data["quality"] = (
        json.loads(qpath.read_text(encoding="utf-8")) if qpath.exists() else {}
    )
    return data


@st.cache_data(show_spinner="Simplificando polígonos distritales…")
def load_districts_geojson(scope: str, tolerance: float = 0.004) -> dict | None:
    """Polígonos simplificados. Sin simplificar, 1 873 distritos ahogan al navegador."""
    import geopandas as gpd

    path = PROC / f"districts_{scope}.gpkg"
    if not path.exists():
        return None
    gdf = gpd.read_file(path)
    gdf["geometry"] = gdf.geometry.simplify(tolerance, preserve_topology=True)
    return json.loads(gdf[["ubigeo", "geometry"]].to_json())


def band_of(minutes: float) -> int:
    for i, b in enumerate(BANDS):
        if minutes <= b:
            return i
    return len(BANDS)


BAND_LABELS = [f"≤ {BANDS[0]} min"] + [
    f"{a}–{b} min" for a, b in zip(BANDS[:-1], BANDS[1:], strict=True)
] + [f"> {BANDS[-1]} min"]


# ============================================================== estado inicial
scope = CFG.mode
DATA = load_all(scope)
access_all: pd.DataFrame = DATA["access"]  # type: ignore[assignment]

if access_all.empty:
    st.title("Acceso vial a establecimientos de salud resolutivos")
    st.error(
        "No hay resultados precomputados para el alcance "
        f"**{scope}**. Falta `data/outputs/access_points_{scope}.csv`."
    )
    st.markdown(
        "Para generarlos:\n\n"
        "```bash\n"
        "python -m src.acquisition\n"
        "python -m src.validation\n"
        "python -m src.routing --stage primary   # requiere OSRM o engine='graph'\n"
        "python -m src.metrics\n"
        "python -m src.models\n"
        "```\n\n"
        "O esperar al workflow `Fase 2 — OSRM en Docker`, que los commitea al "
        "repositorio."
    )
    st.stop()


# ==================================================================== sidebar
st.sidebar.title("Filtros")
st.sidebar.caption(
    f"Alcance de la corrida: **{scope}** · "
    f"{access_all['department'].nunique()} departamentos"
)

deps = sorted(access_all["department"].dropna().unique())
sel_dep = st.sidebar.multiselect("Departamento", deps, default=[])

prov_pool = access_all[access_all["department"].isin(sel_dep)] if sel_dep else access_all
provs = sorted(prov_pool["province"].dropna().unique())
sel_prov = st.sidebar.multiselect("Provincia", provs, default=[])

sel_area = st.sidebar.multiselect(
    "Ámbito", ["URBANO", "RURAL"], default=["URBANO", "RURAL"]
)
sel_region = st.sidebar.multiselect(
    "Región natural",
    sorted(access_all["natural_region"].dropna().unique()),
    default=[],
)

threshold = st.sidebar.select_slider(
    "Umbral de cobertura (minutos)",
    options=[15, 30, 45, 60, 90, 120, 180, 240],
    value=BANDS[1],
    help="Define qué se considera «población cubierta» en los indicadores y el mapa.",
)

st.sidebar.divider()
st.sidebar.subheader("Capa de establecimientos")
fac_all: pd.DataFrame = DATA["facilities"]  # type: ignore[assignment]
show_resolutive = st.sidebar.checkbox("Resolutivos (II-1 o superior)", value=True)
show_candidates = st.sidebar.checkbox("Candidatos I-3 / I-4", value=False)
cat_options = (
    sorted(fac_all.loc[fac_all["has_coords"].fillna(False), "CATEGORIA"].dropna().unique())
    if not fac_all.empty else []
)
sel_cat = st.sidebar.multiselect("Categoría", cat_options, default=[])
inst_options = (
    sorted(fac_all["INSTITUCION"].dropna().unique()) if not fac_all.empty else []
)
sel_inst = st.sidebar.multiselect("Institución", inst_options, default=[])

st.sidebar.divider()
st.sidebar.caption(
    "Los datos son precomputados: la aplicación no consulta ningún motor de "
    "ruteo al cargar."
)


# ============================================================ filtro aplicado
def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    out = df
    if sel_dep:
        out = out[out["department"].isin(sel_dep)]
    if sel_prov:
        out = out[out["province"].isin(sel_prov)]
    if sel_area:
        out = out[out["area_type"].isin(sel_area)]
    if sel_region:
        out = out[out["natural_region"].isin(sel_region)]
    return out


access = apply_filters(access_all)

st.title("Acceso vial a establecimientos de salud resolutivos")
st.caption(
    "Cuánto tarda la población, por carretera y en auto, en llegar a un "
    "establecimiento con capacidad resolutiva (categoría II-1 o superior, "
    "operativo). Ruteo sobre la red vial de OpenStreetMap con OSRM."
)

if access.empty:
    st.warning(
        "Ningún punto de demanda cumple los filtros seleccionados. "
        "Quitar alguna restricción en la barra lateral."
    )
    st.stop()


# ====================================================================== KPIs
w = access["design_weight"]
pop_total = float(w.sum())
covered = access["t_min"].notna() & (access["t_min"] <= threshold)
pop_cov = float(w[covered].sum())
beyond = access["t_min"].isna() | (access["t_min"] > threshold)
t_med = weighted_quantile(access["t_min"], w, 0.5)
t_mean = weighted_mean(access["t_min"], w)

district_now = (
    access.groupby(["department", "district"], sort=False)
    .apply(
        lambda g: pd.Series(
            {
                "poblacion": g["design_weight"].sum(),
                "fuera": g.loc[
                    g["t_min"].isna() | (g["t_min"] > threshold), "design_weight"
                ].sum(),
                "t_medio": weighted_mean(g["t_min"], g["design_weight"]),
            }
        ),
        include_groups=False,
    )
    .reset_index()
)
worst_row = (
    district_now.sort_values("fuera", ascending=False).iloc[0]
    if not district_now.empty else None
)

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Población analizada", f"{pop_total:,.0f}".replace(",", " "))
k2.metric(
    f"Cubierta ≤ {threshold} min",
    f"{pop_cov / pop_total:.1%}",
    help=f"{pop_cov:,.0f} habitantes".replace(",", " "),
)
k3.metric(
    f"Más de {threshold} min",
    f"{float(w[beyond].sum()):,.0f}".replace(",", " "),
    delta=f"{float(w[beyond].sum()) / pop_total:.1%} de la población",
    delta_color="inverse",
)
k4.metric("Tiempo mediano", f"{t_med:.0f} min", help=f"Media ponderada: {t_mean:.1f} min")
if worst_row is not None:
    k5.metric(
        "Distrito con mayor brecha",
        str(worst_row["district"]).title(),
        delta=f"{worst_row['fuera']:,.0f} hab. fuera".replace(",", " "),
        delta_color="off",
    )

st.divider()

TABS = st.tabs(
    [
        "🗺️ Mapa",
        "📊 Distribución",
        "📋 Brechas críticas",
        "🎛️ Simulador de escenarios",
        "🧮 Capacidad (2SFCA)",
        "🧪 Calidad de datos",
    ]
)


# ====================================================================== mapa
with TABS[0]:
    import folium
    from streamlit_folium import st_folium

    st.subheader("Tiempo medio de acceso por distrito")
    st.caption(
        "El color agrupa a los distritos en las bandas de cobertura. Pasar el "
        "cursor sobre un distrito muestra su población y su tiempo medio."
    )

    geo = load_districts_geojson(scope)
    if geo is None:
        st.info("No hay polígonos distritales disponibles para este alcance.")
    else:
        by_ubigeo = (
            access.groupby("ubigeo")
            .apply(
                lambda g: pd.Series(
                    {
                        "poblacion": g["design_weight"].sum(),
                        "t_medio": weighted_mean(g["t_min"], g["design_weight"]),
                        "distrito": g["district"].iloc[0],
                        "departamento": g["department"].iloc[0],
                        "alta_varianza": bool(g["high_variance_district"].max()),
                    }
                ),
                include_groups=False,
            )
            .to_dict("index")
        )

        center = [float(access["lat"].median()), float(access["lon"].median())]
        fmap = folium.Map(
            location=center, zoom_start=5 if not sel_dep else 7,
            tiles=None, control_scale=True,
        )
        # Se declara el proveedor de mosaicos explícitamente en lugar de usar el
        # alias "CartoDB positron" de folium: ese alias resuelve a un dominio
        # heredado que ahora devuelve mosaicos con marca de agua «API key
        # required». Este endpoint es el vigente y es de uso libre con atribución.
        folium.TileLayer(
            tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
            attr=(
                '&copy; <a href="https://www.openstreetmap.org/copyright">'
                "OpenStreetMap</a> contributors &copy; "
                '<a href="https://carto.com/attributions">CARTO</a>'
            ),
            name="Mapa base",
            control=False,
            subdomains="abcd",
            max_zoom=19,
        ).add_to(fmap)

        def style_fn(feature: dict) -> dict:
            rec = by_ubigeo.get(feature["properties"]["ubigeo"])
            if rec is None or pd.isna(rec["t_medio"]):
                return {"fillColor": NODATA, "color": "white", "weight": 0.3,
                        "fillOpacity": 0.55}
            return {
                "fillColor": SEQ[band_of(rec["t_medio"])],
                "color": "white",
                "weight": 0.3,
                "fillOpacity": 0.82,
            }

        for feature in geo["features"]:
            rec = by_ubigeo.get(feature["properties"]["ubigeo"], {})
            feature["properties"]["tooltip"] = (
                f"<b>{str(rec.get('distrito', '—')).title()}</b><br>"
                f"{str(rec.get('departamento', '—')).title()}<br>"
                f"Población: {rec.get('poblacion', float('nan')):,.0f}<br>"
                f"Tiempo medio: {rec.get('t_medio', float('nan')):.0f} min"
                + ("<br><i>estimación de alta varianza</i>" if rec.get("alta_varianza") else "")
            ).replace(",", " ")

        folium.GeoJson(
            geo,
            style_function=style_fn,
            tooltip=folium.GeoJsonTooltip(fields=["tooltip"], labels=False, sticky=True),
            name="Distritos",
        ).add_to(fmap)

        # --- capa de establecimientos ---------------------------------------
        def facility_subset() -> pd.DataFrame:
            if fac_all.empty:
                return fac_all
            f = fac_all[fac_all["has_coords"].fillna(False)]
            keep = pd.Series(False, index=f.index)
            if show_resolutive:
                keep |= f["is_resolutive"].fillna(False)
            if show_candidates:
                keep |= f["is_upgrade_candidate"].fillna(False) & f["is_operational"].fillna(False)
            f = f[keep]
            if sel_cat:
                f = f[f["CATEGORIA"].isin(sel_cat)]
            if sel_inst:
                f = f[f["INSTITUCION"].isin(sel_inst)]
            if sel_dep:
                f = f[f["DEPARTAMENTO_N"].isin(sel_dep)]
            if sel_prov:
                f = f[f["PROVINCIA_N"].isin(sel_prov)]
            return f

        facs = facility_subset()
        if not facs.empty:
            layer = folium.FeatureGroup(name="Establecimientos", show=True)
            for row in facs.head(3000).itertuples():
                resolutive = bool(getattr(row, "is_resolutive", False))
                folium.CircleMarker(
                    location=[float(row.lat), float(row.lon)],
                    radius=4.0 if resolutive else 2.4,
                    color="white",
                    weight=0.8,
                    fill=True,
                    fill_color=CAT[0] if resolutive else CAT[1],
                    fill_opacity=0.95 if resolutive else 0.7,
                    tooltip=(
                        f"<b>{str(row.NOMBRE).title()}</b><br>"
                        f"Categoría {row.CATEGORIA} · {str(row.INSTITUCION).title()}<br>"
                        + ("Resolutivo" if resolutive else "Candidato a ascenso")
                        + ("<br><i>coordenada recuperada por centroide distrital</i>"
                           if getattr(row, "coord_recovered", False) else "")
                    ),
                ).add_to(layer)
            layer.add_to(fmap)
            if len(facs) > 3000:
                st.caption(
                    f"Se dibujan 3 000 de {len(facs):,} establecimientos para que el "
                    "mapa siga siendo usable; los indicadores usan todos.".replace(",", " ")
                )
        folium.LayerControl(collapsed=True).add_to(fmap)

        legend = "".join(
            f"<div style='display:flex;align-items:center;gap:6px;margin:2px 0'>"
            f"<span style='width:14px;height:14px;background:{c};display:inline-block;"
            f"border:1px solid white'></span><span>{lab}</span></div>"
            for lab, c in zip(BAND_LABELS, SEQ, strict=True)
        )
        fmap.get_root().html.add_child(
            folium.Element(
                f"<div style='position:fixed;bottom:26px;left:14px;z-index:9999;"
                f"background:#fcfcfb;padding:8px 10px;border:1px solid #e1e0d9;"
                f"border-radius:6px;font:12px system-ui;color:{INK_2}'>"
                f"<b style='color:{INK}'>Tiempo medio</b>{legend}</div>"
            )
        )
        st_folium(fmap, width=None, height=620, returned_objects=[])


# ============================================================== distribución
with TABS[1]:
    import plotly.graph_objects as go

    st.subheader("¿Cómo se reparte el tiempo de acceso?")
    split = st.radio(
        "Desagregar por",
        ["Ámbito urbano/rural", "Región natural", "Departamento"],
        horizontal=True,
    )
    key = {
        "Ámbito urbano/rural": "area_type",
        "Región natural": "natural_region",
        "Departamento": "department",
    }[split]

    groups = [g for g in access[key].dropna().unique()][:3]
    if len(access[key].dropna().unique()) > 3:
        st.caption(
            "Se grafican los tres grupos con más población: más de tres series "
            "hacen que los colores dejen de ser distinguibles para lectores con "
            "daltonismo. Los demás están en la tabla de abajo."
        )
        groups = (
            access.groupby(key)["design_weight"].sum().nlargest(3).index.tolist()
        )

    fig = go.Figure()
    for name, color in zip(groups, CAT, strict=False):
        g = access[access[key] == name].dropna(subset=["t_min"]).sort_values("t_min")
        if g.empty:
            continue
        share = g["design_weight"].cumsum() / g["design_weight"].sum() * 100
        fig.add_trace(
            go.Scatter(
                x=g["t_min"], y=share, mode="lines", name=str(name).title(),
                line={"color": color, "width": 2},
                hovertemplate="%{y:.1f}% de la población a ≤ %{x:.0f} min<extra>%{fullData.name}</extra>",
            )
        )
    for band in BANDS:
        fig.add_vline(x=band, line={"color": "#c3c2b7", "width": 1, "dash": "dash"})
    fig.update_layout(
        height=420,
        xaxis={"title": "Minutos hasta el establecimiento resolutivo más cercano",
               "range": [0, float(access["t_min"].quantile(0.99))], "gridcolor": "#e1e0d9"},
        yaxis={"title": "% de la población acumulada", "range": [0, 100],
               "gridcolor": "#e1e0d9"},
        plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb",
        font={"color": INK_2}, margin={"l": 10, "r": 10, "t": 20, "b": 10},
        legend={"orientation": "h", "y": -0.2},
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("**Cobertura por grupo** (ponderada por población)")
    rows = []
    for name, g in access.groupby(key):
        gw = g["design_weight"]
        row = {
            split: str(name).title(),
            "Población": float(gw.sum()),
            "Media (min)": weighted_mean(g["t_min"], gw),
            "Mediana (min)": weighted_quantile(g["t_min"], gw, 0.5),
            "p90 (min)": weighted_quantile(g["t_min"], gw, 0.90),
            "Gini": gini(g["t_min"], gw),
        }
        for band in BANDS:
            row[f"≤ {band} min"] = float(gw[g["t_min"] <= band].sum()) / float(gw.sum())
        rows.append(row)
    table = pd.DataFrame(rows).sort_values("Población", ascending=False)
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Población": st.column_config.NumberColumn(format="%.0f"),
            **{
                f"≤ {b} min": st.column_config.ProgressColumn(
                    f"≤ {b} min", min_value=0.0, max_value=1.0, format="%.1f%%"
                )
                for b in BANDS
            },
        },
    )


# =========================================================== brechas críticas
with TABS[2]:
    st.subheader("Distritos con mayor brecha de acceso")
    st.caption(
        f"Ordenados por población que vive a más de {threshold} minutos de un "
        "establecimiento resolutivo. Ese criterio, y no el tiempo medio, es el que "
        "ordena una decisión de inversión: un distrito de 300 habitantes a cinco "
        "horas es un problema distinto de uno de 80 000 a hora y media."
    )

    ranked = (
        access.groupby(["department", "province", "district", "ubigeo"], sort=False)
        .apply(
            lambda g: pd.Series(
                {
                    "Población": g["design_weight"].sum(),
                    "Media (min)": weighted_mean(g["t_min"], g["design_weight"]),
                    "p90 (min)": weighted_quantile(g["t_min"], g["design_weight"], 0.9),
                    f"Fuera de {threshold} min": g.loc[
                        g["t_min"].isna() | (g["t_min"] > threshold), "design_weight"
                    ].sum(),
                    "Puntos muestreados": len(g),
                    "Alta varianza": bool(g["high_variance_district"].max()),
                    "Altitud (m)": g["altitude_m"].median(),
                }
            ),
            include_groups=False,
        )
        .reset_index()
    )
    ranked["Cobertura"] = 1 - ranked[f"Fuera de {threshold} min"] / ranked["Población"]
    ranked = ranked.rename(
        columns={"department": "Departamento", "province": "Provincia",
                 "district": "Distrito", "ubigeo": "Ubigeo"}
    ).sort_values(f"Fuera de {threshold} min", ascending=False)

    hide_thin = st.checkbox(
        "Ocultar distritos con estimación de alta varianza", value=False,
        help=(
            "Distritos con menos de 3 puntos muestreados y menos del 60 % de su "
            "población en centros poblados incluidos con certeza."
        ),
    )
    view = ranked[~ranked["Alta varianza"]] if hide_thin else ranked
    top_n = st.slider("Cuántos mostrar", 10, 100, 25, step=5)

    st.dataframe(
        view.head(top_n),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Población": st.column_config.NumberColumn(format="%.0f"),
            f"Fuera de {threshold} min": st.column_config.NumberColumn(format="%.0f"),
            "Cobertura": st.column_config.ProgressColumn(
                "Cobertura", min_value=0.0, max_value=1.0, format="%.1f%%"
            ),
            "Media (min)": st.column_config.NumberColumn(format="%.0f"),
            "p90 (min)": st.column_config.NumberColumn(format="%.0f"),
            "Altitud (m)": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    st.download_button(
        "Descargar la tabla completa (CSV)",
        view.to_csv(index=False).encode("utf-8"),
        file_name=f"brechas_criticas_{scope}_{threshold}min.csv",
        mime="text/csv",
    )


# ======================================================== simulador escenarios
with TABS[3]:
    st.subheader("¿Qué pasaría si ascendemos establecimientos I-3 / I-4?")
    st.caption(
        "El cálculo usa la matriz origen–destino de la Fase 2 que está en el "
        "repositorio: no se rutea nada en vivo. Se recalcula el tiempo al "
        "establecimiento resolutivo más cercano suponiendo que los seleccionados "
        "pasan a tener capacidad resolutiva."
    )

    cand_long: pd.DataFrame = DATA["candidates"]  # type: ignore[assignment]
    mclp_seq: pd.DataFrame = DATA["mclp_sequence"]  # type: ignore[assignment]

    if cand_long.empty:
        st.info(
            "No hay tabla de candidatas (`od_candidates_*.parquet`). Ejecutar la "
            "Fase 2 para generarla."
        )
    else:
        ids_in_scope = set(access["demand_id"])
        pool = cand_long[cand_long["demand_id"].isin(ids_in_scope)]
        base_t = access.set_index("demand_id")["t_min"]
        weights = access.set_index("demand_id")["design_weight"]

        catalog = (
            fac_all[fac_all["COD_IPRESS"].isin(pool["COD_IPRESS"].unique())]
            [["COD_IPRESS", "NOMBRE", "CATEGORIA", "DEPARTAMENTO", "PROVINCIA", "DISTRITO"]]
            .drop_duplicates()
        )
        catalog["etiqueta"] = (
            catalog["NOMBRE"].str.title() + "  ·  " + catalog["CATEGORIA"]
            + "  ·  " + catalog["DISTRITO"].str.title()
            + ", " + catalog["DEPARTAMENTO"].str.title()
        )
        label_to_id = dict(zip(catalog["etiqueta"], catalog["COD_IPRESS"], strict=True))

        col_a, col_b = st.columns([3, 2])
        with col_b:
            st.markdown("**Atajo: la selección óptima**")
            st.caption(
                "Resuelto con el problema de cobertura máxima (MCLP). El algoritmo "
                "voraz garantiza al menos el 63,2 % del óptimo teórico."
            )
            # Slider y no number_input: este último exige pulsar Enter para
            # aplicar, que es fricción innecesaria en una demostración en vivo.
            max_auto = int(min(50, len(mclp_seq))) if not mclp_seq.empty else 0
            n_auto = (
                st.slider("Número de ascensos a sugerir", 0, max_auto, 0)
                if max_auto > 0
                else 0
            )
            auto_ids = (
                mclp_seq.head(int(n_auto))["COD_IPRESS"].tolist() if n_auto else []
            )
            if auto_ids:
                st.success(f"{len(auto_ids)} establecimientos precargados al costado.")

        with col_a:
            default_labels = [
                lab for lab, cid in label_to_id.items() if cid in set(auto_ids)
            ]
            picked = st.multiselect(
                "Establecimientos a ascender",
                options=sorted(label_to_id),
                default=default_labels,
                help=(
                    "Escribir para buscar por nombre, distrito o departamento. "
                    "Mover el control de la derecha reemplaza la selección por la "
                    "óptima de ese tamaño."
                ),
                # La clave depende de n_auto a propósito. Un multiselect conserva
                # su estado entre reejecuciones e ignora un `default` nuevo, así
                # que sin esto mover el control no cambiaría la selección.
                key=f"upgrade_pick_{n_auto}",
            )
        selected = [label_to_id[p] for p in picked]

        base = recompute_coverage([], base_t, pool, weights, float(threshold))
        new = recompute_coverage(selected, base_t, pool, weights, float(threshold))

        if selected and threshold > new["exact_below_min"]:
            st.warning(
                f"Con umbral de {threshold} min el resultado es aproximado: la tabla "
                f"de candidatas guarda las 10 más cercanas por punto y garantiza "
                f"exactitud hasta {new['exact_below_min']:.0f} min. Puede subestimar "
                "la cobertura ganada."
            )

        m1, m2, m3 = st.columns(3)
        m1.metric(
            f"Cobertura ≤ {threshold} min",
            f"{new['share_cubierta']:.1%}",
            delta=f"{(new['share_cubierta'] - base['share_cubierta']) * 100:+.2f} pp",
        )
        m2.metric(
            "Población que gana cobertura",
            f"{new['poblacion_cubierta'] - base['poblacion_cubierta']:,.0f}".replace(",", " "),
        )
        m3.metric(
            "Tiempo medio",
            f"{new['t_medio_min']:.1f} min",
            delta=f"{new['t_medio_min'] - base['t_medio_min']:+.1f} min",
            delta_color="inverse",
        )

        frontier: pd.DataFrame = DATA["mclp_frontier"]  # type: ignore[assignment]
        if not frontier.empty:
            import plotly.graph_objects as go

            fig = go.Figure()
            fig.add_trace(
                go.Bar(
                    x=frontier["presupuesto"].astype(str),
                    y=frontier["ganancia_vs_base"] / 1000,
                    marker_color=CAT[0], name="Óptimo (voraz)",
                    hovertemplate="%{y:,.0f} mil habitantes<extra></extra>",
                )
            )
            if selected:
                fig.add_hline(
                    y=(new["poblacion_cubierta"] - base["poblacion_cubierta"]) / 1000,
                    line={"color": CRITICAL, "width": 2, "dash": "dash"},
                    annotation_text=f"Tu selección ({len(selected)})",
                    annotation_position="top left",
                )
            fig.update_layout(
                height=330,
                xaxis_title="Número de establecimientos ascendidos",
                yaxis_title="Población ganada (miles)",
                plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb",
                font={"color": INK_2}, margin={"l": 10, "r": 10, "t": 30, "b": 10},
                yaxis={"gridcolor": "#e1e0d9"}, showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)
            st.caption(
                "La frontera es decreciente: los primeros ascensos rinden mucho más "
                "que los siguientes. Ese es el argumento para priorizar, no para "
                "repartir de forma pareja."
            )

        if not mclp_seq.empty:
            with st.expander("Ver el orden completo que recomienda el MCLP"):
                show = mclp_seq[
                    ["orden", "NOMBRE", "CATEGORIA", "DEPARTAMENTO", "DISTRITO",
                     "poblacion_marginal"]
                ].copy()
                show.columns = ["Orden", "Establecimiento", "Categoría", "Departamento",
                                "Distrito", "Población marginal"]
                st.dataframe(
                    show, use_container_width=True, hide_index=True,
                    column_config={
                        "Población marginal": st.column_config.NumberColumn(format="%.0f")
                    },
                )
                st.download_button(
                    "Descargar el orden recomendado (CSV)",
                    show.to_csv(index=False).encode("utf-8"),
                    file_name=f"mclp_orden_{scope}.csv", mime="text/csv",
                )


# =================================================================== 2SFCA
with TABS[4]:
    st.subheader("Accesibilidad con capacidad: 2SFCA")
    sfca: pd.DataFrame = DATA["sfca_district"]  # type: ignore[assignment]
    if sfca.empty:
        st.info("No hay resultados de 2SFCA. Ejecutar `python -m src.models`.")
    else:
        st.caption(
            "El tiempo al establecimiento más cercano ignora la congestión: si "
            "400 000 personas comparten el mismo hospital a 25 minutos, su acceso "
            "efectivo no es el de quien lo tiene para sí. El índice 2SFCA reparte la "
            "capacidad de cada establecimiento entre la demanda que lo alcanza. "
            "**El nivel absoluto no es interpretable** (la capacidad es un proxy "
            "normativo por categoría, porque RENIPRESS no publica dotación de camas); "
            "el ordenamiento entre territorios sí."
        )
        view = sfca.copy()
        if sel_dep:
            view = view[view["department"].isin(sel_dep)]
        if view.empty:
            st.warning("Ningún distrito con los filtros actuales.")
        else:
            import plotly.graph_objects as go

            fig = go.Figure(
                go.Scatter(
                    x=view["t_medio_min"], y=view["sfca_index_per_10k"],
                    mode="markers",
                    marker={
                        "size": np.clip(view["poblacion"] / 4000, 5, 34),
                        "color": CAT[0], "opacity": 0.55,
                        "line": {"width": 0.6, "color": "white"},
                    },
                    text=view["district"].str.title() + ", " + view["department"].str.title(),
                    hovertemplate=(
                        "<b>%{text}</b><br>Tiempo medio: %{x:.0f} min"
                        "<br>Índice 2SFCA: %{y:.2f}<extra></extra>"
                    ),
                )
            )
            fig.update_layout(
                height=420,
                xaxis_title="Tiempo medio de acceso (min)",
                yaxis_title="Índice 2SFCA por 10 000 habitantes",
                plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb",
                font={"color": INK_2}, margin={"l": 10, "r": 10, "t": 20, "b": 10},
                xaxis={"gridcolor": "#e1e0d9"}, yaxis={"gridcolor": "#e1e0d9"},
            )
            st.plotly_chart(fig, use_container_width=True)
            st.caption(
                "Los distritos abajo a la izquierda son los interesantes: están "
                "cerca de un establecimiento, pero de uno saturado. Un análisis "
                "basado solo en tiempo los daría por bien atendidos."
            )
            st.dataframe(
                view.nsmallest(20, "sfca_index_per_10k")[
                    ["department", "province", "district", "poblacion",
                     "t_medio_min", "sfca_index_per_10k"]
                ].rename(columns={
                    "department": "Departamento", "province": "Provincia",
                    "district": "Distrito", "poblacion": "Población",
                    "t_medio_min": "Media (min)", "sfca_index_per_10k": "2SFCA /10k",
                }),
                use_container_width=True, hide_index=True,
                column_config={
                    "Población": st.column_config.NumberColumn(format="%.0f"),
                    "Media (min)": st.column_config.NumberColumn(format="%.0f"),
                    "2SFCA /10k": st.column_config.NumberColumn(format="%.3f"),
                },
            )


# ========================================================== calidad de datos
with TABS[5]:
    st.subheader("Calidad de los datos de origen")
    quality: dict = DATA["quality"]  # type: ignore[assignment]
    if not quality:
        st.info("No hay reporte de calidad. Ejecutar `python -m src.validation`.")
    else:
        counters = quality.get("counters", {})
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("IPRESS en RENIPRESS", f"{counters.get('renipress_rows_raw', 0):,}".replace(",", " "))
        c2.metric(
            "Resolutivas utilizables",
            f"{counters.get('facilities_resolutive', 0):,}".replace(",", " "),
            delta=f"de {counters.get('facilities_resolutive_by_category', 0)} por categoría",
            delta_color="off",
        )
        c3.metric("Centros poblados válidos", f"{counters.get('ccpp_valid', 0):,}".replace(",", " "))
        c4.metric(
            "Coordenadas recuperadas",
            f"{counters.get('facilities_resolutive_on_recovered_coords', 0):,}".replace(",", " "),
            help=(
                "Establecimientos resolutivos cuya ubicación se imputó con el "
                "centroide de su distrito porque RENIPRESS no la publica."
            ),
        )

        decisions = pd.DataFrame(quality.get("decisions", []))
        if not decisions.empty:
            st.markdown("**Verificaciones aplicadas y qué se hizo con cada hallazgo**")
            show = decisions[decisions["n_affected"] > 0][
                ["dataset", "check", "n_affected", "share_affected", "action", "justification"]
            ].rename(columns={
                "dataset": "Fuente", "check": "Verificación", "n_affected": "Registros",
                "share_affected": "% del total", "action": "Acción",
                "justification": "Por qué",
            })
            st.dataframe(
                show, use_container_width=True, hide_index=True,
                column_config={
                    "Registros": st.column_config.NumberColumn(format="%d"),
                    "% del total": st.column_config.NumberColumn(format="%.2f%%"),
                    "Por qué": st.column_config.TextColumn(width="large"),
                },
            )

        st.divider()
        c_left, c_right = st.columns(2)
        snap: pd.DataFrame = DATA["snapping"]  # type: ignore[assignment]
        with c_left:
            st.markdown("**Enganche a la red vial (*snapping*)**")
            if snap.empty:
                st.caption("Sin datos de snapping.")
            else:
                st.dataframe(snap, use_container_width=True, hide_index=True)
                st.caption(
                    "Distancia entre la coordenada declarada y el punto de la red al "
                    "que se ancló. Un enganche largo indica que el punto se rutea "
                    "desde una vía que no es la suya."
                )
        straight: pd.DataFrame = DATA["straight"]  # type: ignore[assignment]
        with c_right:
            st.markdown("**Línea recta frente a red vial**")
            if straight.empty:
                st.caption("Sin comparación disponible.")
            else:
                st.dataframe(straight, use_container_width=True, hide_index=True)
                st.caption(
                    "Por qué no basta con la distancia en línea recta: cambia qué "
                    "establecimiento es el más cercano y declara cubierta a población "
                    "que no lo está."
                )

        st.divider()
        st.markdown("**Trazabilidad de las fuentes**")
        manifest = Path("data/raw/manifest.json")
        if manifest.exists():
            man = json.loads(manifest.read_text(encoding="utf-8"))
            st.dataframe(
                pd.DataFrame(man["sources"])[
                    ["key", "filename", "bytes", "sha256", "license", "url"]
                ],
                use_container_width=True, hide_index=True,
                column_config={"bytes": st.column_config.NumberColumn(format="%d")},
            )


st.divider()
st.caption(
    "Fuentes: RENIPRESS/SUSALUD, listado de centros poblados MTC/INEI, límites "
    "administrativos OCHA-HDX (base INEI), red vial OpenStreetMap (ODbL). "
    "Todos los agregados están ponderados por la población que representa cada "
    "punto de demanda. Metodología completa en `report/main.pdf` y `config.md`."
)
