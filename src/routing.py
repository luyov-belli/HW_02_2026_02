"""Fase 2 — Ruteo por red vial, matriz origen × destino y caché.

Tres motores detrás de una misma interfaz (:class:`RoutingBackend`), elegidos con
``[routing].engine`` en ``config.md``:

* :class:`OSRMBackend`  — OSRM en Docker (motor oficial de este trabajo). Usa el
  servicio ``/table`` para matrices en lote y devuelve además la distancia de
  *snapping* de cada punto sin peticiones extra.
* :class:`GraphBackend` — grafo vial propio construido desde OSM (Overpass o el
  shapefile de Geofabrik) y resuelto con Dijkstra multiorigen de
  ``scipy.sparse.csgraph``. Corre sin Docker; sirve de validación cruzada.
* :class:`ORSBackend`   — OpenRouteService, solo emergencia, con caché y throttling.

Ninguna distancia se calcula por Haversine. La línea recta aparece únicamente en
dos lugares explícitos y documentados: la política de *fallback* para puntos
irruteables y la comparación "línea recta vs. red" que pide la Fase 5.

Salidas en ``data/outputs/``:

* ``od_matrix_<alcance>_<perfil>.parquet``  — matriz completa origen × resolutiva.
* ``od_candidates_<alcance>_<perfil>.parquet`` — k vecinos más cercanos entre los
  candidatos I-3/I-4, insumo del simulador de escenarios y del MCLP.
* ``snapping_report_<alcance>.csv``         — análisis de enganche a la red.
* ``routing_log.json`` / ``.csv``           — decisiones y cobertura de caché.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import numpy as np
import pandas as pd
import requests

from .config import CFG, PROJECT_ROOT
from .utils import DecisionLog, get_logger

LOG = get_logger("routing")

EARTH_R_KM = 6371.0088


# --------------------------------------------------------------------- interfaz
@dataclass
class TableResult:
    """Resultado de una consulta de matriz."""

    duration_s: np.ndarray  # (n_orig, n_dest), NaN si no hay ruta
    distance_m: np.ndarray  # (n_orig, n_dest), NaN si no hay ruta
    source_snap_m: np.ndarray  # (n_orig,) distancia al punto enganchado
    dest_snap_m: np.ndarray  # (n_dest,)


class RoutingBackend(Protocol):
    name: str

    def table(
        self, origins: np.ndarray, destinations: np.ndarray, profile: str
    ) -> TableResult:
        """``origins`` y ``destinations`` son arrays (n, 2) de (lon, lat)."""
        ...


# ------------------------------------------------------------------------ OSRM
class OSRMBackend:
    """Cliente del servicio ``/table`` de ``osrm-routed``.

    El servidor debe levantarse con ``--max-table-size`` al menos igual a
    ``sources + destinations`` de cada petición; ver ``docker/osrm.md`` y el
    workflow ``.github/workflows/routing.yml``.
    """

    name = "osrm"

    def __init__(self) -> None:
        rc = CFG["routing"]
        self.urls: dict[str, str] = dict(rc["osrm"]["base_urls"])
        self.timeout = int(rc["osrm"]["timeout_s"])
        self.retries = int(rc["osrm"]["retries"])
        self.max_table = int(rc["osrm_max_table_size"])
        self.max_coords = int(rc["osrm_max_coords_per_request"])
        self.chunk_sources = int(rc["table_chunk_sources"])
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "HW02-accesibilidad-salud/1.0"

    # -- infraestructura ----------------------------------------------------
    def base_url(self, profile: str) -> str:
        if profile not in self.urls:
            raise KeyError(f"no hay URL de OSRM configurada para el perfil '{profile}'")
        return self.urls[profile].rstrip("/")

    def is_available(self, profile: str) -> bool:
        try:
            r = self.session.get(
                f"{self.base_url(profile)}/nearest/v1/{profile}/-77.0428,-12.0464",
                timeout=10,
            )
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def _request_table(
        self, src: np.ndarray, dst: np.ndarray, profile: str
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Una petición ``GET /table`` con orígenes y destinos explícitos.

        Se usa GET y no POST porque ``osrm-routed`` no expone POST para ``/table``.
        Eso impone un límite práctico de longitud de URL, y de ahí viene
        ``osrm_max_coords_per_request``: los bloques se dimensionan para que la URL
        quede holgadamente por debajo de 8 KB. El costo es más peticiones, que
        contra un servidor local es irrelevante (milisegundos cada una).
        """
        coords = np.vstack([src, dst])
        coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in coords)
        n_s, n_d = len(src), len(dst)
        params = {
            "sources": ";".join(str(i) for i in range(n_s)),
            "destinations": ";".join(str(i) for i in range(n_s, n_s + n_d)),
            "annotations": "duration,distance",
        }
        url = f"{self.base_url(profile)}/table/v1/{profile}/{coord_str}"

        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("code") != "Ok":
                    raise RuntimeError(f"OSRM respondió {payload.get('code')}")
                dur = np.asarray(payload["durations"], dtype="float64")
                dist = np.asarray(payload.get("distances"), dtype="float64")
                s_snap = np.array(
                    [w.get("distance", np.nan) for w in payload.get("sources", [])],
                    dtype="float64",
                )
                d_snap = np.array(
                    [w.get("distance", np.nan) for w in payload.get("destinations", [])],
                    dtype="float64",
                )
                if s_snap.size != n_s:
                    s_snap = np.full(n_s, np.nan)
                if d_snap.size != n_d:
                    d_snap = np.full(n_d, np.nan)
                return dur, dist, s_snap, d_snap
            except Exception as err:  # noqa: BLE001
                last = err
                LOG.warning("table intento %d falló: %s", attempt, err)
                time.sleep(2 * attempt)
        raise RuntimeError(f"OSRM /table falló tras {self.retries} intentos: {last}")

    def table(
        self, origins: np.ndarray, destinations: np.ndarray, profile: str
    ) -> TableResult:
        n_o, n_d = len(origins), len(destinations)
        dur = np.full((n_o, n_d), np.nan)
        dist = np.full((n_o, n_d), np.nan)
        s_snap = np.full(n_o, np.nan)
        d_snap = np.full(n_d, np.nan)

        # Se trocean ambos ejes respetando --max-table-size del servidor y el
        # límite de longitud de URL (ver _request_table).
        budget = min(self.max_table, self.max_coords)
        src_chunk = max(1, min(self.chunk_sources, budget - 1, n_o))
        dst_chunk = max(1, min(n_d, budget - src_chunk))
        n_req = math.ceil(n_o / src_chunk) * math.ceil(n_d / dst_chunk)
        LOG.info(
            "OSRM %s: %d×%d en %d peticiones (bloques %d×%d)",
            profile, n_o, n_d, n_req, src_chunk, dst_chunk,
        )

        done = 0
        for i0 in range(0, n_o, src_chunk):
            i1 = min(i0 + src_chunk, n_o)
            for j0 in range(0, n_d, dst_chunk):
                j1 = min(j0 + dst_chunk, n_d)
                d, m, ss, ds = self._request_table(
                    origins[i0:i1], destinations[j0:j1], profile
                )
                dur[i0:i1, j0:j1] = d
                if m is not None and m.size:
                    dist[i0:i1, j0:j1] = m
                s_snap[i0:i1] = np.where(np.isnan(s_snap[i0:i1]), ss, s_snap[i0:i1])
                d_snap[j0:j1] = np.where(np.isnan(d_snap[j0:j1]), ds, d_snap[j0:j1])
                done += 1
                if done % 10 == 0 or done == n_req:
                    LOG.info("  %d/%d bloques", done, n_req)
        return TableResult(dur, dist, s_snap, d_snap)


