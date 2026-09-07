"""Innovación — 2SFCA y optimización de ubicación (MCLP).

Dos modelos que se apoyan en la matriz OD ya calculada, de modo que su costo
marginal de cómputo es casi nulo:

* :func:`two_step_floating_catchment` — **2SFCA**. El tiempo al establecimiento
  más cercano ignora la congestión: si 400 000 personas están a 25 minutos del
  mismo hospital, su acceso *efectivo* no es el de quien lo tiene para sí. El
  2SFCA divide la capacidad de cada establecimiento entre la demanda que lo tiene
  dentro de su radio de captación, y luego suma esas razones sobre los
  establecimientos alcanzables desde cada punto.

* :func:`mclp_greedy` — **Maximal Covering Location Problem**. La Fase 4 exige un
  simulador donde el usuario elija establecimientos I-3/I-4 para "ascender" a
  resolutivos. El MCLP responde la pregunta que sigue: *cuál* es la mejor
  selección de tamaño B. Se resuelve con el algoritmo voraz clásico, que para
  cobertura máxima tiene garantía ``greedy ≥ (1 − 1/e)·OPT ≈ 0,632·OPT``; además
  se calcula una cota superior de OPT para acotar la brecha por arriba.

Ambos comparten :func:`recompute_coverage`, que es también la función que usa el
dashboard, de modo que el simulador interactivo y el optimizador no pueden
divergir.
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from .config import CFG
from .utils import DecisionLog, get_logger, read_output_csv

LOG = get_logger("models")


# ------------------------------------------------------------------ insumos
def load_model_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """``(access, od_matrix, candidates_long, facilities)`` para los modelos."""
    proc, out = CFG.path("processed"), CFG.path("outputs")
    profile = CFG["routing"]["primary_profile"]

    access = read_output_csv(out / f"access_points_{CFG.mode}.csv")
    od = pd.read_parquet(
        out / f"od_{profile}_matrix_{CFG.mode}.parquet"
    ).set_index("demand_id")
    cand_path = out / f"od_candidates_{CFG.mode}_{profile}.parquet"
    cand = pd.read_parquet(cand_path) if cand_path.exists() else pd.DataFrame()
    fac = pd.read_parquet(proc / CFG.scoped_name("facilities", "parquet"))
    return access, od, cand, fac


# --------------------------------------------------------------------- 2SFCA
def two_step_floating_catchment(
    access: pd.DataFrame,
    od: pd.DataFrame,
    facilities: pd.DataFrame,
    dlog: DecisionLog,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """2SFCA sobre la matriz OD. Devuelve ``(por_punto, por_distrito)``.

    Capacidad ``S_j``: proxy normativo por categoría declarado en
    ``[facilities.capacity_proxy]`` (camas de referencia de la NT 021-MINSA).
    Se usa un proxy y no camas reales porque RENIPRESS no publica dotación; la
    consecuencia es que el nivel absoluto del índice no es interpretable, pero su
    **ordenamiento entre territorios sí**, que es lo que se reporta.

    Ponderación ``W(t)``: escalón o gaussiana, según ``[innovation].sfca_decay``.
    La gaussiana evita el salto artificial en el borde del radio de captación
    (un punto a 59 minutos no puede valer el doble que uno a 61).
    """
    icfg = CFG["innovation"]
    d0 = float(icfg["sfca_catchment_minutes"])
    decay = icfg["sfca_decay"]
    caps = CFG["facilities"]["capacity_proxy"]

    order = access.set_index("demand_id").reindex(od.index)
    pop = order["design_weight"].to_numpy(dtype=float)
    t = od.to_numpy(dtype=float)

    if decay == "gaussian":
        # sigma tal que W(d0) ≈ 0,05: el borde del radio pesa 5%, no 0 ni 1.
        sigma = d0 / np.sqrt(2.0 * np.log(20.0))
        w = np.exp(-0.5 * (t / sigma) ** 2)
        w[~np.isfinite(t)] = 0.0
        w[t > d0] = 0.0
    else:
        w = np.where(np.isfinite(t) & (t <= d0), 1.0, 0.0)

    supply = (
        facilities.set_index("COD_IPRESS")["CATEGORIA"]
        .reindex(od.columns)
        .map(caps)
        .astype(float)
        .to_numpy()
    )
    supply = np.nan_to_num(supply, nan=float(np.nanmedian(list(caps.values()))))

    # Paso 1: razón oferta/demanda de cada establecimiento.
    demand_j = (pop[:, None] * w).sum(axis=0)
    ratio_j = np.divide(
        supply, demand_j, out=np.zeros_like(supply), where=demand_j > 0
    )

    # Paso 2: índice de accesibilidad de cada punto de demanda.
    a_i = (w * ratio_j[None, :]).sum(axis=1)

    per_point = pd.DataFrame(
        {
            "demand_id": od.index,
            "sfca_index": a_i,
            "sfca_index_per_10k": a_i * 10_000.0,
            "n_facilities_in_catchment": (w > 0).sum(axis=1),
        }
    ).merge(
        access[
            ["demand_id", "ubigeo", "department", "province", "district",
             "area_type", "natural_region", "design_weight", "t_min"]
        ],
        on="demand_id", how="left",
    )

    grp = per_point.groupby(["department", "province", "district", "ubigeo"], sort=True)
    per_district = grp.apply(
        lambda g: pd.Series(
            {
                "poblacion": g["design_weight"].sum(),
                "sfca_index_per_10k": np.average(
                    g["sfca_index_per_10k"], weights=g["design_weight"]
                ),
                "t_medio_min": np.average(
                    g["t_min"].fillna(g["t_min"].max()), weights=g["design_weight"]
                ),
                "n_facilities_in_catchment": g["n_facilities_in_catchment"].median(),
            }
        ),
        include_groups=False,
    ).reset_index()

    # Cuánto reordena el 2SFCA respecto de la métrica de tiempo simple.
    both = per_district.dropna(subset=["sfca_index_per_10k", "t_medio_min"])
    rho = float(both["sfca_index_per_10k"].corr(both["t_medio_min"], method="spearman"))
    zero_cover = int((per_district["n_facilities_in_catchment"] == 0).sum())
    dlog.record(
        check="2SFCA (accesibilidad con capacidad)",
        dataset="sfca",
        n_affected=zero_cover,
        n_total=len(per_district),
        action=(
            f"radio de captación {d0:.0f} min, decaimiento '{decay}'; "
            f"{zero_cover} distritos sin ningún resolutivo dentro del radio"
        ),
        justification=(
            "el tiempo al más cercano ignora la congestión. La correlación de "
            f"Spearman entre el índice 2SFCA y el tiempo medio es {rho:.2f}: si "
            "fuera −1 el 2SFCA no aportaría nada; la diferencia es la que revela "
            "distritos cercanos a un hospital saturado. El nivel absoluto del "
            "índice no es interpretable porque la capacidad es un proxy normativo, "
            "pero el ordenamiento sí"
        ),
        spearman_sfca_vs_tiempo=round(rho, 4),
    )
    return per_point, per_district


# ----------------------------------------------------------------------- MCLP
def recompute_coverage(
    selected: list[str] | set[str],
    base_t_min: pd.Series,
    candidates_long: pd.DataFrame,
    weights: pd.Series,
    threshold_min: float,
) -> dict[str, float]:
    """Cobertura resultante si ``selected`` pasaran a ser resolutivos.

    Es la misma función que usa el simulador de escenarios del dashboard, para
    que la exploración interactiva y el optimizador no puedan dar números
    distintos.

    **Alcance de la aproximación.** ``candidates_long`` guarda solo las k
    candidatas más cercanas de cada punto de demanda. El mínimo resultante es
    exacto para cualquier ``threshold_min`` menor o igual al tiempo de la k-ésima
    candidata de ese punto; más allá, una candidata lejana podría cubrir un punto
    y no estar registrada. La función devuelve ``exact_below_min`` para que quien
    la use sepa hasta qué umbral el resultado es exacto.
    """
    sel = set(selected)
    new_t = base_t_min.copy()
    exact_below = float("inf")

    if sel and not candidates_long.empty:
        hit = candidates_long[candidates_long["COD_IPRESS"].isin(sel)]
        if not hit.empty:
            best = hit.groupby("demand_id")["minutes"].min().reindex(new_t.index)
            new_t = pd.Series(
                np.fmin(new_t.to_numpy(dtype=float), best.to_numpy(dtype=float)),
                index=new_t.index,
            )
        exact_below = float(candidates_long.groupby("demand_id")["minutes"].max().min())

    covered = new_t <= threshold_min
    total = float(weights.sum())
    return {
        "poblacion_cubierta": float(weights[covered.fillna(False)].sum()),
        "share_cubierta": float(weights[covered.fillna(False)].sum()) / total,
        "t_medio_min": float(
            np.average(new_t.dropna(), weights=weights.reindex(new_t.dropna().index))
        ),
        "n_seleccionados": len(sel),
        "exact_below_min": exact_below,
    }


def mclp_greedy(
    access: pd.DataFrame,
    base_t_min: pd.Series,
    candidates_long: pd.DataFrame,
    dlog: DecisionLog,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """MCLP voraz: qué I-3/I-4 ascender para maximizar población cubierta.

    Devuelve ``(frontera, secuencia)``: la primera tiene una fila por presupuesto
    de ``[innovation].mclp_upgrade_budget``, la segunda el orden en que el
    algoritmo eligió los establecimientos y cuánta población marginal aportó cada
    uno (que es la información útil para una dirección regional: el décimo
    ascenso aporta mucho menos que el primero).
    """
    icfg = CFG["innovation"]
    thr = float(icfg["mclp_coverage_minutes"])
    budgets = sorted(int(b) for b in icfg["mclp_upgrade_budget"])
    if candidates_long.empty:
        LOG.warning("sin candidatas: no se ejecuta el MCLP")
        return pd.DataFrame(), pd.DataFrame()

    weights = access.set_index("demand_id")["design_weight"].reindex(base_t_min.index)
    total_pop = float(weights.sum())

    # Solo importan los puntos hoy descubiertos y las coberturas dentro del umbral.
    uncovered = base_t_min.isna() | (base_t_min > thr)
    pool = candidates_long[
        (candidates_long["minutes"] <= thr)
        & (candidates_long["demand_id"].isin(base_t_min.index[uncovered]))
    ]
    LOG.info(
        "MCLP: %d puntos descubiertos (%.1f%% de la población), %d candidatas útiles",
        int(uncovered.sum()), 100 * float(weights[uncovered].sum()) / total_pop,
        pool["COD_IPRESS"].nunique(),
    )

    cover: dict[str, set[str]] = {
        cid: set(g["demand_id"]) for cid, g in pool.groupby("COD_IPRESS")
    }
    point_pop = weights.to_dict()

    base_covered = float(weights[~uncovered].sum())
    remaining = set(base_t_min.index[uncovered])
    chosen: list[dict[str, object]] = []
    max_budget = max(budgets)

    # Cota superior de OPT: suma de las B mejores coberturas individuales.
    individual = sorted(
        (sum(point_pop.get(p, 0.0) for p in pts) for pts in cover.values()),
        reverse=True,
    )

    for step in range(1, max_budget + 1):
        best_id, best_gain, best_set = None, 0.0, set()
        for cid, pts in cover.items():
            gain_set = pts & remaining
            if not gain_set:
                continue
            gain = sum(point_pop.get(p, 0.0) for p in gain_set)
            if gain > best_gain:
                best_id, best_gain, best_set = cid, gain, gain_set
        if best_id is None:
            LOG.info("MCLP: sin ganancia marginal en el paso %d; se detiene", step)
            break
        remaining -= best_set
        cover.pop(best_id, None)
        chosen.append(
            {
                "orden": step,
                "COD_IPRESS": best_id,
                "poblacion_marginal": best_gain,
                "poblacion_cubierta_acumulada": base_covered
                + sum(c["poblacion_marginal"] for c in chosen)
                + best_gain,
            }
        )

    sequence = pd.DataFrame(chosen)
    rows = []
    for b in budgets:
        sel = sequence.head(b)["COD_IPRESS"].tolist() if not sequence.empty else []
        res = recompute_coverage(sel, base_t_min, candidates_long, weights, thr)
        ub = base_covered + sum(individual[:b])
        rows.append(
            {
                "presupuesto": b,
                "n_ascendidos": len(sel),
                "poblacion_cubierta": res["poblacion_cubierta"],
                "share_cubierta": res["share_cubierta"],
                "ganancia_vs_base": res["poblacion_cubierta"] - base_covered,
                "t_medio_min": res["t_medio_min"],
                "cota_superior_optimo": min(ub, total_pop),
                "garantia_voraz_pct_del_optimo": 100 * (1 - 1 / np.e),
                "brecha_maxima_vs_cota": max(
                    0.0, min(ub, total_pop) - res["poblacion_cubierta"]
                ),
            }
        )
    frontier = pd.DataFrame(rows)

    if not frontier.empty:
        first = frontier.iloc[0]
        dlog.record(
            check="MCLP (optimización de ubicación)",
            dataset="mclp",
            n_affected=int(first["n_ascendidos"]),
            n_total=int(candidates_long["COD_IPRESS"].nunique()),
            action=(
                f"con {int(first['presupuesto'])} ascensos se cubren "
                f"{first['ganancia_vs_base']:,.0f} habitantes más dentro de "
                f"{thr:.0f} minutos"
            ),
            justification=(
                "algoritmo voraz para cobertura máxima: garantiza al menos "
                "63,2% del óptimo, y la cota superior calculada (suma de las B "
                "mejores coberturas individuales) acota la brecha real por arriba. "
                "Se prefiere sobre un ILP exacto porque no introduce una "
                "dependencia de solver y la brecha resulta pequeña; el orden de "
                "elección es además directamente accionable"
            ),
            umbral_min=thr,
        )
    return frontier, sequence


# ------------------------------------------------------------------------ main
def run(save: bool = True) -> dict[str, pd.DataFrame]:
    dlog = DecisionLog(stage="models")
    access, od, cand, fac = load_model_inputs()
    base_t_min = od.min(axis=1)
    out: dict[str, pd.DataFrame] = {}

    if CFG.get("innovation", "run_2sfca", default=False):
        per_point, per_district = two_step_floating_catchment(access, od, fac, dlog)
        out["sfca_points"] = per_point
        out["sfca_district"] = per_district

    if CFG.get("innovation", "run_mclp", default=False):
        frontier, sequence = mclp_greedy(access, base_t_min, cand, dlog)
        if not frontier.empty:
            named = sequence.merge(
                fac[["COD_IPRESS", "NOMBRE", "CATEGORIA", "DEPARTAMENTO",
                     "PROVINCIA", "DISTRITO", "lat", "lon"]],
                on="COD_IPRESS", how="left",
            )
            out["mclp_frontier"] = frontier
            out["mclp_sequence"] = named

    if save:
        o = CFG.path("outputs")
        for key, df in out.items():
            if df is None or df.empty:
                continue
            path = o / f"{key}_{CFG.mode}.csv"
            df.to_csv(path, index=False, encoding="utf-8")
            LOG.info("escrito %s (%d filas)", path.name, len(df))
        dlog.save("models_log")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Innovación — 2SFCA y MCLP")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    out = run(save=not args.dry_run)

    if "mclp_frontier" in out:
        print("\nFrontera del MCLP (cobertura ganada por presupuesto):")
        print(
            out["mclp_frontier"][
                ["presupuesto", "ganancia_vs_base", "share_cubierta",
                 "brecha_maxima_vs_cota"]
            ].to_string(index=False)
        )
        print("\nPrimeros ascensos recomendados:")
        print(
            out["mclp_sequence"]
            .head(8)[["orden", "NOMBRE", "CATEGORIA", "DEPARTAMENTO", "poblacion_marginal"]]
            .to_string(index=False)
        )
    if "sfca_district" in out:
        top = out["sfca_district"].nsmallest(8, "sfca_index_per_10k")
        print("\nDistritos con menor accesibilidad 2SFCA:")
        print(top[["department", "district", "poblacion", "sfca_index_per_10k"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
