from __future__ import annotations

import unicodedata
from typing import Any

import pandas as pd
import streamlit as st

def parse_coordinate(value: Any, field_name: str, min_value: float, max_value: float) -> float:
    if pd.isna(value):
        raise ValueError(f"{field_name} deve ser preenchida. Exemplo: -23.550520")

    normalized = str(value).strip().replace(",", ".")
    if not normalized:
        raise ValueError(f"{field_name} deve ser preenchida. Exemplo: -23.550520")

    try:
        coordinate = float(normalized)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} deve ser um numero decimal valido. Use ponto ou virgula. Exemplo: -23.550520"
        ) from exc

    if coordinate < min_value or coordinate > max_value:
        raise ValueError(f"{field_name} deve estar entre {min_value} e {max_value}.")

    return coordinate

def parse_location_coordinates(latitude_value: Any, longitude_value: Any) -> tuple[float, float]:
    latitude = parse_coordinate(latitude_value, "Latitude", -90, 90)
    longitude = parse_coordinate(longitude_value, "Longitude", -180, 180)

    if abs(latitude) < 0.000001 and abs(longitude) < 0.000001:
        raise ValueError(
            "Latitude e longitude 0,0 parecem invalidas. Confira se as coordenadas foram preenchidas corretamente."
        )

    return latitude, longitude

def coordinate_warning_messages(latitude: float, longitude: float) -> list[str]:
    warnings: list[str] = []

    # Avisos pensados para uso no Brasil, sem bloquear coordenadas internacionais.
    if -74 <= longitude <= -34 and latitude > 0:
        warnings.append(
            "Confira a latitude: para enderecos no Brasil ela geralmente deve ser negativa."
        )
    if -34 <= latitude <= 6 and longitude > 0:
        warnings.append(
            "Confira a longitude: para enderecos no Brasil ela geralmente deve ser negativa."
        )
    if latitude < -34 or latitude > 6 or longitude < -74 or longitude > -34:
        warnings.append(
            "Coordenada fora da faixa comum do Brasil. Se a loja/CD for no Brasil, confira os sinais e casas decimais."
        )

    return warnings

def show_coordinate_warnings(latitude: float, longitude: float) -> None:
    for warning in coordinate_warning_messages(latitude, longitude):
        st.warning(warning)

def normalize_text(value: Any) -> str:
    if pd.isna(value):
        return ""

    normalized = unicodedata.normalize("NFKD", str(value).strip())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(normalized.lower().split())

def optional_float_value(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None

    text = str(value).strip().replace(",", ".")
    if not text or text.lower() in {"nan", "none", "null"}:
        return None

    try:
        return float(text)
    except ValueError:
        return None

def has_valid_coordinates(row: Any) -> bool:
    try:
        latitude = optional_float_value(row.get("latitude"))
        longitude = optional_float_value(row.get("longitude"))
    except AttributeError:
        return False

    if latitude is None or longitude is None:
        return False
    if abs(latitude) < 0.000001 and abs(longitude) < 0.000001:
        return False
    return -90 <= latitude <= 90 and -180 <= longitude <= 180

def format_coordinate_for_input(value: Any) -> str:
    number = optional_float_value(value)
    return "" if number is None else str(number)

def validate_store_data(
    name: Any, address: Any, latitude_value: Any, longitude_value: Any
) -> dict[str, Any]:
    clean_name = str(name).strip() if not pd.isna(name) else ""
    clean_address = str(address).strip() if not pd.isna(address) else ""

    if not clean_name:
        raise ValueError("Nome da loja deve ser preenchido.")
    if not clean_address:
        raise ValueError("Endereco da loja deve ser preenchido.")

    latitude, longitude = parse_location_coordinates(latitude_value, longitude_value)

    return {
        "nome": clean_name,
        "endereco": clean_address,
        "latitude": latitude,
        "longitude": longitude,
    }

def find_existing_store_index(stores: pd.DataFrame, name: str, address: str | None = None) -> Any:
    """Retorna o índice da loja existente por nome ou endereço normalizado."""
    if stores.empty:
        return None

    normalized_name = normalize_text(name)
    if normalized_name and "nome" in stores.columns:
        matches = stores["nome"].map(normalize_text).eq(normalized_name)
        if matches.any():
            return matches[matches].index[0]

    normalized_address = normalize_text(address or "")
    if normalized_address and "endereco" in stores.columns:
        matches = stores["endereco"].map(normalize_text).eq(normalized_address)
        if matches.any():
            return matches[matches].index[0]

    return None

def find_store_duplicate(
    stores: pd.DataFrame,
    name: str,
    latitude: float | None = None,
    longitude: float | None = None,
    ignore_id: int | None = None,
    address: str | None = None,
) -> str | None:
    if stores.empty:
        return None

    candidates = stores.copy()
    if ignore_id is not None:
        candidates = candidates.loc[candidates["id"] != ignore_id]

    normalized_name = normalize_text(name)
    duplicated_name = candidates["nome"].map(normalize_text).eq(normalized_name)
    if duplicated_name.any():
        return "Ja existe uma loja cadastrada com esse nome."

    normalized_address = normalize_text(address or "")
    if normalized_address:
        duplicated_address = candidates["endereco"].map(normalize_text).eq(normalized_address)
        if duplicated_address.any():
            return "Ja existe uma loja cadastrada com esse endereco."

    latitude_number = optional_float_value(latitude)
    longitude_number = optional_float_value(longitude)
    if latitude_number is not None and longitude_number is not None:
        candidates_with_coordinates = candidates.loc[
            candidates["latitude"].notna() & candidates["longitude"].notna()
        ].copy()
        if not candidates_with_coordinates.empty:
            duplicated_coordinates = (
                (pd.to_numeric(candidates_with_coordinates["latitude"], errors="coerce").sub(latitude_number).abs() < 0.000001)
                & (pd.to_numeric(candidates_with_coordinates["longitude"], errors="coerce").sub(longitude_number).abs() < 0.000001)
            )
            if duplicated_coordinates.any():
                return "Ja existe uma loja cadastrada com essas coordenadas."

    return None

def clean_text_value(value: Any) -> str:
    if pd.isna(value):
        return ""

    if isinstance(value, float) and value.is_integer():
        return str(int(value)).strip()

    return str(value).strip()

def clean_cep(value: Any) -> str:
    text = clean_text_value(value)
    if not text:
        return ""

    digits = "".join(char for char in text if char.isdigit())
    if len(digits) == 8:
        return f"{digits[:5]}-{digits[5:]}"
    if digits:
        return digits
    return text

def has_value(value: Any) -> bool:
    return bool(clean_text_value(value))

def next_store_id(stores: pd.DataFrame) -> int:
    if stores.empty:
        return 1
    return int(stores["id"].max()) + 1