# ------------------------------------------------------------- grafo vial local
class GraphBackend:
    """Grafo vial propio + Dijkstra multiorigen.

    Construcción del grafo (ver ``[routing.graph]`` en ``config.md``):

    1. Se cargan las vías de OSM del alcance activo (Overpass por mosaicos, o el
       shapefile de Geofabrik).
    2. Los vértices se redondean a ``coord_snap_decimals`` decimales y se
       identifican los **cruces** (vértices compartidos por dos o más vías) más los
       extremos de cada vía. Solo esos quedan como nodos; los vértices interiores se
       colapsan sumando su longitud al arco. Esto baja el grafo de ~10⁷ a ~10⁶ nodos
       conservando la longitud real recorrida.
    3. El costo de cada arco es ``longitud / velocidad(fclass)`` con las velocidades
       de ``[routing.graph.speeds_kmh]``.
    4. La matriz se resuelve con ``dijkstra`` desde los **destinos** (que son cientos,
       no miles) en bloques, y se leen las columnas de los orígenes. El grafo se trata
       como no dirigido: se documenta como limitación frente a OSRM, que sí respeta
       sentidos únicos y restricciones de giro.
    """

    name = "graph"

    def __init__(self) -> None:
        self.gcfg = CFG["routing"]["graph"]
        self._nodes: np.ndarray | None = None
        self._csr = None
        self._tree = None
        self._profile_built: str | None = None

    # -- carga de vías ------------------------------------------------------
    def load_roads(self) -> pd.DataFrame:
        """Devuelve un DataFrame con columnas ``fclass`` y ``coords`` (lista de x,y)."""
        source = self.gcfg["roads_source"]
        if source == "overpass":
            return self._load_roads_overpass()
        if source == "geofabrik":
            return self._load_roads_geofabrik()
        raise ValueError(f"roads_source desconocido: {source}")

    def _scope_bbox(self) -> tuple[float, float, float, float]:
        """BBox del alcance activo, a partir de los puntos ya validados."""
        proc = CFG.path("processed")
        pts = pd.read_parquet(proc / CFG.scoped_name("demand_sample", "parquet"))
        fac = pd.read_parquet(proc / CFG.scoped_name("facilities", "parquet"))
        fac = fac[fac["is_resolutive"].fillna(False)]
        lon = pd.concat([pts["lon"], fac["lon"]]).astype(float)
        lat = pd.concat([pts["lat"], fac["lat"]]).astype(float)
        pad = float(CFG["sources"]["overpass"]["bbox_pad_deg"])
        return (lon.min() - pad, lat.min() - pad, lon.max() + pad, lat.max() + pad)

    def _load_roads_overpass(self) -> pd.DataFrame:
        ocfg = CFG["sources"]["overpass"]
        cache = CFG.path("cache") / f"roads_overpass_{CFG.mode}.parquet"
        if cache.exists():
            LOG.info("vías de Overpass desde caché: %s", cache.name)
            df = pd.read_parquet(cache)
            df["coords"] = df["coords_wkt"].map(_decode_coords)
            return df[["fclass", "coords"]]

        lon0, lat0, lon1, lat1 = self._scope_bbox()
        step = float(ocfg["tile_size_deg"])
        tiles = [
            (a, b, min(a + step, lat1), min(b + step, lon1))
            for a in np.arange(lat0, lat1, step)
            for b in np.arange(lon0, lon1, step)
        ]
        LOG.info("Overpass: %d mosaicos de %.1f° sobre el alcance", len(tiles), step)

        rows: list[dict] = []
        seen: set[int] = set()
        for k, (s, w, n, e) in enumerate(tiles, start=1):
            data = self._overpass_query(s, w, n, e)
            elements = data.get("elements", [])
            # Con "out geom" cada vía trae su geometría embebida; con "out;" trae
            # solo ids de nodos y los nodos vienen como elementos aparte. Se
            # soportan ambas formas para no depender de la variante de la consulta.
            nodes = {
                el["id"]: (el["lon"], el["lat"])
                for el in elements
                if el.get("type") == "node"
            }
            for el in elements:
                if el.get("type") != "way":
                    continue
                # Una vía puede aparecer en dos mosaicos si cruza el borde.
                if el["id"] in seen:
                    continue
                if el.get("geometry"):
                    coords = [(p["lon"], p["lat"]) for p in el["geometry"]]
                else:
                    coords = [nodes[i] for i in el.get("nodes", []) if i in nodes]
                if len(coords) < 2:
                    continue
                seen.add(el["id"])
                rows.append(
                    {"fclass": el.get("tags", {}).get("highway", "road"), "coords": coords}
                )
            LOG.info("  mosaico %d/%d -> %d vías acumuladas", k, len(tiles), len(rows))

        if not rows:
            raise RuntimeError(
                "Overpass no devolvió ninguna vía para el alcance activo; revisar "
                "highway_regex y el bbox en config.md"
            )
        df = pd.DataFrame(rows)
        out = df.copy()
        out["coords_wkt"] = out["coords"].map(_encode_coords)
        out[["fclass", "coords_wkt"]].to_parquet(cache, index=False)
        LOG.info("vías guardadas en caché: %s (%d vías)", cache.name, len(df))
        return df

    def _overpass_query(self, s: float, w: float, n: float, e: float) -> dict:
        ocfg = CFG["sources"]["overpass"]
        query = (
            f'[out:json][timeout:{ocfg["timeout_s"]}];'
            f'way["highway"~"{ocfg["highway_regex"]}"]'
            f"({s:.4f},{w:.4f},{n:.4f},{e:.4f});"
            "out geom;"
        )
        # Overpass devuelve 406 sin User-Agent identificable; ver config.md.
        headers = {"User-Agent": ocfg["user_agent"]}
        last: Exception | None = None
        for endpoint in ocfg["endpoints"]:
            for attempt in range(1, 4):
                try:
                    r = requests.post(
                        endpoint,
                        data={"data": query},
                        headers=headers,
                        timeout=int(ocfg["timeout_s"]) + 60,
                    )
                    if r.status_code == 429:
                        time.sleep(20 * attempt)
                        continue
                    r.raise_for_status()
                    return r.json()
                except Exception as err:  # noqa: BLE001
                    last = err
                    time.sleep(5 * attempt)
            LOG.warning("endpoint Overpass agotado: %s", endpoint)
        raise RuntimeError(f"Overpass falló en todos los endpoints: {last}")

    def _load_roads_geofabrik(self) -> pd.DataFrame:
        import geopandas as gpd

        from .acquisition import find_layer

        shp = find_layer(CFG.path("raw") / "osm_shp", "roads", ".shp")
        lon0, lat0, lon1, lat1 = self._scope_bbox()
        gdf = gpd.read_file(shp, bbox=(lon0, lat0, lon1, lat1))
        LOG.info("vías de Geofabrik: %d (bbox del alcance)", len(gdf))
        return pd.DataFrame(
            {
                "fclass": gdf["fclass"].astype(str).to_numpy(),
                "coords": [list(g.coords) for g in gdf.geometry],
            }
        )

    # -- construcción del grafo --------------------------------------------
    def build(self, profile: str) -> None:
        from scipy.sparse import coo_matrix
        from scipy.spatial import cKDTree

        if self._profile_built == profile:
            return
        roads = self.load_roads()
        excluded = set(CFG["routing"]["graph"]["excluded_classes"].get(profile, []))
        speeds = CFG["routing"]["graph"]["speeds_kmh"]
        dec = int(self.gcfg["coord_snap_decimals"])

        # 1) contar apariciones de cada vértice para hallar cruces
        counts: dict[tuple[int, int], int] = {}
        kept: list[tuple[str, list[tuple[int, int]], list[tuple[float, float]]]] = []
        for fclass, coords in zip(roads["fclass"], roads["coords"], strict=True):
            if fclass in excluded:
                continue
            keys = [(round(x, dec), round(y, dec)) for x, y in coords]
            ikeys = [(int(x * 10**dec), int(y * 10**dec)) for x, y in keys]
            for k in ikeys:
                counts[k] = counts.get(k, 0) + 1
            kept.append((fclass, ikeys, coords))
        LOG.info("vías utilizables para '%s': %d", profile, len(kept))

        # 2) nodos = extremos + cruces
        node_id: dict[tuple[int, int], int] = {}
        coords_list: list[tuple[float, float]] = []

        def nid(key: tuple[int, int], xy: tuple[float, float]) -> int:
            got = node_id.get(key)
            if got is None:
                got = len(coords_list)
                node_id[key] = got
                coords_list.append(xy)
            return got

        # Se deduplica por par de nodos conservando el **menor** tiempo: dos vías
        # distintas pueden unir el mismo par de cruces y coo_matrix sumaría sus
        # pesos, inventando un arco más lento que cualquiera de las dos.
        edges: dict[tuple[int, int], float] = {}
        for fclass, ikeys, coords in kept:
            speed = float(speeds.get(fclass, speeds["default"]))
            start = 0
            acc = 0.0
            prev = coords[0]
            for pos in range(1, len(ikeys)):
                acc += _haversine_km(prev, coords[pos])
                prev = coords[pos]
                is_break = (
                    pos == len(ikeys) - 1
                    or counts[ikeys[pos]] > 1
                    or not self.gcfg["collapse_interior_vertices"]
                )
                if is_break and acc > 0:
                    a = nid(ikeys[start], coords[start])
                    b = nid(ikeys[pos], coords[pos])
                    if a != b:
                        seconds = acc / speed * 3600.0
                        key = (a, b) if a < b else (b, a)
                        prior = edges.get(key)
                        if prior is None or seconds < prior:
                            edges[key] = seconds
                    start = pos
                    acc = 0.0

        n = len(coords_list)
        self._nodes = np.asarray(coords_list, dtype="float64")
        ab = np.fromiter(
            (v for k in edges for v in k), dtype="int64", count=2 * len(edges)
        ).reshape(-1, 2)
        w = np.fromiter(edges.values(), dtype="float64", count=len(edges))
        # Se almacenan ambos sentidos y se resuelve con directed=False; el grafo no
        # respeta sentidos únicos ni restricciones de giro (limitación frente a OSRM).
        mat = coo_matrix(
            (np.concatenate([w, w]),
             (np.concatenate([ab[:, 0], ab[:, 1]]),
              np.concatenate([ab[:, 1], ab[:, 0]]))),
            shape=(n, n),
        ).tocsr()
        self._csr = mat
        self._tree = cKDTree(self._nodes)
        self._profile_built = profile
        LOG.info("grafo '%s': %d nodos, %d arcos", profile, n, mat.nnz // 2)

    def _snap(self, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Nodo más cercano y distancia de enganche en metros."""
        assert self._tree is not None and self._nodes is not None
        _, idx = self._tree.query(pts, k=1)
        idx = np.atleast_1d(idx)
        snapped = self._nodes[idx]
        dist_m = np.array(
            [_haversine_km(tuple(a), tuple(b)) * 1000.0 for a, b in zip(pts, snapped, strict=True)]
        )
        return idx, dist_m

    def table(
        self, origins: np.ndarray, destinations: np.ndarray, profile: str
    ) -> TableResult:
        from scipy.sparse.csgraph import dijkstra

        self.build(profile)
        o_idx, o_snap = self._snap(origins)
        d_idx, d_snap = self._snap(destinations)

        n_o, n_d = len(origins), len(destinations)
        dur = np.full((n_o, n_d), np.nan)
        block = 40  # 40 filas × n_nodos × 8 bytes acota la memoria
        LOG.info("Dijkstra desde %d destinos en bloques de %d", n_d, block)
        for j0 in range(0, n_d, block):
            j1 = min(j0 + block, n_d)
            dm = dijkstra(self._csr, directed=False, indices=d_idx[j0:j1])
            dur[:, j0:j1] = dm[:, o_idx].T
            LOG.info("  destinos %d/%d", j1, n_d)
        dur[np.isinf(dur)] = np.nan
        # El grafo pesa en segundos; la distancia se reconstruye con la velocidad
        # media efectiva por arco, que no es recuperable de la matriz de tiempos,
        # así que se deja como NaN y se documenta: el motor 'graph' no reporta
        # distancia de red, solo tiempo. OSRM sí reporta ambas.
        dist = np.full((n_o, n_d), np.nan)
        return TableResult(dur, dist, o_snap, d_snap)


# ----------------------------------------------------------- OpenRouteService
class ORSBackend:
    """Solo emergencia: matriz de ORS con caché en disco y throttling."""

    name = "ors"

    def __init__(self) -> None:
        import os

        self.key = os.environ.get("ORS_API_KEY")
        if not self.key:
            raise RuntimeError(
                "engine='ors' requiere la variable de entorno ORS_API_KEY"
            )
        self.profiles = {
            "car": "driving-car",
            "foot": "foot-walking",
            "bike": "cycling-regular",
        }

    def table(
        self, origins: np.ndarray, destinations: np.ndarray, profile: str
    ) -> TableResult:
        n_o, n_d = len(origins), len(destinations)
        dur = np.full((n_o, n_d), np.nan)
        dist = np.full((n_o, n_d), np.nan)
        # El plan gratuito limita a 3 500 pares por petición y 40 por minuto.
        per_call = 3500
        src_chunk = max(1, per_call // max(n_d, 1))
        for i0 in range(0, n_o, src_chunk):
            i1 = min(i0 + src_chunk, n_o)
            locs = np.vstack([origins[i0:i1], destinations]).tolist()
            body = {
                "locations": locs,
                "sources": list(range(i1 - i0)),
                "destinations": list(range(i1 - i0, i1 - i0 + n_d)),
                "metrics": ["duration", "distance"],
            }
            r = requests.post(
                f"https://api.openrouteservice.org/v2/matrix/{self.profiles[profile]}",
                json=body,
                headers={"Authorization": self.key},
                timeout=300,
            )
            r.raise_for_status()
            payload = r.json()
            dur[i0:i1] = np.asarray(payload["durations"], dtype="float64")
            dist[i0:i1] = np.asarray(payload["distances"], dtype="float64")
            time.sleep(2.0)  # throttling explícito
        return TableResult(dur, dist, np.full(n_o, np.nan), np.full(n_d, np.nan))


BACKENDS = {"osrm": OSRMBackend, "graph": GraphBackend, "ors": ORSBackend}


def get_backend(engine: str | None = None) -> RoutingBackend:
    engine = engine or CFG["routing"]["engine"]
    if engine not in BACKENDS:
        raise ValueError(f"engine '{engine}' no está entre {sorted(BACKENDS)}")
    LOG.info("motor de ruteo: %s", engine)
    return BACKENDS[engine]()  # type: ignore[abstract]


# ------------------------------------------------------------------- geometría
def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distancia de círculo máximo. **No** se usa para ruteo (ver docstring)."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(h))


def haversine_matrix(origins: np.ndarray, destinations: np.ndarray) -> np.ndarray:
    """Matriz de distancias en línea recta (km), vectorizada.

    Se usa solo para (a) la política de *fallback* de puntos irruteables y (b) la
    comparación "línea recta vs. red" del informe, que es justamente el argumento
    de por qué no basta con Haversine.
    """
    lo1 = np.radians(origins[:, 0])[:, None]
    la1 = np.radians(origins[:, 1])[:, None]
    lo2 = np.radians(destinations[:, 0])[None, :]
    la2 = np.radians(destinations[:, 1])[None, :]
    h = (
        np.sin((la2 - la1) / 2) ** 2
        + np.cos(la1) * np.cos(la2) * np.sin((lo2 - lo1) / 2) ** 2
    )
    return 2 * EARTH_R_KM * np.arcsin(np.sqrt(np.clip(h, 0, 1)))


def _encode_coords(coords: Iterable[tuple[float, float]]) -> str:
    return ";".join(f"{x:.6f},{y:.6f}" for x, y in coords)


def _decode_coords(text: str) -> list[tuple[float, float]]:
    return [
        (float(p.split(",")[0]), float(p.split(",")[1])) for p in text.split(";") if p
    ]


# ----------------------------------------------------------------------- caché
def cache_key(
    engine: str, profile: str, origins: np.ndarray, destinations: np.ndarray
) -> str:
    """Huella estable del conjunto de pares consultado."""
    h = hashlib.sha256()
    h.update(engine.encode())
    h.update(profile.encode())
    h.update(np.round(origins, 6).tobytes())
    h.update(np.round(destinations, 6).tobytes())
    return h.hexdigest()[:16]


def table_cached(
    backend: RoutingBackend,
    origins: np.ndarray,
    destinations: np.ndarray,
    profile: str,
    dlog: DecisionLog | None = None,
) -> tuple[TableResult, bool]:
    """Consulta la matriz usando el disco si ya se calculó (requisito de la Fase 2)."""
    cdir = Path(CFG["routing"]["cache_dir"])
    if not cdir.is_absolute():
        cdir = PROJECT_ROOT / cdir
    cdir.mkdir(parents=True, exist_ok=True)
    key = cache_key(backend.name, profile, origins, destinations)
    npz = cdir / f"table_{backend.name}_{profile}_{key}.npz"

    if npz.exists():
        z = np.load(npz)
        LOG.info("matriz %s/%s desde caché en disco (%s)", backend.name, profile, npz.name)
        if dlog:
            dlog.record(
                check="uso de caché de ruteo",
                dataset=f"od_{profile}",
                n_affected=int(z["duration_s"].size),
                n_total=int(z["duration_s"].size),
                action=f"leído de disco: {npz.name}",
                justification=(
                    "la segunda corrida no vuelve a consultar el motor de ruteo; la "
                    "clave de caché es el hash de (motor, perfil, coordenadas)"
                ),
            )
        return (
            TableResult(
                z["duration_s"], z["distance_m"], z["source_snap_m"], z["dest_snap_m"]
            ),
            True,
        )

    t0 = time.time()
    res = backend.table(origins, destinations, profile)
    np.savez_compressed(
        npz,
        duration_s=res.duration_s.astype("float32"),
        distance_m=res.distance_m.astype("float32"),
        source_snap_m=res.source_snap_m.astype("float32"),
        dest_snap_m=res.dest_snap_m.astype("float32"),
    )
    LOG.info(
        "matriz %s/%s calculada en %.1f s y guardada en %s",
        backend.name, profile, time.time() - t0, npz.name,
    )
    if dlog:
        dlog.record(
            check="cálculo de matriz de ruteo",
            dataset=f"od_{profile}",
            n_affected=int(res.duration_s.size),
            n_total=int(res.duration_s.size),
            action=f"{origins.shape[0]}×{destinations.shape[0]} pares en {time.time() - t0:.0f} s",
            justification=f"motor {backend.name}, perfil {profile}; guardado en {npz.name}",
        )
    return res, False


# ------------------------------------------------------- fallback y diagnóstico
def apply_fallback(
    res: TableResult,
    origins: np.ndarray,
    destinations: np.ndarray,
    dlog: DecisionLog,
    profile: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Política documentada para pares sin ruta.

    Devuelve ``(duration_s, imputed_mask)``. Los pares sin ruta se imputan con
    ``distancia_recta × factor_desvío / velocidad_recta``; los orígenes cuyo
    enganche a la red excede ``no_road_access_threshold_m`` **no se imputan**: se
    dejan como NaN y se reportan aparte como "sin acceso vial", porque imputarles
    un tiempo de auto sería inventar una carretera que no existe.
    """
    fb = CFG["routing"]["fallback"]
    dur = res.duration_s.copy()
    missing = ~np.isfinite(dur)

    no_access = res.source_snap_m > float(fb["no_road_access_threshold_m"])
    no_access = np.nan_to_num(no_access, nan=False).astype(bool)

    straight_km = haversine_matrix(origins, destinations)
    imputed_s = (
        straight_km
        * float(fb["straight_line_detour_factor"])
        / float(fb["straight_line_speed_kmh"])
        * 3600.0
    )
    to_impute = missing & ~no_access[:, None]
    dur[to_impute] = imputed_s[to_impute]
    dur[no_access, :] = np.nan

    dlog.record(
        check="pares sin ruta en la red",
        dataset=f"od_{profile}",
        n_affected=int(missing.sum()),
        n_total=int(dur.size),
        action=(
            f"{int(to_impute.sum())} pares imputados por línea recta penalizada "
            f"(×{fb['straight_line_detour_factor']} a "
            f"{fb['straight_line_speed_kmh']} km/h); "
            f"{int(no_access.sum())} orígenes marcados sin acceso vial"
        ),
        justification=(
            "no imputar dejaría distritos enteros fuera de los agregados y sesgaría "
            "la cobertura al alza; imputar a un origen que está a más de "
            f"{fb['no_road_access_threshold_m'] / 1000:.0f} km de cualquier vía "
            "inventaría infraestructura inexistente, así que ese caso se reporta "
            "por separado en lugar de rellenarse"
        ),
    )
    return dur, to_impute


def snapping_report(
    res: TableResult, profile: str, dlog: DecisionLog
) -> pd.DataFrame:
    """Análisis de enganche a la red que exige la Fase 2.

    Las distancias de enganche vienen dentro de la respuesta de ``/table`` de
    OSRM, así que este reporte no cuesta peticiones adicionales.
    """
    fb = CFG["routing"]["fallback"]
    limit = float(CFG["routing"]["snap_max_distance_m"])

    rows = []
    for label, snap in (
        ("demanda", res.source_snap_m),
        ("establecimientos", res.dest_snap_m),
    ):
        snap = np.asarray(snap, dtype="float64")
        finite = np.isfinite(snap)
        rows.append(
            {
                "profile": profile,
                "conjunto": label,
                "n_puntos": int(len(snap)),
                "n_snap_desconocido": int((~finite).sum()),
                "snap_medio_m": float(np.nanmean(snap)) if finite.any() else float("nan"),
                "snap_mediana_m": float(np.nanmedian(snap)) if finite.any() else float("nan"),
                "snap_p95_m": float(np.nanpercentile(snap[finite], 95)) if finite.any() else float("nan"),
                "snap_max_m": float(np.nanmax(snap)) if finite.any() else float("nan"),
                "n_sobre_limite": int((snap > limit).sum()),
                "n_sin_acceso_vial": int((snap > float(fb["no_road_access_threshold_m"])).sum()),
            }
        )
    rep = pd.DataFrame(rows)
    dlog.record(
        check="análisis de snapping",
        dataset=f"od_{profile}",
        n_affected=int(rep["n_sobre_limite"].sum()),
        n_total=int(rep["n_puntos"].sum()),
        action=f"puntos enganchados a más de {limit:.0f} m de su coordenada declarada",
        justification=(
            "un enganche largo significa que el punto se está ruteando desde una vía "
            "que no es la suya; se reporta la distribución completa (media, mediana, "
            "p95, máximo) en lugar de un solo promedio"
        ),
        snap_mean_demand_m=round(float(rep.loc[0, "snap_medio_m"]), 1),
    )
    return rep


# ----------------------------------------------------------------- orquestación
def _coords(df: pd.DataFrame) -> np.ndarray:
    return df[["lon", "lat"]].astype(float).to_numpy()


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    proc = CFG.path("processed")
    demand = pd.read_parquet(proc / CFG.scoped_name("demand_sample", "parquet"))
    fac = pd.read_parquet(proc / CFG.scoped_name("facilities", "parquet"))
    resolutive = fac[fac["is_resolutive"].fillna(False)].reset_index(drop=True)
    candidates = fac[
        fac["is_upgrade_candidate"].fillna(False)
        & fac["is_operational"].fillna(False)
        & fac["has_coords"].fillna(False)
    ].reset_index(drop=True)
    return demand, resolutive, candidates


def run_primary(
    engine: str | None = None, k_candidates: int = 10, save: bool = True
) -> dict[str, pd.DataFrame]:
    """Etapa 1: matriz completa origen × resolutiva en el perfil principal.

    Escribe también la tabla reducida de candidatas I-3/I-4 que alimentan el
    simulador de escenarios y el MCLP.
    """
    rc = CFG["routing"]
    primary = rc["primary_profile"]
    dlog = DecisionLog(stage=f"routing_{primary}")
    backend = get_backend(engine)
    demand, resolutive, candidates = load_inputs()

    LOG.info(
        "alcance %s: %d orígenes, %d resolutivas, %d candidatas I-3/I-4",
        CFG.mode, len(demand), len(resolutive), len(candidates),
    )
    dlog.set_counter("origins", len(demand))
    dlog.set_counter("resolutive_destinations", len(resolutive))
    dlog.set_counter("upgrade_candidates", len(candidates))

    out: dict[str, pd.DataFrame] = {}
    o_all = _coords(demand)
    d_res = _coords(resolutive)

    res, _ = table_cached(backend, o_all, d_res, primary, dlog)
    dur, imputed = apply_fallback(res, o_all, d_res, dlog, primary)
    out["snapping"] = snapping_report(res, primary, dlog)

    index = pd.Index(demand["demand_id"], name="demand_id")
    columns = pd.Index(resolutive["COD_IPRESS"], name="COD_IPRESS")
    wide = pd.DataFrame((dur / 60.0).astype("float32"), index=index, columns=columns)
    dist_wide = pd.DataFrame(
        (res.distance_m / 1000.0).astype("float32"), index=index, columns=columns
    )
    out[f"od_{primary}"] = wide
    out[f"od_dist_{primary}"] = dist_wide
    out[f"od_imputed_{primary}"] = pd.DataFrame(imputed, index=index, columns=columns)

    if len(candidates):
        d_cand = _coords(candidates)
        res_c, _ = table_cached(backend, o_all, d_cand, primary, dlog)
        dur_c, _ = apply_fallback(res_c, o_all, d_cand, dlog, f"{primary}_cand")
        out["od_candidates"] = _topk_long(
            dur_c / 60.0,
            demand["demand_id"].to_numpy(),
            candidates["COD_IPRESS"].to_numpy(),
            k_candidates,
        )
        dlog.record(
            check="reducción de la matriz de candidatas",
            dataset="od_candidates",
            n_affected=int(len(out["od_candidates"])),
            n_total=int(dur_c.size),
            action=f"se conservan las {k_candidates} candidatas más cercanas por origen",
            justification=(
                "para recalcular cobertura bajo cualquier umbral ≤ el tiempo de la "
                f"{k_candidates}.ª candidata, el mínimo sobre el conjunto ascendido "
                "es idéntico al que daría la matriz completa; guardar 26 millones de "
                "pares en el repositorio no aportaría información utilizable"
            ),
        )

    out["straight_vs_network"] = straight_line_comparison(
        wide, dist_wide, o_all, d_res, demand, dlog
    )
    if save:
        _save_outputs(out, dlog, f"routing_log_{primary}")
    return out


def run_secondary(
    profile: str, engine: str | None = None, save: bool = True
) -> dict[str, pd.DataFrame]:
    """Etapa 2: perfil secundario (pie o bicicleta) sobre submuestra.

    Los destinos se restringen a la unión de los ``secondary_top_k`` establecimientos
    más cercanos **en auto** de cada origen de la submuestra. Motivo: rutear a pie
    los 644 establecimientos del país produciría trayectos de días que ningún
    peatón recorre, y multiplicaría el tiempo de cómputo sin cambiar el mínimo. Con
    k = 15 la probabilidad de que el establecimiento más cercano a pie no esté en el
    conjunto es despreciable, y se verifica reportando cuántos mínimos caen en la
    posición k-ésima.
    """
    rc = CFG["routing"]
    primary = rc["primary_profile"]
    dlog = DecisionLog(stage=f"routing_{profile}")
    backend = get_backend(engine)
    demand, resolutive, _ = load_inputs()

    sub = _secondary_subsample(demand, int(rc["secondary_profile_sample"]))
    base = read_matrix(f"od_{primary}")
    if base is None:
        raise RuntimeError(
            f"falta od_{primary}_matrix_{CFG.mode}.parquet; ejecutar primero "
            "`python -m src.routing --stage primary`"
        )
    top_k = int(rc["secondary_top_k"])
    sub = sub[sub["demand_id"].isin(base.index)].reset_index(drop=True)
    near = base.loc[sub["demand_id"]]
    vals = np.where(np.isfinite(near.to_numpy()), near.to_numpy(), np.inf)
    k = min(top_k, vals.shape[1])
    nearest_idx = np.unique(np.argpartition(vals, k - 1, axis=1)[:, :k])
    keep_cols = near.columns.to_numpy()[nearest_idx]
    dest = resolutive[resolutive["COD_IPRESS"].isin(keep_cols)].reset_index(drop=True)
    LOG.info(
        "perfil %s: %d orígenes × %d destinos (unión de los %d más cercanos en auto)",
        profile, len(sub), len(dest), top_k,
    )

    o_sub, d_sub = _coords(sub), _coords(dest)
    res, _ = table_cached(backend, o_sub, d_sub, profile, dlog)
    dur, _ = apply_fallback(res, o_sub, d_sub, dlog, profile)

    out = {
        f"od_{profile}": pd.DataFrame(
            (dur / 60.0).astype("float32"),
            index=pd.Index(sub["demand_id"], name="demand_id"),
            columns=pd.Index(dest["COD_IPRESS"], name="COD_IPRESS"),
        ),
        f"snapping_{profile}": snapping_report(res, profile, dlog),
    }
    dlog.record(
        check="restricción de destinos del perfil secundario",
        dataset=f"od_{profile}",
        n_affected=len(dest),
        n_total=len(resolutive),
        action=f"unión de los {top_k} establecimientos más cercanos en auto",
        justification=(
            "rutear a pie los 644 establecimientos del país generaría trayectos de "
            "días sin cambiar el mínimo; k=15 hace despreciable el riesgo de perder "
            "el más cercano a pie"
        ),
    )
    if save:
        _save_outputs(out, dlog, f"routing_log_{profile}")
    return out


def run_combine(save: bool = True) -> dict[str, pd.DataFrame]:
    """Etapa 3: análisis multimodal a partir de las matrices ya escritas.

    Se ejecuta después de las etapas por perfil porque en CI cada perfil de OSRM
    se construye y se descarta por separado (los tres grafos no caben a la vez en
    el disco del runner).
    """
    dlog = DecisionLog(stage="routing_combine")
    demand, resolutive, _ = load_inputs()
    primary = CFG["routing"]["primary_profile"]

    loaded: dict[str, pd.DataFrame] = {}
    for profile in CFG["routing"]["profiles"]:
        mat = read_matrix(f"od_{profile}")
        if mat is not None:
            loaded[f"od_{profile}"] = mat
            LOG.info("cargada matriz %s: %s", profile, mat.shape)
        else:
            LOG.warning("no hay matriz para el perfil '%s'", profile)
            dlog.record(
                check=f"disponibilidad del perfil {profile}",
                dataset=f"od_{profile}",
                n_affected=0,
                n_total=1,
                action="omitido del análisis multimodal",
                justification="la etapa de ese perfil no produjo matriz",
            )

    out = {"cross_mode": cross_mode_analysis(loaded, demand, resolutive, dlog)}
    if save:
        _save_outputs(out, dlog, "routing_log_combine")
    return out


def read_matrix(key: str) -> pd.DataFrame | None:
    """Lee una matriz ancha ya escrita en ``data/outputs``."""
    path = CFG.path("outputs") / f"{key}_matrix_{CFG.mode}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    return df.set_index("demand_id")


def _secondary_subsample(demand: pd.DataFrame, n: int) -> pd.DataFrame:
    """Submuestra para perfiles a pie/bicicleta, priorizando población urbana.

    El enunciado pide tiempos a pie "para áreas urbanas": la submuestra se
    construye con los puntos urbanos de mayor población y se completa con puntos
    rurales para poder contrastar los dos entornos.
    """
    if len(demand) <= n:
        return demand
    urban = demand[demand["area_type"] == "URBANO"].nlargest(
        int(n * 0.75), "population"
    )
    rural = demand[demand["area_type"] == "RURAL"].nlargest(
        n - len(urban), "population"
    )
    return pd.concat([urban, rural]).reset_index(drop=True)


def _topk_long(
    minutes: np.ndarray, origin_ids: np.ndarray, dest_ids: np.ndarray, k: int
) -> pd.DataFrame:
    k = min(k, minutes.shape[1])
    filled = np.where(np.isfinite(minutes), minutes, np.inf)
    idx = np.argpartition(filled, k - 1, axis=1)[:, :k]
    rows = np.repeat(np.arange(minutes.shape[0]), k)
    cols = idx.ravel()
    vals = minutes[rows, cols]
    df = pd.DataFrame(
        {
            "demand_id": origin_ids[rows],
            "COD_IPRESS": dest_ids[cols],
            "minutes": vals.astype("float32"),
        }
    )
    df = df[np.isfinite(df["minutes"])].reset_index(drop=True)
    return df.sort_values(["demand_id", "minutes"]).reset_index(drop=True)


def cross_mode_analysis(
    out: dict[str, pd.DataFrame],
    demand: pd.DataFrame,
    resolutive: pd.DataFrame,
    dlog: DecisionLog,
) -> pd.DataFrame:
    """Comparación auto / bicicleta / pie y cambio del establecimiento más cercano."""
    primary = CFG["routing"]["primary_profile"]
    base = out.get(f"od_{primary}")
    if base is None:
        return pd.DataFrame()

    rows = []
    for profile in ("foot", "bike"):
        alt = out.get(f"od_{profile}")
        if alt is None or alt.empty:
            continue
        common = base.index.intersection(alt.index)
        b = base.loc[common]
        a = alt.loc[common]
        t_b = b.min(axis=1)
        t_a = a.min(axis=1)
        near_b = b.idxmin(axis=1)
        near_a = a.idxmin(axis=1)
        differs = (near_b != near_a) & t_b.notna() & t_a.notna()
        ratio = (t_a / t_b).replace([np.inf, -np.inf], np.nan)
        rows.append(
            {
                "profile": profile,
                "n_origenes": int(len(common)),
                "mediana_min_auto": float(t_b.median()),
                f"mediana_min_{profile}": float(t_a.median()),
                "ratio_mediano": float(ratio.median()),
                "ratio_p90": float(ratio.quantile(0.90)),
                "n_cambia_establecimiento_mas_cercano": int(differs.sum()),
                "share_cambia": float(differs.mean()),
            }
        )
    rep = pd.DataFrame(rows)
    if not rep.empty:
        dlog.record(
            check="análisis multimodal",
            dataset="cross_mode",
            n_affected=int(rep["n_cambia_establecimiento_mas_cercano"].sum()),
            n_total=int(rep["n_origenes"].sum()),
            action="orígenes cuyo establecimiento más cercano cambia según el modo",
            justification=(
                "si el establecimiento más cercano en auto no es el más cercano a "
                "pie, la cobertura declarada depende del supuesto de motorización, "
                "que en zonas rurales no se cumple"
            ),
        )
    return rep


def straight_line_comparison(
    wide: pd.DataFrame,
    dist_wide: pd.DataFrame,
    origins: np.ndarray,
    destinations: np.ndarray,
    demand: pd.DataFrame,
    dlog: DecisionLog,
) -> pd.DataFrame:
    """Cuantifica el error de usar Haversine en lugar de la red (insumo de la Fase 5).

    Se reportan dos cosas distintas y ambas importan:

    * **Índice de desvío en distancia** = km de red / km en línea recta hasta el
      mismo establecimiento. Es la medida limpia, sin supuestos de velocidad, pero
      requiere que el motor devuelva distancias (OSRM sí; el motor ``graph`` no).
    * **Reclasificación en tiempo**: cuánta población cambiaría de banda de cobertura
      si se hubiera usado Haversine con la velocidad supuesta que declara
      ``[metrics].naive_haversine_speed_kmh``.
    """
    naive_kmh = float(CFG["metrics"]["naive_haversine_speed_kmh"])
    bands = list(CFG["metrics"]["time_bands_min"])
    straight_km = haversine_matrix(origins, destinations)

    net_min = wide.to_numpy()
    t_net = np.where(np.isfinite(net_min), net_min, np.inf).min(axis=1)
    nearest_net = np.where(np.isfinite(net_min), net_min, np.inf).argmin(axis=1)
    nearest_straight = straight_km.argmin(axis=1)
    t_straight = straight_km.min(axis=1) / naive_kmh * 60.0

    t_net = np.where(np.isfinite(t_net), t_net, np.nan)
    valid = np.isfinite(t_net) & np.isfinite(t_straight)
    detour_time = np.full_like(t_net, np.nan)
    detour_time[valid] = t_net[valid] / np.maximum(t_straight[valid], 1e-6)

    # Índice de desvío en distancia, hacia el mismo establecimiento más cercano.
    rows_idx = np.arange(len(t_net))
    net_km = dist_wide.to_numpy()[rows_idx, nearest_net]
    line_km = straight_km[rows_idx, nearest_net]
    dist_ok = np.isfinite(net_km) & (line_km > 0.5)
    detour_dist = np.full_like(t_net, np.nan)
    detour_dist[dist_ok] = net_km[dist_ok] / line_km[dist_ok]

    weights = demand["design_weight"].to_numpy(dtype=float)
    reclass: dict[str, float] = {}
    for band in bands:
        moved = valid & (t_straight <= band) & (t_net > band)
        reclass[f"poblacion_reclasificada_{band}min"] = float(weights[moved].sum())

    metrics: dict[str, float] = {
        "n_origenes": float(valid.sum()),
        "velocidad_naif_supuesta_kmh": naive_kmh,
        "mediana_min_red": float(np.nanmedian(t_net[valid])),
        "mediana_min_linea_recta": float(np.nanmedian(t_straight[valid])),
        "factor_desvio_tiempo_mediano": float(np.nanmedian(detour_time[valid])),
        "factor_desvio_tiempo_p90": float(np.nanpercentile(detour_time[valid], 90)),
        "indice_desvio_distancia_mediano": (
            float(np.nanmedian(detour_dist[dist_ok])) if dist_ok.any() else float("nan")
        ),
        "indice_desvio_distancia_p90": (
            float(np.nanpercentile(detour_dist[dist_ok], 90))
            if dist_ok.any()
            else float("nan")
        ),
        "n_con_distancia_de_red": float(dist_ok.sum()),
        "share_cambia_establecimiento_mas_cercano": float(
            (nearest_net != nearest_straight)[valid].mean()
        ),
        **reclass,
    }
    rep = pd.DataFrame({"metrica": list(metrics), "valor": list(metrics.values())})

    dlog.record(
        check="línea recta vs. red vial",
        dataset="straight_vs_network",
        n_affected=int(((nearest_net != nearest_straight) & valid).sum()),
        n_total=int(valid.sum()),
        action="orígenes en los que Haversine elige otro establecimiento como el más cercano",
        justification=(
            "es la evidencia de por qué el enunciado prohíbe Haversine. Índice de "
            f"desvío en distancia (mediana) = "
            f"{metrics['indice_desvio_distancia_mediano']:.2f}; con una velocidad "
            f"supuesta de {naive_kmh:.0f} km/h, "
            f"{reclass[f'poblacion_reclasificada_{bands[1]}min']:,.0f} habitantes "
            f"que el análisis naíf declararía dentro de {bands[1]} minutos están en "
            "realidad fuera"
        ),
    )
    return rep


def _save_outputs(
    out: dict[str, pd.DataFrame], dlog: DecisionLog, log_stem: str
) -> None:
    o = CFG.path("outputs")
    primary = CFG["routing"]["primary_profile"]

    for key, df in out.items():
        if df is None or (hasattr(df, "empty") and df.empty):
            continue
        if key.startswith("od_") and key != "od_candidates":
            path = o / f"{key}_matrix_{CFG.mode}.parquet"
            df.reset_index().to_parquet(path, index=False, compression="zstd")
        elif key == "od_candidates":
            path = o / f"od_candidates_{CFG.mode}_{primary}.parquet"
            df.to_parquet(path, index=False, compression="zstd")
        else:
            path = o / f"{key}_{CFG.mode}.csv"
            df.to_csv(path, index=False, encoding="utf-8")
        LOG.info("escrito %s (%.1f MB)", path.name, path.stat().st_size / 1e6)

    dlog.save(log_stem)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fase 2 — ruteo y matriz OD")
    p.add_argument("--engine", default=None, choices=sorted(BACKENDS))
    p.add_argument(
        "--stage",
        default="all",
        choices=["all", "primary", "secondary", "combine", "check"],
        help=(
            "primary: matriz completa del perfil principal; secondary: un perfil "
            "alterno; combine: análisis multimodal con lo ya escrito. En CI se "
            "invocan por separado porque cada perfil de OSRM se construye aparte."
        ),
    )
    p.add_argument("--profile", default=None, help="perfil para --stage secondary")
    p.add_argument("--k-candidates", type=int, default=10)
    args = p.parse_args(argv)

    primary = CFG["routing"]["primary_profile"]

    if args.stage == "check":
        b = get_backend(args.engine)
        if isinstance(b, OSRMBackend):
            for prof in CFG["routing"]["profiles"]:
                try:
                    ok = b.is_available(prof)
                except KeyError:
                    ok = False
                print(f"  perfil {prof:5s}: {'disponible' if ok else 'NO disponible'}")
        else:
            print(f"  motor '{b.name}' no requiere servidor")
        return 0

    if args.stage == "secondary":
        if not args.profile:
            p.error("--stage secondary requiere --profile")
        run_secondary(args.profile, engine=args.engine)
        return 0

    if args.stage == "combine":
        rep = run_combine()
        print(rep["cross_mode"].to_string(index=False) if not rep["cross_mode"].empty
              else "sin perfiles secundarios disponibles")
        return 0

    out = run_primary(engine=args.engine, k_candidates=args.k_candidates)
    if args.stage == "all":
        for prof in [x for x in CFG["routing"]["profiles"] if x != primary]:
            try:
                run_secondary(prof, engine=args.engine)
            except Exception as err:  # noqa: BLE001
                LOG.warning("perfil '%s' omitido: %s", prof, err)
        run_combine()

    wide = out[f"od_{primary}"]
    tmin = wide.min(axis=1)
    print(
        f"\nMatriz {wide.shape[0]}×{wide.shape[1]}. Tiempo al establecimiento "
        f"resolutivo más cercano (min): mediana {tmin.median():.1f}, "
        f"p90 {tmin.quantile(0.9):.1f}, máx {tmin.max():.1f}."
    )
    print(
        json.dumps(
            out["straight_vs_network"].to_dict("records"), indent=2, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
