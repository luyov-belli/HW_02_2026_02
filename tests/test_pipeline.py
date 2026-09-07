"""Pruebas de las piezas que, si se rompen, lo hacen en silencio.

No se prueban las descargas ni el ruteo (dependen de la red y de un motor
externo). Se prueba lo que produce números: los estimadores ponderados, el
reparto de cupos del muestreo, la detección de coordenadas invertidas y las
conversiones que históricamente fallaron sin avisar, como los ceros a la
izquierda de los ubigeo.

Ejecutar::

    pytest -q
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

from src.config import CFG
from src.metrics import gini, lorenz_curve, weighted_mean, weighted_quantile
from src.models import recompute_coverage
from src.routing import haversine_matrix
from src.utils import (
    coerce_id_columns,
    lossy_ascii_score,
    mojibake_score,
    safe_row_idxmin,
)
from src.validation import (
    _allocate_quota,
    _resolve_ccpp_coord_columns,
    in_bbox,
    norm_name,
    to_numeric_coord,
)
from src.utils import DecisionLog


# --------------------------------------------------------- estimadores ponderados
def test_weighted_mean_ignora_pesos_nulos_y_nan():
    v = pd.Series([10.0, 20.0, np.nan, 30.0])
    w = pd.Series([1.0, 3.0, 100.0, 0.0])
    # Solo cuentan 10 (peso 1) y 20 (peso 3): (10 + 60) / 4 = 17.5
    assert weighted_mean(v, w) == pytest.approx(17.5)


def test_weighted_mean_equivale_al_simple_con_pesos_iguales():
    v = pd.Series([1.0, 2.0, 3.0, 4.0])
    w = pd.Series([2.0, 2.0, 2.0, 2.0])
    assert weighted_mean(v, w) == pytest.approx(v.mean())


def test_weighted_quantile_respeta_el_peso():
    # 99 personas a 10 minutos y 1 a 1000: la mediana poblacional es 10.
    v = pd.Series([10.0, 1000.0])
    w = pd.Series([99.0, 1.0])
    assert weighted_quantile(v, w, 0.5) == pytest.approx(10.0, abs=1e-6)
    assert weighted_quantile(v, w, 0.999) > 10.0


def test_gini_es_cero_si_todos_tardan_lo_mismo():
    v = pd.Series([42.0] * 50)
    w = pd.Series(np.random.default_rng(0).uniform(1, 100, 50))
    assert gini(v, w) == pytest.approx(0.0, abs=1e-9)


def test_gini_es_invariante_a_la_escala():
    rng = np.random.default_rng(1)
    v = pd.Series(rng.gamma(2.0, 20.0, 500))
    w = pd.Series(rng.uniform(1, 1000, 500))
    assert gini(v, w) == pytest.approx(gini(v * 7.5, w), abs=1e-9)


def test_gini_crece_con_la_concentracion():
    w = pd.Series([1.0] * 100)
    parejo = pd.Series([50.0] * 100)
    concentrado = pd.Series([1.0] * 99 + [10_000.0])
    assert gini(parejo, w) < gini(concentrado, w)
    assert 0.0 <= gini(concentrado, w) <= 1.0


def test_lorenz_va_de_cero_a_uno_y_queda_bajo_la_diagonal():
    rng = np.random.default_rng(2)
    v = pd.Series(rng.gamma(2.0, 20.0, 300))
    w = pd.Series(rng.uniform(1, 500, 300))
    lz = lorenz_curve(v, w)
    assert lz["share_poblacion"].iloc[0] == pytest.approx(0.0)
    assert lz["share_poblacion"].iloc[-1] == pytest.approx(1.0)
    assert lz["share_tiempo_acumulado"].iloc[-1] == pytest.approx(1.0)
    # Ordenada de menor a mayor tiempo, la curva nunca supera la diagonal.
    assert (lz["share_tiempo_acumulado"] <= lz["share_poblacion"] + 1e-9).all()


# ------------------------------------------------------- reparto de cupos (muestreo)
def _quota_case(pop, sizes, slots, floor=1):
    return _allocate_quota(
        pd.Series(pop, dtype=float), pd.Series(sizes, dtype=int), slots, floor=floor
    )


def test_cupos_suman_exactamente_el_presupuesto():
    q = _quota_case({"a": 1000, "b": 500, "c": 10}, {"a": 50, "b": 50, "c": 50}, 100)
    assert int(q.sum()) == 100


def test_cupos_nunca_exceden_el_tamano_del_estrato():
    sizes = {"a": 3, "b": 2, "c": 1}
    q = _quota_case({"a": 1000, "b": 900, "c": 800}, sizes, 100)
    assert (q <= pd.Series(sizes)).all()
    # No se pueden asignar más de los 6 centros poblados que existen.
    assert int(q.sum()) == 6


def test_el_piso_da_representacion_a_todo_estrato_que_quepa():
    q = _quota_case(
        {f"e{i}": 100 - i for i in range(10)}, {f"e{i}": 5 for i in range(10)}, 10
    )
    assert (q >= 1).all()


def test_el_piso_prioriza_los_estratos_mas_poblados_si_no_alcanza():
    q = _quota_case(
        {"grande": 10_000, "mediano": 100, "chico": 1},
        {"grande": 5, "mediano": 5, "chico": 5},
        2,
    )
    assert int(q.sum()) == 2
    assert q["chico"] == 0


def test_el_reparto_es_monotono_en_la_poblacion():
    q = _quota_case(
        {"a": 10_000, "b": 1_000, "c": 100},
        {"a": 100, "b": 100, "c": 100},
        60,
    )
    assert q["a"] >= q["b"] >= q["c"]


# ------------------------------------------------------------------ coordenadas
def test_in_bbox_acepta_el_peru_y_rechaza_el_indico():
    lon = pd.Series([-77.0428, -6.4754])
    lat = pd.Series([-12.0464, -77.8306])
    assert bool(in_bbox(lon, lat).iloc[0])
    assert not bool(in_bbox(lon, lat).iloc[1])


def test_to_numeric_coord_tolera_coma_decimal_y_espacios():
    s = pd.Series(["-77,0428", "  -12.0464 ", "", "n/d"])
    out = to_numeric_coord(s)
    assert out.iloc[0] == pytest.approx(-77.0428)
    assert out.iloc[1] == pytest.approx(-12.0464)
    assert out.iloc[2:].isna().all()


def test_detecta_el_encabezado_de_coordenadas_invertido():
    """Reproduce el defecto real del listado de centros poblados del MTC.

    La columna rotulada como latitud contiene la longitud. La detección no debe
    fiarse de la etiqueta sino del rango de valores.
    """
    df = pd.DataFrame(
        {
            "Latitud (coord X)": ["-77.8306", "-71.9800", "-80.6300"],
            "Longitud (coord Y)": ["-6.4754", "-13.5000", "-3.7500"],
        }
    )
    lat_col, lon_col = _resolve_ccpp_coord_columns(df, DecisionLog(stage="test"))
    assert lat_col == "Longitud (coord Y)"
    assert lon_col == "Latitud (coord X)"


def test_respeta_un_encabezado_correcto():
    df = pd.DataFrame(
        {
            "Latitud": ["-6.4754", "-13.5000", "-3.7500"],
            "Longitud": ["-77.8306", "-71.9800", "-80.6300"],
        }
    )
    lat_col, lon_col = _resolve_ccpp_coord_columns(df, DecisionLog(stage="test"))
    assert lat_col == "Latitud"
    assert lon_col == "Longitud"


def test_haversine_reproduce_una_distancia_conocida():
    # Lima (Plaza Mayor) a Cusco: ~572 km en línea recta. Ojo, no confundir con
    # la distancia por carretera, que ronda los 1 100 km: esa brecha es
    # justamente el fenómeno que mide el índice de desvío del informe.
    origen = np.array([[-77.0300, -12.0450]])
    destino = np.array([[-71.9780, -13.5170]])
    km = haversine_matrix(origen, destino)[0, 0]
    assert km == pytest.approx(572, rel=0.01)


def test_haversine_reproduce_un_grado_de_latitud():
    # Un grado de latitud son ~111,2 km en cualquier meridiano.
    a = np.array([[-77.0, -12.0]])
    b = np.array([[-77.0, -13.0]])
    assert haversine_matrix(a, b)[0, 0] == pytest.approx(111.2, rel=0.005)


def test_haversine_es_cero_en_el_mismo_punto():
    p = np.array([[-77.0300, -12.0450]])
    assert haversine_matrix(p, p)[0, 0] == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------ identificadores y texto
def test_los_ubigeo_conservan_los_ceros_a_la_izquierda():
    """Fue un fallo silencioso real: 010109 volvía como 10109 y los cruces fallaban."""
    df = pd.DataFrame({"ubigeo": [10109, 150101, 1101], "COD_IPRESS": [2806, 12, 0]})
    out = coerce_id_columns(df.copy())
    assert list(out["ubigeo"]) == ["010109", "150101", "001101"]
    assert list(out["COD_IPRESS"]) == ["00002806", "00000012", "00000000"]


def test_detecta_perdida_de_caracteres_no_ascii():
    s = pd.Series(["SE?OR DE LUREN", "I?APARI", "HOSPITAL REGIONAL", "¿ES ASI?"])
    # Los dos primeros perdieron la eñe; el último tiene interrogación legítima.
    assert lossy_ascii_score(s) == 2


def test_idxmin_tolera_filas_sin_ningun_valor():
    """Regresión: la corrida nacional de OSRM murió aquí tras 27 min de ruteo.

    ``DataFrame.idxmin(axis=1)`` lanza ValueError desde pandas 2.1 cuando una fila
    es toda NaN, y eso ocurre legítimamente: los orígenes sin acceso vial y los
    que no alcanzan ningún establecimiento a pie tienen la fila vacía.
    """
    mat = pd.DataFrame(
        [[30.0, 12.0], [np.nan, np.nan], [np.nan, 5.0]],
        index=["a", "b", "c"],
        columns=["IPRESS_1", "IPRESS_2"],
    )
    with pytest.raises(ValueError):
        mat.idxmin(axis=1)

    out = safe_row_idxmin(mat)
    assert out["a"] == "IPRESS_2"
    assert pd.isna(out["b"])
    assert out["c"] == "IPRESS_2"


def test_idxmin_seguro_devuelve_todo_na_si_la_matriz_esta_vacia_de_valores():
    mat = pd.DataFrame(np.nan, index=["a", "b"], columns=["x", "y"])
    assert safe_row_idxmin(mat).isna().all()


def test_detecta_doble_codificacion():
    assert mojibake_score(pd.Series(["CAÃ‘AVERAL", "NORMAL"])) == 1


def test_norm_name_quita_tildes_y_normaliza_espacios():
    s = pd.Series(["  Ñahuinpuquio ", "SAN  JOSÉ", None])
    out = norm_name(s)
    assert list(out) == ["NAHUINPUQUIO", "SAN JOSE", ""]


# ------------------------------------------------- simulador de escenarios (MCLP)
def _sim_fixture():
    base = pd.Series([90.0, 120.0, 20.0, np.nan], index=["p1", "p2", "p3", "p4"])
    weights = pd.Series([100.0, 200.0, 300.0, 50.0], index=base.index)
    cand = pd.DataFrame(
        {
            "demand_id": ["p1", "p1", "p2", "p4"],
            "COD_IPRESS": ["A", "B", "A", "B"],
            "minutes": [40.0, 80.0, 25.0, 15.0],
        }
    )
    return base, weights, cand


def test_ascender_no_puede_empeorar_la_cobertura():
    base, w, cand = _sim_fixture()
    sin = recompute_coverage([], base, cand, w, 60.0)
    con = recompute_coverage(["A"], base, cand, w, 60.0)
    assert con["poblacion_cubierta"] >= sin["poblacion_cubierta"]
    assert con["t_medio_min"] <= sin["t_medio_min"]


def test_la_cobertura_es_monotona_al_agregar_establecimientos():
    base, w, cand = _sim_fixture()
    uno = recompute_coverage(["A"], base, cand, w, 60.0)
    dos = recompute_coverage(["A", "B"], base, cand, w, 60.0)
    assert dos["poblacion_cubierta"] >= uno["poblacion_cubierta"]


def test_un_punto_sin_ruta_se_cubre_si_la_candidata_lo_alcanza():
    base, w, cand = _sim_fixture()
    # p4 no tiene ruta a ningún resolutivo, pero está a 15 min de la candidata B.
    con_b = recompute_coverage(["B"], base, cand, w, 60.0)
    assert con_b["poblacion_cubierta"] >= 300.0 + 50.0


def test_ascender_una_candidata_lejana_no_cambia_nada():
    base, w, cand = _sim_fixture()
    sin = recompute_coverage([], base, cand, w, 30.0)
    # A está a 40 min de p1 y 25 de p2: con umbral 30 solo ayuda a p2.
    con = recompute_coverage(["A"], base, cand, w, 30.0)
    assert con["poblacion_cubierta"] - sin["poblacion_cubierta"] == pytest.approx(200.0)


# ----------------------------------------------------------------------- informe
def _tex() -> str:
    from src.config import PROJECT_ROOT

    return (PROJECT_ROOT / "report" / "main.tex").read_text(encoding="utf-8")


def _rendered_words(tex: str) -> int:
    """Cuenta palabras como quedarán en el PDF, no como están en la fuente.

    Cada macro de cifra (``\\kpiX{}``) y cada ``\\num{...}`` se renderiza como un
    número, es decir una sola palabra; el resto de comandos y llaves desaparece.
    """
    body = re.sub(r"%.*", "", tex)
    body = re.sub(r"\\(?:kpi[A-Za-z]+|num)\s*\{[^}]*\}", " 0 ", body)
    body = re.sub(r"\\(?:kpi[A-Za-z]+)", " 0 ", body)
    body = re.sub(r"\\[a-zA-Z]+\*?", " ", body)
    body = re.sub(r"[{}\\~,]", " ", body)
    return len(body.split())


def test_el_resumen_no_excede_150_palabras():
    """El enunciado lo exige explícitamente y se califica."""
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", _tex(), re.S)
    assert m, "el informe debe tener un resumen"
    n = _rendered_words(m.group(1))
    assert n < 150, f"el resumen tiene {n} palabras renderizadas"


def test_no_hay_referencias_colgadas_en_el_informe():
    """Todo ``\\ref`` debe apuntar a una etiqueta que exista.

    Fue un fallo real: el texto citaba las tablas de calidad de datos y de
    snapping, pero ninguna de las dos se insertaba, así que el PDF mostraba «??».
    Las etiquetas de tabla viven en los .tex generados por src/export.py, de modo
    que la comprobación mira también ahí.
    """
    from src.config import PROJECT_ROOT

    tex = _tex()
    labels = set(re.findall(r"\\label\{([^}]+)\}", tex))
    tables_dir = PROJECT_ROOT / "report" / "tables"
    for path in tables_dir.glob("*.tex"):
        labels |= set(
            re.findall(r"\\label\{([^}]+)\}", path.read_text(encoding="utf-8"))
        )
    # Solo se exige la etiqueta de las tablas que el informe realmente inserta.
    insertadas = set(re.findall(r"\\inputtable\{([^}]+)\}", tex))
    referidas = set(re.findall(r"\\ref\{([^}]+)\}", tex))

    faltantes = sorted(
        r
        for r in referidas
        if r not in labels
        and not any(t.replace("tab_", "").replace("_", "-") in r for t in insertadas)
    )
    assert not faltantes, f"referencias sin etiqueta: {faltantes}"


def test_toda_figura_generada_se_usa_en_el_informe():
    """Una figura que el pipeline produce y nadie muestra es trabajo desperdiciado."""
    from src.config import PROJECT_ROOT

    tex = _tex()
    figs = sorted(p.stem for p in (PROJECT_ROOT / "report" / "figures").glob("*.pdf"))
    sin_usar = [f for f in figs if f not in tex]
    assert not sin_usar, f"figuras generadas pero no incluidas: {sin_usar}"


#: T1 + inputenc cubre en la práctica Latin-1 más unos pocos signos tipográficos.
_TEX_SEGUROS = set("—–‘’“”…")


def _caracteres_no_compilables(texto: str) -> set[str]:
    return {c for c in texto if ord(c) > 0xFF and c not in _TEX_SEGUROS}


def test_las_tablas_generadas_no_traen_caracteres_que_rompan_latex():
    """Regresión: «≤» (U+2264) en tres encabezados abortaba pdflatex.

    ``inputenc`` con T1 no puede representar U+2264 y detiene la compilación con
    "Unicode character ≤ not set up for use with LaTeX". En las figuras el mismo
    símbolo es correcto, porque matplotlib no tiene esa limitación; el error solo
    aparece al pasar por LaTeX, y el log de pdflatex no es público en CI.
    """
    from src.config import PROJECT_ROOT

    culpables: dict[str, set[str]] = {}
    for path in (PROJECT_ROOT / "report" / "tables").glob("*.tex"):
        malos = _caracteres_no_compilables(path.read_text(encoding="utf-8"))
        if malos:
            culpables[path.name] = malos
    malos_tex = _caracteres_no_compilables(_tex())
    if malos_tex:
        culpables["main.tex"] = malos_tex
    assert not culpables, f"caracteres no representables en T1: {culpables}"


def test_los_encabezados_de_banda_son_texto_plano():
    """El encabezado no debe llevar LaTeX: df.to_latex(escape=True) lo escaparía."""
    from src.export import _header_hasta

    h = _header_hasta(60)
    assert "≤" not in h
    assert "\\" not in h
    assert "%" in h and "60" in h


def test_las_corridas_de_prueba_no_escriben_en_los_artefactos_oficiales():
    """Regresión: pytest dejaba líneas dentro de logs/validation.log, un entregable.

    Los directorios con nombres de archivo fijos (logs, salidas, figuras, tablas)
    tienen que desviarse a un sufijo cuando la corrida no es el estudio. Los que
    ya llevan el alcance en el nombre del archivo no deben desviarse, porque el
    dashboard los busca por ese nombre.
    """
    # create=False: una prueba no debe dejar directorios detrás, y el paso de
    # commit de CI hace `git add -f report`, que ignoraría el .gitignore.
    assert CFG.artifact_suffix == "_test"
    for key in ("logs", "outputs", "figures", "tables"):
        assert CFG.path(key, create=False).name.endswith("_test"), key
    for key in ("processed", "raw"):
        assert not CFG.path(key, create=False).name.endswith("_test"), key


# ----------------------------------------------------------------- configuración
def test_config_declara_todo_lo_que_el_codigo_lee():
    """Contrato entre config.md y el código: si falta una clave, falla aquí."""
    requeridas = [
        ("scope", "mode"),
        ("facilities", "resolutive_categories"),
        ("facilities", "upgrade_candidate_categories"),
        ("demand", "max_demand_points"),
        ("validation", "lon_min"),
        ("routing", "engine"),
        ("routing", "primary_profile"),
        ("routing", "osrm_max_coords_per_request"),
        ("routing", "fallback", "no_road_access_threshold_m"),
        ("metrics", "time_bands_min"),
        ("metrics", "naive_haversine_speed_kmh"),
        ("metrics", "cross_variables"),
        ("innovation", "mclp_coverage_minutes"),
        ("innovation", "sfca_catchment_minutes"),
        ("paths", "outputs"),
    ]
    faltantes = [k for k in requeridas if CFG.get(*k) is None]
    assert not faltantes, f"claves ausentes en config.md: {faltantes}"


def test_las_categorias_resolutivas_no_se_solapan_con_las_candidatas():
    res = set(CFG["facilities"]["resolutive_categories"])
    cand = set(CFG["facilities"]["upgrade_candidate_categories"])
    assert not (res & cand), "una categoría no puede ser resolutiva y candidata a la vez"


def test_las_bandas_de_cobertura_estan_ordenadas():
    bands = CFG["metrics"]["time_bands_min"]
    assert bands == sorted(bands)
    assert len(bands) >= 2


def test_el_modo_de_alcance_es_uno_de_los_soportados():
    assert CFG.mode in {"national", "regions", "dev"}
    assert len(CFG.departments()) >= 1


def test_toda_region_natural_declarada_cubre_los_departamentos_del_alcance():
    sin_region = [
        d for d in CFG.departments() if CFG.region_of_department(d) == "no_determinada"
    ]
    assert not sin_region, f"departamentos sin región natural: {sin_region}"


def test_la_velocidad_de_imputacion_no_se_confunde_con_la_del_analisis_naif():
    """Son dos parámetros distintos y confundirlos invirtió el signo del desvío."""
    fallback = CFG.get("routing", "fallback", "straight_line_speed_kmh")
    naive = CFG.get("metrics", "naive_haversine_speed_kmh")
    assert fallback != naive
