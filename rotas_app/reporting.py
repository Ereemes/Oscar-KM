from __future__ import annotations

from typing import Any

import pandas as pd

from .routing import find_saved_route, parse_route_points
from .utils import has_valid_coordinates

def compute_report(
    cd: pd.Series | None, stores: pd.DataFrame, config: dict[str, Any], routes: pd.DataFrame
) -> pd.DataFrame:
    columns = [
        "id",
        "Nome da loja",
        "Endereco",
        "Distancia ida em km",
        "Distancia considerada em km",
        "Litros estimados",
        "Custo estimado",
        "Tempo estimado min",
        "Calculo",
        "Status rota",
        "Erro rota",
        "Geometry",
    ]
    if cd is None or stores.empty:
        return pd.DataFrame(columns=columns)

    multiplier = 2 if config["ida_e_volta"] else 1
    report_rows = []
    for _, store in stores.iterrows():
        if not has_valid_coordinates(store):
            report_rows.append(
                {
                    "id": int(store["id"]),
                    "Nome da loja": store["nome"],
                    "Endereco": store["endereco"],
                    "Distancia ida em km": None,
                    "Distancia considerada em km": None,
                    "Litros estimados": None,
                    "Custo estimado": None,
                    "Tempo estimado min": None,
                    "Calculo": "Sem coordenada",
                    "Status rota": "Sem coordenada",
                    "Erro rota": str(store.get("erro_coordenada") or "Loja sem latitude/longitude. Corrija o endereco ou informe a coordenada manualmente."),
                    "Geometry": [],
                }
            )
            continue

        route = find_saved_route(routes, cd, store)

        if route is None:
            report_rows.append(
                {
                    "id": int(store["id"]),
                    "Nome da loja": store["nome"],
                    "Endereco": store["endereco"],
                    "Distancia ida em km": None,
                    "Distancia considerada em km": None,
                    "Litros estimados": None,
                    "Custo estimado": None,
                    "Tempo estimado min": None,
                    "Calculo": "Pendente",
                    "Status rota": "Pendente",
                    "Erro rota": "Clique em Recalcular rota do destino selecionado para calcular a rota real.",
                    "Geometry": [],
                }
            )
            continue

        if route.get("status") != "ok" or pd.isna(route.get("distance_km")):
            report_rows.append(
                {
                    "id": int(store["id"]),
                    "Nome da loja": store["nome"],
                    "Endereco": store["endereco"],
                    "Distancia ida em km": None,
                    "Distancia considerada em km": None,
                    "Litros estimados": None,
                    "Custo estimado": None,
                    "Tempo estimado min": None,
                    "Calculo": "Rota real nao calculada",
                    "Status rota": "Erro",
                    "Erro rota": str(route.get("error") or "Nao foi possivel calcular a rota real."),
                    "Geometry": [],
                }
            )
            continue

        one_way_distance = float(route["distance_km"])
        considered_distance = one_way_distance * multiplier
        liters = considered_distance / config["km_por_litro"]
        cost = liters * config["valor_combustivel"]
        duration_min = float(route["duration_min"]) if not pd.isna(route["duration_min"]) else None
        considered_duration = duration_min * multiplier if duration_min is not None else None

        report_rows.append(
            {
                "id": int(store["id"]),
                "Nome da loja": store["nome"],
                "Endereco": store["endereco"],
                "Distancia ida em km": round(one_way_distance, 2),
                "Distancia considerada em km": round(considered_distance, 2),
                "Litros estimados": round(liters, 2),
                "Custo estimado": round(cost, 2),
                "Tempo estimado min": round(considered_duration, 0)
                if considered_duration is not None
                else None,
                "Calculo": "Rota real via OSRM",
                "Status rota": "Calculada",
                "Erro rota": "",
                "Geometry": parse_route_points(route["geometry_json"]),
            }
        )

    return pd.DataFrame(report_rows, columns=columns)

def format_optional_number(value: Any, decimals: int = 2, suffix: str = "") -> str:
    if pd.isna(value):
        return "-"
    return f"{float(value):.{decimals}f}{suffix}"

def format_report_for_display(report: pd.DataFrame) -> pd.DataFrame:
    if report.empty:
        return report

    display_columns = [
        "Nome da loja",
        "Endereco",
        "Status rota",
        "Distancia ida em km",
        "Distancia considerada em km",
        "Litros estimados",
        "Custo estimado",
        "Tempo estimado min",
        "Calculo",
        "Erro rota",
    ]
    available_columns = [column for column in display_columns if column in report.columns]
    display_df = report[available_columns].copy()

    if "Distancia ida em km" in display_df:
        display_df["Distancia ida em km"] = display_df["Distancia ida em km"].map(
            lambda value: format_optional_number(value, suffix=" km")
        )
    if "Distancia considerada em km" in display_df:
        display_df["Distancia considerada em km"] = display_df[
            "Distancia considerada em km"
        ].map(lambda value: format_optional_number(value, suffix=" km"))
    if "Litros estimados" in display_df:
        display_df["Litros estimados"] = display_df["Litros estimados"].map(
            lambda value: format_optional_number(value, suffix=" L")
        )
    if "Tempo estimado min" in display_df:
        display_df["Tempo estimado min"] = display_df["Tempo estimado min"].map(
            lambda value: format_optional_number(value, decimals=0, suffix=" min")
        )
    if "Custo estimado" in display_df:
        display_df["Custo estimado"] = display_df["Custo estimado"].map(
            lambda value: "-" if pd.isna(value) else currency_brl(float(value))
        )

    return display_df

def currency_brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def get_current_cd(cd_df: pd.DataFrame) -> pd.Series | None:
    if cd_df.empty:
        return None
    return cd_df.iloc[0]
