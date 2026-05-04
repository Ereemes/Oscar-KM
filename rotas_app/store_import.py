from __future__ import annotations

from typing import Any

import pandas as pd

from .addressing import build_full_address
from .geocoding import resolve_location_data
from .utils import (
    clean_text_value,
    coordinate_warning_messages,
    find_existing_store_index,
    find_store_duplicate,
    has_valid_coordinates,
    has_value,
    next_store_id,
    normalize_text,
)

def get_stores_template_csv() -> bytes:
    template = pd.DataFrame(
        [
            {
                "Filial": "Loja Exemplo",
                "CEP": "01310-100",
                "Endereço": "Av. Paulista",
                "Número": "1000",
                "Bairro": "Bela Vista",
                "Cidade": "São Paulo",
                "UF": "SP",
            }
        ]
    )
    return template.to_csv(index=False).encode("utf-8-sig")

def get_stores_template_with_coordinates_csv() -> bytes:
    template = pd.DataFrame(
        [
            {
                "nome": "Loja Exemplo",
                "endereco": "Av. Paulista, 1000 - Sao Paulo/SP",
                "latitude": -23.561684,
                "longitude": -46.655981,
            }
        ]
    )
    return template.to_csv(index=False).encode("utf-8-sig")

def read_uploaded_stores_file(uploaded_file: Any) -> pd.DataFrame:
    file_name = uploaded_file.name.lower()
    if file_name.endswith(".xlsx"):
        return pd.read_excel(uploaded_file)
    return pd.read_csv(uploaded_file, sep=None, engine="python")

def normalize_import_columns(import_df: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "nome": "nome",
        "nome da loja": "nome",
        "loja": "nome",
        "filial": "nome",
        "store": "nome",
        "name": "nome",
        "endereco": "endereco",
        "logradouro": "endereco",
        "endereco da loja": "endereco",
        "address": "endereco",
        "numero": "numero",
        "num": "numero",
        "n": "numero",
        "bairro": "bairro",
        "cidade": "cidade",
        "municipio": "cidade",
        "uf": "uf",
        "estado": "uf",
        "cep": "cep",
        "zip": "cep",
        "latitude": "latitude",
        "lat": "latitude",
        "longitude": "longitude",
        "lon": "longitude",
        "lng": "longitude",
    }

    normalized_df = pd.DataFrame(index=import_df.index)
    for column in import_df.columns:
        normalized_column = normalize_text(column)
        target = aliases.get(normalized_column)
        if not target:
            continue

        values = import_df[column]
        if target not in normalized_df.columns:
            normalized_df[target] = values
        else:
            current = normalized_df[target]
            current_has_value = current.map(has_value)
            normalized_df[target] = current.where(current_has_value, values)

    supported_columns = [
        "nome",
        "endereco",
        "numero",
        "bairro",
        "cidade",
        "uf",
        "cep",
        "latitude",
        "longitude",
    ]
    for column in supported_columns:
        if column not in normalized_df.columns:
            normalized_df[column] = ""

    if "nome" not in normalized_df.columns or not normalized_df["nome"].map(has_value).any():
        raise ValueError(
            "A planilha precisa ter uma coluna de nome da loja, como 'Filial', 'Loja' ou 'nome'."
        )

    has_coordinates = normalized_df["latitude"].map(has_value).any() and normalized_df["longitude"].map(has_value).any()
    has_address_parts = (
        normalized_df["endereco"].map(has_value).any()
        or normalized_df["cep"].map(has_value).any()
    )
    if not has_coordinates and not has_address_parts:
        raise ValueError(
            "A planilha precisa ter latitude/longitude ou colunas de endereco, como CEP, Endereço, Número, Cidade e UF."
        )

    return normalized_df[supported_columns].copy()

