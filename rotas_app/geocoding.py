from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import requests

from .addressing import build_full_address, build_geocode_candidates, unique_addresses
from .constants import (
    GEOCODING_DELAY_SECONDS,
    GEOCODING_TIMEOUT_SECONDS,
    GEOCODING_USER_AGENT,
    NOMINATIM_SEARCH_URL,
)
from .storage import ensure_storage, get_connection
from .utils import clean_text_value, has_value, normalize_text, parse_location_coordinates

def get_cached_geocode(address: str) -> tuple[float, float] | None:
    normalized_address = normalize_text(address)
    if not normalized_address:
        return None

    ensure_storage()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT latitude, longitude, status
            FROM geocoding_cache
            WHERE endereco_normalizado = ?
            """,
            (normalized_address,),
        ).fetchone()

    if row is None or row["status"] != "ok":
        return None
    if row["latitude"] is None or row["longitude"] is None:
        return None
    return float(row["latitude"]), float(row["longitude"])

def save_geocode_cache(address: str, latitude: float | None, longitude: float | None, status: str, error: str = "") -> None:
    normalized_address = normalize_text(address)
    if not normalized_address:
        return

    ensure_storage()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO geocoding_cache
            (endereco_normalizado, endereco_original, latitude, longitude, status, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endereco_normalizado) DO UPDATE SET
                endereco_original = excluded.endereco_original,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                status = excluded.status,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                normalized_address,
                address,
                latitude,
                longitude,
                status,
                error,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()

def fetch_geocode_coordinates(address: str) -> tuple[tuple[float, float] | None, str | None]:
    params = {
        "q": address,
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
        "countrycodes": "br",
    }
    headers = {"User-Agent": GEOCODING_USER_AGENT}

    try:
        response = requests.get(
            NOMINATIM_SEARCH_URL,
            params=params,
            headers=headers,
            timeout=GEOCODING_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout:
        return None, "Tempo limite excedido ao buscar latitude/longitude."
    except requests.RequestException as exc:
        return None, f"Falha de conexao ao buscar latitude/longitude: {exc}"
    except ValueError:
        return None, "Resposta invalida recebida ao buscar latitude/longitude."

    if not payload:
        return None, "Endereco nao encontrado no geocodificador."

    first_result = payload[0]
    try:
        latitude, longitude = parse_location_coordinates(
            first_result.get("lat"), first_result.get("lon")
        )
    except ValueError as exc:
        return None, str(exc)

    return (latitude, longitude), None

def geocode_address(address: str | list[str]) -> tuple[tuple[float, float] | None, str | None, bool]:
    candidates = unique_addresses(address if isinstance(address, list) else [address])
    if not candidates:
        return None, "Endereco vazio. Informe endereco, numero, cidade/UF ou CEP.", False

    errors: list[str] = []
    used_cache = False

    for clean_address in candidates:
        cached = get_cached_geocode(clean_address)
        if cached is not None:
            return cached, None, True

    for clean_address in candidates:
        # Nominatim possui limite de uso no servico publico; a pausa evita varias consultas seguidas.
        time.sleep(GEOCODING_DELAY_SECONDS)
        coordinates, error = fetch_geocode_coordinates(clean_address)
        if coordinates is None:
            error_message = error or "Endereco nao encontrado."
            errors.append(f"{clean_address}: {error_message}")
            save_geocode_cache(clean_address, None, None, "erro", error_message)
            continue

        latitude, longitude = coordinates
        save_geocode_cache(clean_address, latitude, longitude, "ok", "")
        return (latitude, longitude), None, used_cache

    if errors:
        return None, "Nao localizado nas tentativas: " + " | ".join(errors[:3]), False
    return None, "Endereco nao encontrado no geocodificador.", False

def resolve_location_data(
    name: Any,
    address: Any,
    latitude_value: Any = "",
    longitude_value: Any = "",
    number: Any = "",
    neighborhood: Any = "",
    city: Any = "",
    state: Any = "",
    cep: Any = "",
    entity_label: str = "loja",
    allow_pending: bool = False,
) -> dict[str, Any]:
    clean_name = clean_text_value(name)
    if not clean_name:
        raise ValueError(f"Nome da {entity_label} deve ser preenchido.")

    full_address = build_full_address(address, number, neighborhood, city, state, cep)
    if not full_address:
        if allow_pending:
            return {
                "nome": clean_name,
                "endereco": "",
                "latitude": None,
                "longitude": None,
                "geocoded": False,
                "status_coordenada": "pendente",
                "erro_coordenada": "Endereco vazio. Informe CEP, endereco, numero, cidade e UF.",
            }
        raise ValueError(
            f"Endereco da {entity_label} deve ser preenchido. Informe ao menos endereco completo ou CEP."
        )

    if has_value(latitude_value) and has_value(longitude_value):
        latitude, longitude = parse_location_coordinates(latitude_value, longitude_value)
        return {
            "nome": clean_name,
            "endereco": full_address,
            "latitude": latitude,
            "longitude": longitude,
            "geocoded": False,
            "status_coordenada": "ok",
            "erro_coordenada": "",
        }

    geocode_candidates = build_geocode_candidates(address, number, neighborhood, city, state, cep)
    coordinates, error, from_cache = geocode_address(geocode_candidates or full_address)
    if coordinates is None:
        if allow_pending:
            return {
                "nome": clean_name,
                "endereco": full_address,
                "latitude": None,
                "longitude": None,
                "geocoded": False,
                "status_coordenada": "pendente",
                "erro_coordenada": error or "Nao foi possivel encontrar latitude/longitude para este endereco.",
            }
        raise ValueError(
            f"Nao foi possivel encontrar latitude/longitude para: {full_address}. {error or ''}".strip()
        )

    latitude, longitude = coordinates
    return {
        "nome": clean_name,
        "endereco": full_address,
        "latitude": latitude,
        "longitude": longitude,
        "geocoded": True,
        "status_coordenada": "ok",
        "erro_coordenada": "",
    }
