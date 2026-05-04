from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pandas as pd
import requests

from .constants import OSRM_ROUTE_URL, ROUTING_TIMEOUT_SECONDS
from .storage import load_routes, upsert_route
from .utils import has_valid_coordinates

def fetch_street_route(
    start_lat: float, start_lon: float, end_lat: float, end_lon: float
) -> tuple[dict[str, Any] | None, str | None]:
    coordinates = f"{start_lon},{start_lat};{end_lon},{end_lat}"
    params = {
        "alternatives": "false",
        "steps": "false",
        "geometries": "geojson",
        "overview": "full",
    }

    try:
        response = requests.get(
            OSRM_ROUTE_URL.format(coordinates=coordinates),
            params=params,
            timeout=ROUTING_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout:
        return None, "Tempo limite excedido ao consultar o OSRM."
    except requests.RequestException as exc:
        return None, f"Falha de conexao com o OSRM: {exc}"
    except ValueError:
        return None, "Resposta invalida recebida do OSRM."

    if payload.get("code") != "Ok" or not payload.get("routes"):
        return None, "O OSRM nao encontrou uma rota real para essas coordenadas."

    route = payload["routes"][0]
    geometry = route.get("geometry", {}).get("coordinates", [])
    route_points = [[lat, lon] for lon, lat in geometry]
    if not route_points:
        return None, "O OSRM retornou uma rota sem geometria."

    return (
        {
            "distance_km": float(route["distance"]) / 1000,
            "duration_min": float(route.get("duration", 0)) / 60,
            "points": route_points,
        },
        None,
    )

def build_route_cache_row(
    store: pd.Series,
    cd: pd.Series,
    route: dict[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    start_lat = float(cd["latitude"])
    start_lon = float(cd["longitude"])
    end_lat = float(store["latitude"])
    end_lon = float(store["longitude"])

    if route is None:
        return {
            "store_id": int(store["id"]),
            "cd_latitude": start_lat,
            "cd_longitude": start_lon,
            "store_latitude": end_lat,
            "store_longitude": end_lon,
            "distance_km": None,
            "duration_min": None,
            "geometry_json": "[]",
            "status": "erro",
            "error": error or "Nao foi possivel calcular a rota real.",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    return {
        "store_id": int(store["id"]),
        "cd_latitude": start_lat,
        "cd_longitude": start_lon,
        "store_latitude": end_lat,
        "store_longitude": end_lon,
        "distance_km": route["distance_km"],
        "duration_min": route["duration_min"],
        "geometry_json": json.dumps(route["points"]),
        "status": "ok",
        "error": "",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

def calculate_and_save_real_routes(cd: pd.Series, stores: pd.DataFrame, force: bool = False) -> tuple[int, int, int]:
    routes_cache = load_routes()
    calculated = 0
    failed = 0
    skipped_cache = 0

    for _, store in stores.iterrows():
        if not has_valid_coordinates(store):
            failed += 1
            continue

        cached_route = find_saved_route(routes_cache, cd, store)
        if cached_route is not None and cached_route.get("status") == "ok" and not force:
            skipped_cache += 1
            continue

        start_lat = float(cd["latitude"])
        start_lon = float(cd["longitude"])
        end_lat = float(store["latitude"])
        end_lon = float(store["longitude"])
        route, error = fetch_street_route(start_lat, start_lon, end_lat, end_lon)
        route_row = build_route_cache_row(store, cd, route, error)
        upsert_route(route_row)

        if route is None:
            failed += 1
        else:
            calculated += 1

    return calculated, failed, skipped_cache

def coordinates_match(route_row: pd.Series, cd: pd.Series, store: pd.Series) -> bool:
    tolerance = 0.000001
    comparisons = [
        (route_row.get("cd_latitude"), cd["latitude"]),
        (route_row.get("cd_longitude"), cd["longitude"]),
        (route_row.get("store_latitude"), store["latitude"]),
        (route_row.get("store_longitude"), store["longitude"]),
    ]
    return all(abs(float(left) - float(right)) <= tolerance for left, right in comparisons)

def find_saved_route(routes: pd.DataFrame, cd: pd.Series, store: pd.Series) -> pd.Series | None:
    if routes.empty:
        return None

    candidates = routes.loc[routes["store_id"] == int(store["id"])].copy()
    if candidates.empty:
        return None

    candidates = candidates.sort_values("updated_at", ascending=False)
    for _, route_row in candidates.iterrows():
        try:
            if coordinates_match(route_row, cd, store):
                return route_row
        except (TypeError, ValueError):
            continue

    return None

def parse_route_points(geometry_json: Any) -> list[list[float]]:
    try:
        points = json.loads(str(geometry_json))
    except json.JSONDecodeError:
        return []

    if not isinstance(points, list):
        return []

    clean_points = []
    for point in points:
        if isinstance(point, list) and len(point) == 2:
            try:
                clean_points.append([float(point[0]), float(point[1])])
            except (TypeError, ValueError):
                continue
    return clean_points