def import_stores_from_dataframe(stores: pd.DataFrame, import_df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int], pd.DataFrame]:
    normalized_df = normalize_import_columns(import_df)
    imported_rows: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []
    current_stores = stores.copy()
    next_id = next_store_id(current_stores)
    summary = {
        "total_linhas": 0,
        "importadas_ok": 0,
        "importadas_pendentes": 0,
        "duplicadas": 0,
        "nao_importadas": 0,
    }

    for row_number, row in normalized_df.iterrows():
        readable_row = int(row_number) + 2
        if row.isna().all() or not any(has_value(row.get(column)) for column in normalized_df.columns):
            continue

        summary["total_linhas"] += 1
        original_name = clean_text_value(row.get("nome"))
        original_address = build_full_address(
            row.get("endereco"),
            row.get("numero"),
            row.get("bairro"),
            row.get("cidade"),
            row.get("uf"),
            row.get("cep"),
        )

        try:
            store_data = resolve_location_data(
                row["nome"],
                row["endereco"],
                row["latitude"],
                row["longitude"],
                number=row["numero"],
                neighborhood=row["bairro"],
                city=row["cidade"],
                state=row["uf"],
                cep=row["cep"],
                entity_label="loja",
                allow_pending=True,
            )
        except ValueError as exc:
            summary["nao_importadas"] += 1
            result_rows.append(
                {
                    "Linha": readable_row,
                    "Filial": original_name,
                    "Endereco": original_address,
                    "Status": "Nao importada",
                    "Motivo": str(exc),
                    "Latitude": None,
                    "Longitude": None,
                }
            )
            continue

        duplicate_index = find_existing_store_index(
            current_stores,
            store_data["nome"],
            store_data.get("endereco"),
        )
        duplicate_message = find_store_duplicate(
            current_stores,
            store_data["nome"],
            None,
            None,
            address=store_data.get("endereco"),
        )
        if duplicate_message and duplicate_index is not None:
            existing_store = current_stores.loc[duplicate_index]
            existing_has_coordinates = has_valid_coordinates(existing_store)
            new_has_coordinates = has_valid_coordinates(store_data)

            # Se a loja ja existia como pendente, a reimportacao pode corrigir endereco/coordenada.
            if not existing_has_coordinates:
                for column in ["nome", "endereco", "latitude", "longitude", "status_coordenada", "erro_coordenada"]:
                    current_stores.at[duplicate_index, column] = store_data.get(column)

                if new_has_coordinates:
                    summary["importadas_ok"] += 1
                    result_status = "Atualizada"
                    motivo = "Loja ja existia como pendente e agora recebeu latitude/longitude."
                else:
                    summary["importadas_pendentes"] += 1
                    result_status = "Pendente atualizada"
                    motivo = str(store_data.get("erro_coordenada") or "Loja pendente de coordenada atualizada.")

                result_rows.append(
                    {
                        "Linha": readable_row,
                        "Filial": store_data["nome"],
                        "Endereco": store_data["endereco"],
                        "Status": result_status,
                        "Motivo": motivo,
                        "Latitude": store_data.get("latitude"),
                        "Longitude": store_data.get("longitude"),
                    }
                )
                continue

            summary["duplicadas"] += 1
            result_rows.append(
                {
                    "Linha": readable_row,
                    "Filial": store_data["nome"],
                    "Endereco": store_data["endereco"],
                    "Status": "Ja existia",
                    "Motivo": duplicate_message,
                    "Latitude": store_data.get("latitude"),
                    "Longitude": store_data.get("longitude"),
                }
            )
            continue

        coordinate_warnings = []
        if has_valid_coordinates(store_data):
            coordinate_warnings = coordinate_warning_messages(
                float(store_data["latitude"]), float(store_data["longitude"])
            )

        status = str(store_data.get("status_coordenada") or "").strip().lower()
        if status == "ok" and has_valid_coordinates(store_data):
            summary["importadas_ok"] += 1
            result_status = "Importada"
            motivo = "Coordenada encontrada automaticamente." if store_data.get("geocoded") else "Importada com coordenada informada."
            if coordinate_warnings:
                motivo += " Aviso: " + " ".join(coordinate_warnings)
        else:
            summary["importadas_pendentes"] += 1
            result_status = "Pendente de coordenada"
            motivo = str(store_data.get("erro_coordenada") or "Latitude/longitude nao encontrada.")

        store_data.pop("geocoded", None)
        store_data["id"] = next_id
        next_id += 1
        imported_rows.append(store_data)
        current_stores = pd.concat([current_stores, pd.DataFrame([store_data])], ignore_index=True)
        result_rows.append(
            {
                "Linha": readable_row,
                "Filial": store_data["nome"],
                "Endereco": store_data["endereco"],
                "Status": result_status,
                "Motivo": motivo,
                "Latitude": store_data.get("latitude"),
                "Longitude": store_data.get("longitude"),
            }
        )

    stores = current_stores.copy()

    result_df = pd.DataFrame(
        result_rows,
        columns=["Linha", "Filial", "Endereco", "Status", "Motivo", "Latitude", "Longitude"],
    )
    return stores, summary, result_df
