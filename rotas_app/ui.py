from __future__ import annotations

import re
from datetime import datetime
from html import escape
from textwrap import dedent

import folium
import pandas as pd
import streamlit as st
from branca.element import MacroElement, Template
from streamlit_folium import st_folium

from .addressing import build_full_address, expand_address_abbreviations
from .constants import APP_TITLE, DEFAULT_CONFIG
from .geocoding import resolve_location_data
from .reporting import (
    compute_report,
    currency_brl,
    format_optional_number,
    format_report_for_display,
    get_current_cd,
)
from .routing import calculate_and_save_real_routes
from .storage import (
    delete_routes_for_store,
    ensure_storage,
    load_cd,
    load_config,
    load_routes,
    load_stores,
    save_cd,
    save_config,
    save_stores,
)
from .store_import import import_stores_from_dataframe, read_uploaded_stores_file
from .utils import (
    clean_cep,
    clean_text_value,
    coordinate_warning_messages,
    find_store_duplicate,
    format_coordinate_for_input,
    has_valid_coordinates,
    has_value,
    next_store_id,
    normalize_text,
)

CONFIRMED_ACTIVITIES_KEY = "map_confirmed_activity_ids"
CONFIG_WIDGET_DRAFT_KEYS = {
    "config_km_l": "config_km_l_draft",
    "config_fuel_value": "config_fuel_value_draft",
    "config_round_trip": "config_round_trip_draft",
}


def format_decimal_input(value: float, decimals: int = 1, prefix: str = "") -> str:
    formatted = f"{float(value):.{decimals}f}".replace(".", ",")
    return f"{prefix}{formatted}" if prefix else formatted


def parse_decimal_input(value: object, field_name: str, min_value: float) -> float:
    text = str(value or "").strip()
    cleaned = re.sub(r"[^0-9,.\-]", "", text)
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")

    try:
        number = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"{field_name} deve ser um numero valido.") from exc

    if number < min_value:
        raise ValueError(f"{field_name} deve ser maior que zero.")
    return number


def split_saved_address(address: str) -> dict[str, str]:
    parts = [part.strip() for part in str(address or "").split(",") if part.strip()]
    result = {"address": "", "number": "", "neighborhood": "", "city": "", "state": "", "cep": ""}
    if not parts:
        return result

    street = parts[0]
    street_match = re.match(r"^(?P<street>.*?)(?:,\s*)?(?P<number>\d+[A-Za-z]?)?$", street)
    if street_match:
        result["address"] = street_match.group("street").strip(" ,-") or street
        result["number"] = (street_match.group("number") or "").strip()
    else:
        result["address"] = street

    remaining = parts[1:]
    if remaining:
        number_only = re.match(r"^(?P<number>\d+[A-Za-z]?)$", remaining[0])
        if number_only:
            result["number"] = result["number"] or number_only.group("number").strip()
            remaining = remaining[1:]

    if remaining:
        number_neighborhood = re.match(r"^(?P<number>\d+[A-Za-z]?)\s*-\s*(?P<neighborhood>.+)$", remaining[0])
        if number_neighborhood:
            result["number"] = result["number"] or number_neighborhood.group("number").strip()
            result["neighborhood"] = number_neighborhood.group("neighborhood").strip()
            remaining = remaining[1:]

    if remaining:
        maybe_neighborhood = remaining[0]
        if not re.fullmatch(r"\d+[A-Za-z]?", maybe_neighborhood) and " - " not in maybe_neighborhood and not re.search(r"\b[A-Z]{2}\b", maybe_neighborhood):
            result["neighborhood"] = maybe_neighborhood
            remaining = remaining[1:]

    for part in remaining:
        city_state_match = re.search(r"(?P<city>[^,-]+)\s*-\s*(?P<state>[A-Za-z]{2})", part)
        if city_state_match:
            result["city"] = city_state_match.group("city").strip()
            result["state"] = city_state_match.group("state").upper()
        cep_match = re.search(r"\b\d{5}-?\d{3}\b", part)
        if cep_match:
            cep = cep_match.group(0)
            result["cep"] = cep if "-" in cep else f"{cep[:5]}-{cep[5:]}"

    return result


def normalize_cd_cep_value(value: object) -> str:
    return clean_cep(value)


def is_valid_cd_cep_input(value: object) -> bool:
    text = clean_text_value(value)
    if not text:
        return True
    return bool(re.fullmatch(r"\d{8}|\d{5}-\d{3}", text))


def is_valid_cd_number_input(value: object) -> bool:
    text = clean_text_value(value)
    if not text:
        return False
    return text.isdigit()


def normalize_cd_cep() -> None:
    raw_value = clean_text_value(st.session_state.get("cd_cep", ""))
    if raw_value and not is_valid_cd_cep_input(raw_value):
        st.session_state["cd_cep_notice"] = "CEP inválido. Use o formato 00000-000."
    else:
        st.session_state.pop("cd_cep_notice", None)
    st.session_state.pop("cd_form_notice", None)


def normalize_cd_number_value(value: object) -> str:
    text = clean_text_value(value)
    return "".join(char for char in text if char.isdigit())


def normalize_cd_number() -> None:
    raw_value = clean_text_value(st.session_state.get("cd_number", ""))
    if raw_value and not is_valid_cd_number_input(raw_value):
        st.session_state["cd_number_notice"] = "Número inválido. Use apenas números."
    else:
        st.session_state.pop("cd_number_notice", None)
    st.session_state.pop("cd_form_notice", None)


def normalize_cd_address() -> None:
    value = clean_text_value(st.session_state.get("cd_address", ""))
    value = re.sub(r"_+", " ", value)
    value = re.sub(r"[^\w\s\-\.,/ºª]", "", value, flags=re.UNICODE)
    st.session_state["cd_address"] = expand_address_abbreviations(" ".join(value.split()))
    st.session_state.pop("cd_form_notice", None)


def normalize_cd_neighborhood() -> None:
    value = clean_text_value(st.session_state.get("cd_neighborhood", ""))
    value = re.sub(r"_+", " ", value)
    value = re.sub(r"[^\w\s\-.ºª]", "", value, flags=re.UNICODE)
    st.session_state["cd_neighborhood"] = expand_address_abbreviations(" ".join(value.split()))
    st.session_state.pop("cd_form_notice", None)


def normalize_cd_city() -> None:
    value = clean_text_value(st.session_state.get("cd_city", ""))
    value = re.sub(r"_+", " ", value)
    value = re.sub(r"[^\w\s\-.']", "", value, flags=re.UNICODE)
    st.session_state["cd_city"] = " ".join(value.split())
    st.session_state.pop("cd_form_notice", None)


def validate_cd_form_inputs() -> None:
    cep_digits = "".join(char for char in clean_text_value(st.session_state.get("cd_cep", "")) if char.isdigit())
    if cep_digits and len(cep_digits) != 8:
        raise ValueError("CEP deve estar no formato 00000-000.")

    if has_value(st.session_state.get("cd_number")) and not str(st.session_state.get("cd_number", "")).isdigit():
        raise ValueError("Numero deve conter apenas digitos.")


def normalize_cd_address_value_for_save(value: object) -> str:
    text = clean_text_value(value)
    text = re.sub(r"_+", " ", text)
    text = re.sub(r"[^\w\s\-\.,/ÂºÂª]", "", text, flags=re.UNICODE)
    return expand_address_abbreviations(" ".join(text.split()))


def normalize_cd_neighborhood_value_for_save(value: object) -> str:
    text = clean_text_value(value)
    text = re.sub(r"_+", " ", text)
    text = re.sub(r"[^\w\s\-.ÂºÂª]", "", text, flags=re.UNICODE)
    return expand_address_abbreviations(" ".join(text.split()))


def normalize_cd_city_value_for_save(value: object) -> str:
    text = clean_text_value(value)
    text = re.sub(r"_+", " ", text)
    text = re.sub(r"[^\w\s\-.']", "", text, flags=re.UNICODE)
    return " ".join(text.split())


def validate_cd_form_values(cep_value: object, number_value: object) -> None:
    if not is_valid_cd_cep_input(cep_value):
        raise ValueError("CEP deve estar no formato 00000-000.")

    if has_value(number_value) and not is_valid_cd_number_input(number_value):
        raise ValueError("Numero deve conter apenas digitos.")


def validate_cd_location_inputs(
    address_value: object,
    number_value: object,
    city_value: object,
    state_value: object,
    cep_value: object,
    latitude_value: object,
    longitude_value: object,
) -> None:
    has_lat = has_value(latitude_value)
    has_lon = has_value(longitude_value)
    if has_lat != has_lon:
        raise ValueError("Informe latitude e longitude juntas ou deixe ambas em branco.")

    address_text = clean_text_value(address_value)
    number_text = clean_text_value(number_value)
    city_text = clean_text_value(city_value)
    state_text = clean_text_value(state_value).upper()
    cep_digits = "".join(char for char in clean_text_value(cep_value) if char.isdigit())

    if not address_text:
        raise ValueError("Informe o endereco do CD.")
    if not number_text:
        raise ValueError("Informe o numero do endereco do CD.")
    if not city_text:
        raise ValueError("Informe a cidade do CD.")
    if len(state_text) != 2:
        raise ValueError("Selecione a UF do CD.")

    # Se o CEP for informado, ele precisa ser brasileiro valido.
    if cep_digits and len(cep_digits) != 8:
        raise ValueError("CEP deve estar no formato 00000-000.")

    # Evita aceitar textos aleatorios muito curtos como endereco/cidade.
    address_tokens = [token for token in re.split(r"\s+", address_text) if re.search(r"[A-Za-zÀ-ÿ]", token)]
    address_letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", address_text)
    city_letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", city_text)
    if len(address_letters) < 3 or len(address_tokens) < 2:
        raise ValueError("Informe um endereco valido para o CD.")
    if len(city_letters) < 2:
        raise ValueError("Informe uma cidade valida para o CD.")


def build_cd_location_signature(
    cep_value: object,
    address_value: object,
    number_value: object,
    neighborhood_value: object,
    city_value: object,
    state_value: object,
) -> tuple[str, str, str, str, str, str]:
    return (
        clean_cep(cep_value),
        normalize_cd_address_value_for_save(address_value),
        clean_text_value(number_value),
        normalize_cd_neighborhood_value_for_save(neighborhood_value),
        normalize_cd_city_value_for_save(city_value),
        clean_text_value(state_value).upper(),
    )


def should_discard_stale_cd_coordinates(
    current_location_signature: tuple[str, str, str, str, str, str],
    latitude_value: object,
    longitude_value: object,
) -> bool:
    current_lat = clean_text_value(latitude_value)
    current_lon = clean_text_value(longitude_value)
    if not current_lat or not current_lon:
        return False

    loaded_location_signature = tuple(st.session_state.get("cd_loaded_location_signature", ()))
    loaded_coordinate_signature = tuple(st.session_state.get("cd_loaded_coordinate_signature", ()))
    if len(loaded_location_signature) != 6 or len(loaded_coordinate_signature) != 2:
        return False

    if current_location_signature == loaded_location_signature:
        return False

    return (current_lat, current_lon) == loaded_coordinate_signature


def refresh_cd_inline_notices() -> None:
    raw_cep = clean_text_value(st.session_state.get("cd_cep", ""))
    if raw_cep and not is_valid_cd_cep_input(raw_cep):
        st.session_state["cd_cep_notice"] = "CEP inválido. Use o formato 00000-000."
    else:
        st.session_state.pop("cd_cep_notice", None)

    raw_number = clean_text_value(st.session_state.get("cd_number", ""))
    if raw_number and not is_valid_cd_number_input(raw_number):
        st.session_state["cd_number_notice"] = "Número inválido. Use apenas números."
    else:
        st.session_state.pop("cd_number_notice", None)


def is_cd_address_lookup_error(message: str) -> bool:
    normalized = normalize_text(message)
    patterns = (
        "nao foi possivel encontrar latitude longitude",
        "nao localizado nas tentativas",
        "endereco nao encontrado",
        "nao existe",
    )
    return any(pattern in normalized for pattern in patterns)


def current_cd_form_values() -> dict[str, str]:
    return {
        "name": clean_text_value(st.session_state.get("cd_name", "")),
        "cep": clean_cep(st.session_state.get("cd_cep", "")),
        "raw_cep": clean_text_value(st.session_state.get("cd_cep", "")),
        "address": normalize_cd_address_value_for_save(st.session_state.get("cd_address", "")),
        "number": clean_text_value(st.session_state.get("cd_number", "")),
        "neighborhood": normalize_cd_neighborhood_value_for_save(st.session_state.get("cd_neighborhood", "")),
        "city": normalize_cd_city_value_for_save(st.session_state.get("cd_city", "")),
        "state": clean_text_value(st.session_state.get("cd_state", "")).upper(),
        "lat": clean_text_value(st.session_state.get("cd_lat", "")),
        "lon": clean_text_value(st.session_state.get("cd_lon", "")),
    }


def cd_form_signature_from_values(values: dict[str, str]) -> tuple[str, str, str, str, str, str, str, str, str]:
    return (
        values["name"],
        values["cep"],
        values["address"],
        values["number"],
        values["neighborhood"],
        values["city"],
        values["state"],
        values["lat"],
        values["lon"],
    )


def cd_saved_signature_from_values(
    current_name: str,
    parsed_address: dict[str, str],
    current_lat: str,
    current_lon: str,
) -> tuple[str, str, str, str, str, str, str, str, str]:
    return (
        clean_text_value(current_name),
        clean_cep(parsed_address.get("cep", "")),
        normalize_cd_address_value_for_save(parsed_address.get("address", "")),
        clean_text_value(parsed_address.get("number", "")),
        normalize_cd_neighborhood_value_for_save(parsed_address.get("neighborhood", "")),
        normalize_cd_city_value_for_save(parsed_address.get("city", "")),
        clean_text_value(parsed_address.get("state", "")).upper(),
        clean_text_value(current_lat),
        clean_text_value(current_lon),
    )


def cd_form_blocker(values: dict[str, str]) -> str:
    if not values["name"]:
        return "Informe o nome do CD."
    if values["raw_cep"] and not is_valid_cd_cep_input(values["raw_cep"]):
        return "Corrija o CEP antes de salvar."
    if not values["address"]:
        return "Informe o endereço do CD."
    if not values["number"]:
        return "Informe o número do endereço."
    if not is_valid_cd_number_input(values["number"]):
        return "Corrija o número antes de salvar."
    if not values["city"]:
        return "Informe a cidade do CD."
    if len(values["state"]) != 2:
        return "Selecione a UF do CD."
    if bool(values["lat"]) != bool(values["lon"]):
        return "Informe latitude e longitude juntas ou deixe ambas em branco."
    return ""


def cd_form_preview_address(values: dict[str, str]) -> str:
    address = build_full_address(
        values["address"],
        values["number"],
        values["neighborhood"],
        values["city"],
        values["state"],
        values["cep"],
    )
    return address.replace(", Brasil", "") if address else "Preencha endereço, número, cidade e UF para visualizar a prévia."


def cd_coordinate_status_html(
    values: dict[str, str],
    location_signature: tuple[str, str, str, str, str, str],
) -> str:
    has_lat = bool(values["lat"])
    has_lon = bool(values["lon"])
    if has_lat != has_lon:
        title = "Coordenadas incompletas"
        detail = "Informe latitude e longitude juntas ou deixe ambas em branco."
        kind = "warning"
    elif has_lat and should_discard_stale_cd_coordinates(location_signature, values["lat"], values["lon"]):
        title = "Coordenadas antigas"
        detail = "O endereço foi alterado; ao salvar, o sistema buscará novas coordenadas."
        kind = "warning"
    elif has_lat and has_lon:
        title = "Coordenadas manuais"
        detail = "O sistema usará a latitude e longitude informadas."
        kind = "success"
    else:
        title = "Busca automática"
        detail = "Ao salvar, o sistema buscará as coordenadas pelo endereço."
        kind = "neutral"

    icon = "crosshair" if kind != "neutral" else "map-pin"
    color = "orange" if kind == "warning" else "green" if kind == "success" else "blue"
    return (
        f'<div class="cd-coordinate-status {kind}">'
        f'{mini_icon(icon, color)}'
        '<div>'
        f'<div class="cd-coordinate-title">{escape(title)}</div>'
        f'<div class="cd-coordinate-detail">{escape(detail)}</div>'
        '</div>'
        '</div>'
    )


def stores_editor_legacy(stores: pd.DataFrame) -> None:
    st.subheader("Cadastro de lojas")

    with st.expander("Importar lojas por planilha", expanded=False):
        st.write(
            "Voce pode importar uma planilha com Filial, CEP, Endereço, Número, Bairro, Cidade e UF. "
            "Se latitude/longitude nao existirem, o sistema busca automaticamente pelo endereco e salva em cache."
        )
        st.info(
            "Importe sua planilha Excel no formato usado atualmente: Filial, CEP, Endereço, Número, Bairro, Cidade e UF. "
            "Nao e necessario informar latitude e longitude."
        )
        uploaded_file = st.file_uploader(
            "Selecionar arquivo CSV ou Excel",
            type=["csv", "xlsx"],
            key="stores_upload",
        )

        if st.button("Importar lojas", disabled=uploaded_file is None):
            try:
                import_df = read_uploaded_stores_file(uploaded_file)
                with st.spinner("Importando planilha e buscando coordenadas quando possivel..."):
                    updated_stores, import_summary, import_result = import_stores_from_dataframe(
                        stores, import_df
                    )

                total_new = import_summary["importadas_ok"] + import_summary["importadas_pendentes"]
                if total_new:
                    save_stores(updated_stores)
                    stores = updated_stores
                    st.success(f"{total_new} loja(s) adicionada(s) ao cadastro.")
                else:
                    st.warning("Nenhuma loja nova foi adicionada ao cadastro.")

                col_total, col_ok, col_pending, col_dup, col_fail = st.columns(5)
                col_total.metric("Linhas lidas", import_summary["total_linhas"])
                col_ok.metric("Com coordenada", import_summary["importadas_ok"])
                col_pending.metric("Pendentes", import_summary["importadas_pendentes"])
                col_dup.metric("Ja existiam", import_summary["duplicadas"])
                col_fail.metric("Nao importadas", import_summary["nao_importadas"])

                if import_summary["importadas_pendentes"]:
                    st.warning(
                        "Algumas lojas foram salvas como pendentes de coordenada. Elas aparecem no cadastro, "
                        "mas so entram no mapa depois de corrigir endereco ou informar latitude/longitude."
                    )

                if not import_result.empty:
                    st.markdown("#### Resultado da importacao")
                    st.dataframe(import_result, use_container_width=True, hide_index=True)
            except Exception as exc:
                st.error(f"Nao foi possivel importar a planilha: {exc}")

    st.markdown("#### Cadastrar loja manualmente")
    with st.form("form_cadastrar_loja", clear_on_submit=True):
        st.caption(
            "Latitude e longitude sao opcionais. Se deixar em branco, o sistema tenta buscar pelo endereco informado."
        )
        col_name, col_cep = st.columns([2, 1])
        with col_name:
            name = st.text_input("Nome da loja")
        with col_cep:
            cep = st.text_input("CEP", placeholder="01310-100")

        col_address, col_number = st.columns([3, 1])
        with col_address:
            address = st.text_input("Endereco / Logradouro", placeholder="Av. Paulista")
        with col_number:
            number = st.text_input("Numero", placeholder="1000")

        col_neighborhood, col_city, col_state = st.columns([2, 2, 1])
        with col_neighborhood:
            neighborhood = st.text_input("Bairro", placeholder="Bela Vista")
        with col_city:
            city = st.text_input("Cidade", placeholder="Sao Paulo")
        with col_state:
            state = st.text_input("UF", placeholder="SP")

        col_lat, col_lon = st.columns(2)
        with col_lat:
            lat = st.text_input("Latitude opcional", placeholder="-23.550520")
        with col_lon:
            lon = st.text_input("Longitude opcional", placeholder="-46.633308")

        submitted = st.form_submit_button("Salvar loja")

    if submitted:
        try:
            with st.spinner("Validando loja e buscando coordenadas quando necessario..."):
                store_data = resolve_location_data(
                    name,
                    address,
                    lat,
                    lon,
                    number=number,
                    neighborhood=neighborhood,
                    city=city,
                    state=state,
                    cep=cep,
                    entity_label="loja",
                )
            duplicate_message = find_store_duplicate(
                stores,
                store_data["nome"],
                store_data["latitude"],
                store_data["longitude"],
                address=store_data["endereco"],
            )
            if duplicate_message:
                st.error(duplicate_message)
                return

            store_data.pop("geocoded", None)
            new_store = pd.DataFrame(
                [
                    {
                        "id": next_store_id(stores),
                        **store_data,
                    }
                ]
            )
            save_stores(pd.concat([stores, new_store], ignore_index=True))
            warnings = coordinate_warning_messages(store_data["latitude"], store_data["longitude"])
            if warnings:
                st.session_state["coordinate_warning"] = "Loja salva. " + " ".join(warnings)
            st.success("Loja cadastrada com sucesso.")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    st.divider()
    st.subheader("Lojas cadastradas")

    if stores.empty:
        st.info("Nenhuma loja cadastrada ainda.")
        return

    filter_col_search, filter_col_coord, filter_col_sort = st.columns([2, 1, 1])
    with filter_col_search:
        search_term = st.text_input(
            "Buscar loja por nome ou endereco",
            placeholder="Digite parte do nome ou endereco",
        )
    with filter_col_coord:
        coordinate_filter = st.selectbox(
            "Filtro de coordenadas",
            options=["Todas", "Com coordenada", "Pendentes de coordenada", "Com aviso", "Sem aviso"],
            help="Pendentes sao lojas importadas, mas que ainda nao tiveram latitude/longitude encontrada.",
        )
    with filter_col_sort:
        sort_option = st.selectbox(
            "Ordenar",
            options=["ID", "Nome A-Z", "Nome Z-A", "Endereco A-Z"],
        )

    filtered_stores = stores.copy()
    if search_term.strip():
        normalized_search = normalize_text(search_term)
        filtered_stores = filtered_stores.loc[
            filtered_stores["nome"].map(normalize_text).str.contains(normalized_search, na=False, regex=False)
            | filtered_stores["endereco"].map(normalize_text).str.contains(normalized_search, na=False, regex=False)
        ]

    if coordinate_filter != "Todas" and not filtered_stores.empty:
        has_coordinates = filtered_stores.apply(has_valid_coordinates, axis=1)
        has_warning = filtered_stores.apply(
            lambda row: bool(coordinate_warning_messages(float(row["latitude"]), float(row["longitude"])))
            if has_valid_coordinates(row)
            else False,
            axis=1,
        )
        if coordinate_filter == "Com coordenada":
            filtered_stores = filtered_stores.loc[has_coordinates]
        elif coordinate_filter == "Pendentes de coordenada":
            filtered_stores = filtered_stores.loc[~has_coordinates]
        elif coordinate_filter == "Com aviso":
            filtered_stores = filtered_stores.loc[has_warning]
        elif coordinate_filter == "Sem aviso":
            filtered_stores = filtered_stores.loc[has_coordinates & ~has_warning]

    if sort_option == "Nome A-Z":
        filtered_stores = filtered_stores.sort_values("nome", key=lambda series: series.map(normalize_text))
    elif sort_option == "Nome Z-A":
        filtered_stores = filtered_stores.sort_values("nome", key=lambda series: series.map(normalize_text), ascending=False)
    elif sort_option == "Endereco A-Z":
        filtered_stores = filtered_stores.sort_values("endereco", key=lambda series: series.map(normalize_text))
    else:
        filtered_stores = filtered_stores.sort_values("id")

    st.caption(f"Mostrando {len(filtered_stores)} de {len(stores)} loja(s).")

    display_df = filtered_stores.rename(
        columns={
            "id": "ID",
            "nome": "Nome",
            "endereco": "Endereco",
            "latitude": "Latitude",
            "longitude": "Longitude",
            "status_coordenada": "Status coordenada",
            "erro_coordenada": "Observacao coordenada",
        }
    )
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    if filtered_stores.empty:
        st.info("Nenhuma loja encontrada com esse filtro.")
        return

    st.markdown("#### Editar ou excluir loja")
    selected_option = st.selectbox(
        "Selecionar loja",
        options=[str(store_id) for store_id in filtered_stores["id"].tolist()],
        format_func=lambda store_id: str(
            stores.loc[stores["id"] == int(store_id), "nome"].iloc[0]
        ),
    )
    selected_id = int(selected_option)
    selected_store = stores.loc[stores["id"] == selected_id].iloc[0]

    with st.form("form_editar_loja"):
        col_edit_name, col_edit_address = st.columns([1, 2])
        with col_edit_name:
            edit_name = st.text_input(
                "Nome da loja", value=str(selected_store["nome"]), key="edit_name"
            )
        with col_edit_address:
            edit_address = st.text_input(
                "Endereco", value=str(selected_store["endereco"]), key="edit_address"
            )

        col_edit_lat, col_edit_lon = st.columns(2)
        with col_edit_lat:
            edit_lat = st.text_input(
                "Latitude", value=format_coordinate_for_input(selected_store["latitude"]), key="edit_lat"
            )
        with col_edit_lon:
            edit_lon = st.text_input(
                "Longitude", value=format_coordinate_for_input(selected_store["longitude"]), key="edit_lon"
            )

        confirm_delete = st.checkbox(
            "Confirmo que quero excluir esta loja",
            help="Marque esta opcao antes de clicar em Excluir loja.",
        )
        save_button, delete_button = st.columns(2)
        save_clicked = save_button.form_submit_button("Salvar alteracoes")
        delete_clicked = delete_button.form_submit_button("Excluir loja")

    if save_clicked:
        try:
            with st.spinner("Validando loja e buscando coordenadas quando necessario..."):
                store_data = resolve_location_data(
                    edit_name,
                    edit_address,
                    edit_lat,
                    edit_lon,
                    entity_label="loja",
                )
            duplicate_message = find_store_duplicate(
                stores,
                store_data["nome"],
                store_data["latitude"],
                store_data["longitude"],
                ignore_id=selected_id,
                address=store_data["endereco"],
            )
            if duplicate_message:
                st.error(duplicate_message)
                return

            store_data.pop("geocoded", None)
            updated_stores = stores.copy()
            store_index = updated_stores.index[updated_stores["id"] == selected_id][0]
            updated_stores.loc[
                store_index,
                ["nome", "endereco", "latitude", "longitude", "status_coordenada", "erro_coordenada"],
            ] = [
                store_data["nome"],
                store_data["endereco"],
                store_data["latitude"],
                store_data["longitude"],
                store_data.get("status_coordenada", "ok"),
                store_data.get("erro_coordenada", ""),
            ]
            save_stores(updated_stores)
            warnings = coordinate_warning_messages(store_data["latitude"], store_data["longitude"])
            if warnings:
                st.session_state["coordinate_warning"] = "Loja atualizada. " + " ".join(warnings)
            st.success("Loja atualizada com sucesso.")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    if delete_clicked:
        if not confirm_delete:
            st.error("Marque a confirmacao antes de excluir a loja.")
            return

        updated_stores = stores.loc[stores["id"] != selected_id].copy()
        save_stores(updated_stores)
        delete_routes_for_store(selected_id)
        st.success("Loja excluida com sucesso.")
        st.rerun()


STORE_FORM_KEYS = {
    "name": "store_form_name",
    "cep": "store_form_cep",
    "address": "store_form_address",
    "number": "store_form_number",
    "neighborhood": "store_form_neighborhood",
    "city": "store_form_city",
    "state": "store_form_state",
    "lat": "store_form_lat",
    "lon": "store_form_lon",
}


def render_stores_styles() -> None:
    st.markdown(
        """
        <style>
        .stores-page { color: var(--summary-ink); }
        .stores-title { font-size: 32px; line-height: 1.1; font-weight: 800; margin: 2px 0 8px; }
        .stores-subtitle { color: #40588c; font-size: 15px; margin-bottom: 20px; }
        .stores-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px; margin-bottom: 20px; }
        .stores-stat-card, .st-key-stores_import_panel, .st-key-stores_form_panel, .st-key-stores_table_panel {
            border: 1px solid #d8e2f0; border-radius: 8px; background: #ffffff; box-shadow: 0 8px 18px rgba(12, 32, 68, 0.05);
        }
        .stores-stat-card { min-height: 104px; display: grid; grid-template-columns: 82px 1fr; align-items: center; padding: 18px 22px; }
        .stores-stat-label { color: #18336f; font-size: 14px; font-weight: 600; margin-bottom: 8px; }
        .stores-stat-value { color: var(--summary-ink); font-size: 25px; line-height: 1; font-weight: 800; }
        .st-key-stores_import_panel, .st-key-stores_form_panel, .st-key-stores_table_panel { padding: 22px; }
        .st-key-stores_form_panel { min-height: 550px; }
        .st-key-stores_import_panel { position: relative; }
        .stores-panel-title, .stores-table-title { color: var(--summary-ink); font-size: 18px; font-weight: 800; margin-bottom: 16px; }
        .stores-dropzone {
            min-height: 120px; display: grid; place-items: center; text-align: center; border: 1.5px dashed #c3cee0;
            border-radius: 8px; color: #263f73; margin: 0 0 10px; padding: 14px;
        }
        .stores-excel {
            width: 42px; height: 50px; display: inline-grid; place-items: center; border-radius: 8px; background: #11a34a;
            color: #ffffff; font-size: 23px; font-weight: 800; margin-bottom: 8px; box-shadow: 0 10px 18px rgba(17, 163, 74, 0.22);
        }
        .stores-import-copy { max-width: 420px; margin: 8px auto; color: #263f73; text-align: center; font-size: 14px; line-height: 1.4; }
        .stores-import-note, .stores-import-result {
            display: flex; align-items: center; justify-content: center; gap: 8px; color: #334d80; font-size: 14px; font-weight: 600; margin-top: 8px;
        }
        .stores-import-info-icon {
            width: 18px;
            height: 18px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border: 2px solid #334d80;
            border-radius: 50%;
            color: #334d80;
            font-size: 12px;
            font-weight: 800;
            line-height: 1;
        }
        .stores-import-warning {
            margin-top: 12px; padding: 11px 14px; border-radius: 8px; background: #fff8df; color: #8a5d00;
            font-size: 14px; line-height: 1.35;
        }
        .stores-import-result { justify-content: flex-start; padding: 14px; border: 1px solid #bde8d2; border-radius: 8px; background: #f0fbf5; color: #183c61; }
        .stores-editing-banner, .stores-editing-table-notice {
            display: flex; align-items: center; gap: 10px; border-radius: 8px; border: 1px solid #b7d2ff;
            background: #eef5ff; color: #163a70; font-size: 13px; font-weight: 700; line-height: 1.35;
        }
        .stores-editing-banner { padding: 12px 14px; margin: -4px 0 14px; }
        .stores-editing-table-notice { padding: 10px 12px; margin: 10px 0 12px; }
        .stores-row-edit-pill {
            display: inline-flex; align-items: center; border-radius: 999px; background: #e8f1ff; color: #0b63f6;
            font-size: 11px; font-weight: 800; padding: 4px 8px; margin-top: 5px;
        }
        .stores-table-head {
            display: grid; grid-template-columns: 1.1fr 1.75fr 0.95fr 0.75fr 0.7fr; gap: 16px; align-items: center;
            min-height: 38px; color: #40588c; font-size: 13px; font-weight: 700; border: 1px solid #dce4f0;
            border-radius: 8px 8px 0 0; padding: 0 10px; margin-top: 8px;
        }
        div[class*="st-key-store_row_"] {
            min-height: 42px; color: #18336f; font-size: 13px; border-left: 1px solid #dce4f0;
            border-right: 1px solid #dce4f0; border-bottom: 1px solid #dce4f0; padding: 6px 10px;
        }
        div[class*="st-key-store_row_"] [data-testid="stMarkdownContainer"] p { margin: 0; font-size: 13px; }
        .stores-row-name { color: var(--summary-ink); font-weight: 800; }
        .stores-status-pill {
            display: inline-flex; min-width: 76px; justify-content: center; border-radius: 999px; padding: 6px 11px;
            font-size: 12px; font-weight: 800;
        }
        .stores-status-pill.active { color: #0a8b41; background: #dcf6e8; }
        .stores-status-pill.pending { color: #f08a00; background: #fff1d6; }
        .stores-count-caption {
            color: #5b6f9c;
            font-size: 14px;
            line-height: 36px;
        }
        .st-key-stores_pagination {
            margin-top: 14px;
        }
        .st-key-stores_pagination [data-testid="stHorizontalBlock"] {
            width: 100%;
            justify-content: space-between;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
        }
        .st-key-stores_pagination [data-testid="stColumn"] {
            min-width: 0 !important;
        }
        .st-key-stores_pagination .stButton button {
            min-height: 36px;
            height: 36px;
            border-radius: 7px;
            border-color: #0b63f6 !important;
            color: #0b63f6 !important;
            font-size: 13px;
            font-weight: 800;
            white-space: nowrap;
        }
        .st-key-stores_pagination .stButton button:hover {
            border-color: #084fc4 !important;
            color: #084fc4 !important;
        }
        .st-key-stores_pagination .stButton button:disabled {
            border-color: #c8d3e4 !important;
            color: #8da0bf !important;
        }
        .stores-page-status {
            color: #18336f;
            font-size: 14px;
            font-weight: 800;
            line-height: 36px;
            text-align: center;
            white-space: nowrap;
        }
        .st-key-stores_form_panel [data-testid="stForm"] { border: 0; padding: 0; }
        .st-key-stores_upload {
            position: absolute;
            top: 66px;
            left: 22px;
            right: 22px;
            height: 120px;
            z-index: 3;
            opacity: 0;
            overflow: hidden;
        }
        .st-key-stores_upload [data-testid="stFileUploader"],
        .st-key-stores_upload [data-testid="stFileUploader"] section {
            height: 120px;
            min-height: 120px;
        }
        .st-key-stores_import_button button, .st-key-stores_confirm_delete button, .st-key-stores_cancel_delete button,
        .st-key-stores_cancel_edit button { height: 38px; border-radius: 8px; font-weight: 800; }
        .st-key-stores_import_button { max-width: 180px; margin: 14px auto 0; }
        .st-key-stores_import_button button {
            background: #0b63f6 !important; color: #ffffff !important; border-color: #0b63f6 !important;
            display: inline-flex; align-items: center; justify-content: center; gap: 8px;
            box-shadow: 0 8px 16px rgba(11, 99, 246, 0.18);
        }
        .st-key-stores_import_button button:before {
            content: "";
            width: 16px;
            height: 16px;
            display: inline-block;
            background: #ffffff;
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M12 3v12M7 8l5-5 5 5M5 15v4h14v-4' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
            mask-position: center;
            mask-repeat: no-repeat;
            mask-size: contain;
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M12 3v12M7 8l5-5 5 5M5 15v4h14v-4' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
            -webkit-mask-position: center;
            -webkit-mask-repeat: no-repeat;
            -webkit-mask-size: contain;
        }
        .st-key-stores_form_panel [data-testid="stFormSubmitButton"] button[kind="primaryFormSubmit"] {
            background: #0b63f6 !important; color: #ffffff !important; border-color: #0b63f6 !important;
        }
        .st-key-stores_form_panel [data-testid="stFormSubmitButton"] button[kind="secondaryFormSubmit"] {
            background: #ffffff !important; color: var(--summary-ink) !important; border: 1px solid #d7deea !important;
        }
        .st-key-stores_cancel_edit button, .st-key-stores_cancel_delete button {
            background: #ffffff !important; color: var(--summary-ink) !important; border: 1px solid #d7deea !important;
        }
        .st-key-stores_confirm_delete button { background: #fff1f1 !important; color: #dc2626 !important; border: 1px solid #fecaca !important; }
        div[class*="st-key-store_edit_"] button, div[class*="st-key-store_delete_"] button,
        div[class*="st-key-stores_page_"] button, .st-key-stores_page_prev button, .st-key-stores_page_next button {
            min-height: 30px; height: 30px; padding: 0 10px; border-radius: 7px; font-size: 13px; font-weight: 800;
        }
        div[class*="st-key-store_edit_"] button, div[class*="st-key-store_delete_"] button {
            width: 34px;
            padding: 0 !important;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }
        div[class*="st-key-store_edit_"] button p, div[class*="st-key-store_delete_"] button p {
            display: none !important;
        }
        div[class*="st-key-store_edit_"] button:after, div[class*="st-key-store_delete_"] button:after {
            content: "";
            width: 16px;
            height: 16px;
            display: block;
            background: #18336f;
            mask-position: center;
            mask-repeat: no-repeat;
            mask-size: contain;
            -webkit-mask-position: center;
            -webkit-mask-repeat: no-repeat;
            -webkit-mask-size: contain;
        }
        div[class*="st-key-store_edit_"] button:after {
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
        }
        div[class*="st-key-store_delete_"] button:after {
            background: #ef4444;
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M3 6h18M8 6V4h8v2M6 6l1 15h10l1-15M10 11v6M14 11v6' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M3 6h18M8 6V4h8v2M6 6l1 15h10l1-15M10 11v6M14 11v6' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
        }
        div[class*="st-key-store_delete_"] button { color: #ef4444 !important; border-color: #fecaca !important; background: #ffffff !important; }
        div[class*="st-key-store_edit_"] button { color: #18336f !important; border-color: #d7deea !important; background: #ffffff !important; }
        @media (max-width: 980px) {
            .stores-stats { grid-template-columns: 1fr; }
            .stores-table-head { display: none; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def stores_stat_card(icon: str, color: str, label: str, value: str) -> str:
    return (
        '<div class="stores-stat-card">'
        f'{icon_bubble(icon, color, 34)}'
        '<div>'
        f'<div class="stores-stat-label">{escape(label)}</div>'
        f'<div class="stores-stat-value">{escape(value)}</div>'
        '</div></div>'
    )


def clear_store_form_state() -> None:
    for key in STORE_FORM_KEYS.values():
        st.session_state[key] = ""
    st.session_state["stores_edit_id"] = None


def request_clear_store_form_state() -> None:
    st.session_state["stores_clear_form_requested"] = True


def request_load_store_form_state(store_id: int) -> None:
    st.session_state["stores_load_form_id"] = int(store_id)


def ensure_store_form_state() -> None:
    for key in STORE_FORM_KEYS.values():
        st.session_state.setdefault(key, "")
    st.session_state.setdefault("stores_edit_id", None)
    st.session_state.setdefault("stores_delete_id", None)
    st.session_state.setdefault("stores_page", 1)


def load_store_form_state(store: pd.Series) -> None:
    address_parts = split_saved_address(str(store.get("endereco", "")))
    st.session_state[STORE_FORM_KEYS["name"]] = str(store.get("nome", ""))
    st.session_state[STORE_FORM_KEYS["cep"]] = address_parts.get("cep", "")
    st.session_state[STORE_FORM_KEYS["address"]] = address_parts.get("address", str(store.get("endereco", "")))
    st.session_state[STORE_FORM_KEYS["number"]] = address_parts.get("number", "")
    st.session_state[STORE_FORM_KEYS["neighborhood"]] = address_parts.get("neighborhood", "")
    st.session_state[STORE_FORM_KEYS["city"]] = address_parts.get("city", "")
    st.session_state[STORE_FORM_KEYS["state"]] = address_parts.get("state", "")
    st.session_state[STORE_FORM_KEYS["lat"]] = format_coordinate_for_input(store.get("latitude"))
    st.session_state[STORE_FORM_KEYS["lon"]] = format_coordinate_for_input(store.get("longitude"))
    st.session_state["stores_edit_id"] = int(store.get("id"))


def selected_store_name(stores: pd.DataFrame, store_id: object) -> str:
    if store_id is None or stores.empty:
        return ""
    match = stores.loc[stores["id"].astype(int) == int(store_id)]
    if match.empty:
        return ""
    return str(match.iloc[0].get("nome", "")).strip()


def store_city_state(store: pd.Series) -> str:
    parts = split_saved_address(str(store.get("endereco", "")))
    city = parts.get("city", "")
    state = parts.get("state", "")
    if city or state:
        return f"{city} / {state}".strip(" /")
    return extract_city_uf(str(store.get("endereco", ""))) or "-"


def store_status_html(store: pd.Series) -> str:
    if has_valid_coordinates(store):
        return '<span class="stores-status-pill active">Ativa</span>'
    return '<span class="stores-status-pill pending">Pendente</span>'


def filter_store_rows(stores: pd.DataFrame, search_term: str, status_filter: str, sort_option: str) -> pd.DataFrame:
    filtered = stores.copy()
    if search_term.strip() and not filtered.empty:
        normalized_search = normalize_text(search_term)
        normalized_names = filtered["nome"].map(normalize_text)
        if normalized_search.isdigit():
            normalized_number = str(int(normalized_search))
            store_codes = normalized_names.str.extract(r"^\D*(\d+)", expand=False).fillna("")
            filtered = filtered.loc[
                store_codes.map(lambda code: bool(code) and str(int(code)) == normalized_number)
            ]
        else:
            filtered = filtered.loc[
                normalized_names.str.contains(normalized_search, na=False, regex=False)
                | filtered["endereco"].map(normalize_text).str.contains(normalized_search, na=False, regex=False)
            ]
    if status_filter == "Ativa" and not filtered.empty:
        filtered = filtered.loc[filtered.apply(has_valid_coordinates, axis=1)]
    elif status_filter == "Pendente" and not filtered.empty:
        filtered = filtered.loc[~filtered.apply(has_valid_coordinates, axis=1)]
    if not filtered.empty:
        filtered = filtered.copy()
        filtered["_city_state_sort"] = filtered.apply(lambda row: normalize_text(store_city_state(row)), axis=1)
        filtered["_status_sort"] = filtered.apply(lambda row: 0 if has_valid_coordinates(row) else 1, axis=1)
        filtered["_name_sort"] = filtered["nome"].map(normalize_text)

        if sort_option == "Nome Z-A":
            filtered = filtered.sort_values("_name_sort", ascending=False)
        elif sort_option == "Cidade A-Z":
            filtered = filtered.sort_values(["_city_state_sort", "_name_sort"], ascending=[True, True])
        elif sort_option == "Cidade Z-A":
            filtered = filtered.sort_values(["_city_state_sort", "_name_sort"], ascending=[False, True])
        elif sort_option == "Status: Ativas primeiro":
            filtered = filtered.sort_values(["_status_sort", "_name_sort"], ascending=[True, True])
        elif sort_option == "Status: Pendentes primeiro":
            filtered = filtered.sort_values(["_status_sort", "_name_sort"], ascending=[False, True])
        else:
            filtered = filtered.sort_values("_name_sort")

        filtered = filtered.drop(columns=["_city_state_sort", "_status_sort", "_name_sort"])
    return filtered


def pagination_items(current_page: int, total_pages: int) -> list[int | str]:
    if total_pages <= 5:
        return list(range(1, total_pages + 1))
    items: list[int | str] = [1, 2, 3]
    if current_page > 4:
        items.append("...")
    if current_page not in {1, 2, 3, total_pages}:
        items.append(current_page)
    if current_page < total_pages - 1:
        items.append("...")
    items.append(total_pages)
    result: list[int | str] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def stores_editor(stores: pd.DataFrame) -> None:
    render_stores_styles()
    ensure_store_form_state()
    stores = stores.copy()
    if st.session_state.pop("stores_clear_form_requested", False):
        clear_store_form_state()
    load_form_id = st.session_state.pop("stores_load_form_id", None)
    if load_form_id is not None:
        store_match = stores.loc[stores["id"].astype(int) == int(load_form_id)]
        if not store_match.empty:
            load_store_form_state(store_match.iloc[0])

    total_stores = len(stores)
    pending_coordinates = 0 if stores.empty else int((~stores.apply(has_valid_coordinates, axis=1)).sum())
    latest_import = st.session_state.get("stores_last_import_label", "Sem importacao")

    st.markdown(
        """
        <main class="stores-page">
            <div class="stores-title">Cadastro de Lojas</div>
            <div class="stores-subtitle">Cadastre lojas manualmente ou importe uma planilha</div>
        </main>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="stores-stats">'
        f'{stores_stat_card("store", "blue", "Lojas cadastradas", str(total_stores))}'
        f'{stores_stat_card("alert", "orange", "Pendentes de coordenada", str(pending_coordinates))}'
        f'{stores_stat_card("clock", "purple", "Ultima importacao", str(latest_import))}'
        '</div>',
        unsafe_allow_html=True,
    )

    import_col, form_col = st.columns(2, gap="large")
    with import_col:
        with st.container(key="stores_import_panel"):
            st.markdown('<div class="stores-panel-title">Importar planilha</div>', unsafe_allow_html=True)
            st.markdown(
                """
                <div class="stores-dropzone">
                    <div>
                        <div class="stores-excel">X</div>
                        <div>Arraste e solte sua planilha aqui<br>ou clique para selecionar</div>
                    </div>
                </div>
                <div class="stores-import-copy">
                    Importe um arquivo .xlsx com Filial, CEP, Endereco, Numero, Bairro, Cidade e UF
                </div>
                <div class="stores-import-note"><span class="stores-import-info-icon">i</span><span>O sistema buscara latitude e longitude automaticamente</span></div>
                """,
                unsafe_allow_html=True,
            )
            uploaded_file = st.file_uploader(
                "Selecionar arquivo CSV ou Excel",
                type=["csv", "xlsx"],
                key="stores_upload",
                label_visibility="collapsed",
            )
            if st.button("Importar planilha", key="stores_import_button"):
                if uploaded_file is None:
                    st.markdown('<div class="stores-import-warning">Selecione uma planilha antes de importar.</div>', unsafe_allow_html=True)
                else:
                    try:
                        import_df = read_uploaded_stores_file(uploaded_file)
                        with st.spinner("Importando planilha e buscando coordenadas quando possivel..."):
                            updated_stores, import_summary, import_result = import_stores_from_dataframe(stores, import_df)
                        total_new = import_summary["importadas_ok"] + import_summary["importadas_pendentes"]
                        if total_new:
                            save_stores(updated_stores)
                            st.session_state["stores_last_import_label"] = f"Hoje, {datetime.now():%H:%M}"
                            st.session_state["stores_last_import_summary"] = (
                                f"Ultima importacao: {import_summary['total_linhas']} linhas lidas  "
                                f"- {total_new} importadas  - {import_summary['importadas_pendentes']} pendentes"
                            )
                            st.success(f"{total_new} loja(s) adicionada(s) ao cadastro.")
                            st.rerun()
                        else:
                            st.warning("Nenhuma loja nova foi adicionada ao cadastro.")
                        if not import_result.empty:
                            st.dataframe(import_result, use_container_width=True, hide_index=True)
                    except Exception as exc:
                        st.error(f"Nao foi possivel importar a planilha: {exc}")

            import_summary_text = st.session_state.get("stores_last_import_summary")
            if import_summary_text:
                st.markdown(
                    f'<div class="stores-import-result"><span>{app_icon("check", 18)}</span><span>{escape(import_summary_text)}</span></div>',
                    unsafe_allow_html=True,
                )

    with form_col:
        with st.container(key="stores_form_panel"):
            editing_id = st.session_state.get("stores_edit_id")
            editing_name = selected_store_name(stores, editing_id)
            title = "Editar loja" if editing_id else "Cadastrar loja manualmente"
            st.markdown(f'<div class="stores-panel-title">{escape(title)}</div>', unsafe_allow_html=True)
            if editing_id:
                st.markdown(
                    (
                        '<div class="stores-editing-banner">'
                        f'{app_icon("edit", 17)}'
                        f'<span>Editando agora: {escape(editing_name or "loja selecionada")}. '
                        'Altere os campos abaixo e clique em Salvar alteracoes.</span>'
                        '</div>'
                    ),
                    unsafe_allow_html=True,
                )
            states = ["", "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"]
            if st.session_state.get(STORE_FORM_KEYS["state"]) not in states:
                st.session_state[STORE_FORM_KEYS["state"]] = ""

            with st.form("stores_manual_form"):
                name = st.text_input("Nome da loja", placeholder="Digite o nome da loja", key=STORE_FORM_KEYS["name"])
                cep_col, address_col = st.columns([0.9, 1.7])
                with cep_col:
                    cep = st.text_input("CEP", placeholder="00000-000", key=STORE_FORM_KEYS["cep"])
                with address_col:
                    address = st.text_input("Endereco", placeholder="Digite o endereco", key=STORE_FORM_KEYS["address"])
                number_col, neighborhood_col = st.columns([0.9, 1.7])
                with number_col:
                    number = st.text_input("Numero", placeholder="Numero", key=STORE_FORM_KEYS["number"])
                with neighborhood_col:
                    neighborhood = st.text_input("Bairro", placeholder="Digite o bairro", key=STORE_FORM_KEYS["neighborhood"])
                city_col, state_col = st.columns([1.7, 0.9])
                with city_col:
                    city = st.text_input("Cidade", placeholder="Digite a cidade", key=STORE_FORM_KEYS["city"])
                with state_col:
                    state = st.selectbox("UF", states, key=STORE_FORM_KEYS["state"])
                lat_col, lon_col = st.columns(2)
                with lat_col:
                    lat = st.text_input("Latitude (opcional)", placeholder="Ex.: -23.5505", key=STORE_FORM_KEYS["lat"])
                with lon_col:
                    lon = st.text_input("Longitude (opcional)", placeholder="Ex.: -46.6333", key=STORE_FORM_KEYS["lon"])
                save_col, clear_col = st.columns(2)
                save_clicked = save_col.form_submit_button(
                    "Salvar loja" if not editing_id else "Salvar alteracoes",
                    use_container_width=True,
                    type="primary",
                )
                clear_clicked = clear_col.form_submit_button("Limpar", use_container_width=True)

            if clear_clicked:
                request_clear_store_form_state()
                st.rerun()
            if save_clicked:
                try:
                    with st.spinner("Validando loja e buscando coordenadas quando necessario..."):
                        store_data = resolve_location_data(name, address, lat, lon, number=number, neighborhood=neighborhood, city=city, state=state, cep=cep, entity_label="loja")
                    duplicate_message = find_store_duplicate(
                        stores,
                        store_data["nome"],
                        store_data["latitude"],
                        store_data["longitude"],
                        ignore_id=int(editing_id) if editing_id else None,
                        address=store_data["endereco"],
                    )
                    if duplicate_message:
                        st.error(duplicate_message)
                        return
                    store_data.pop("geocoded", None)
                    if editing_id:
                        updated_stores = stores.copy()
                        store_index = updated_stores.index[updated_stores["id"].astype(int) == int(editing_id)][0]
                        updated_stores.loc[store_index, ["nome", "endereco", "latitude", "longitude", "status_coordenada", "erro_coordenada"]] = [
                            store_data["nome"],
                            store_data["endereco"],
                            store_data["latitude"],
                            store_data["longitude"],
                            store_data.get("status_coordenada", "ok"),
                            store_data.get("erro_coordenada", ""),
                        ]
                        save_stores(updated_stores)
                        st.success("Loja atualizada com sucesso.")
                    else:
                        new_store = pd.DataFrame([{"id": next_store_id(stores), **store_data}])
                        save_stores(pd.concat([stores, new_store], ignore_index=True))
                        st.success("Loja cadastrada com sucesso.")
                    warnings = coordinate_warning_messages(store_data["latitude"], store_data["longitude"])
                    if warnings:
                        st.session_state["coordinate_warning"] = "Loja salva. " + " ".join(warnings)
                    request_clear_store_form_state()
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            if editing_id and st.button("Cancelar edicao", use_container_width=True, key="stores_cancel_edit"):
                request_clear_store_form_state()
                st.rerun()

    with st.container(key="stores_table_panel"):
        st.markdown('<div class="stores-table-title">Lojas cadastradas</div>', unsafe_allow_html=True)
        editing_id = st.session_state.get("stores_edit_id")
        editing_name = selected_store_name(stores, editing_id)
        if editing_id:
            st.markdown(
                (
                    '<div class="stores-editing-table-notice">'
                    f'{app_icon("edit", 16)}'
                    f'<span>{escape(editing_name or "Loja selecionada")} foi carregada no formulario de edicao acima.</span>'
                    '</div>'
                ),
                unsafe_allow_html=True,
            )
            st.markdown(
                f"""
                <style>
                .st-key-store_row_{int(editing_id)} {{
                    background: #f6f9ff !important;
                    border-left: 4px solid #0b63f6 !important;
                    box-shadow: inset 0 0 0 1px #cfe0ff;
                }}
                </style>
                """,
                unsafe_allow_html=True,
            )
        search_col, status_col, sort_col, page_size_col = st.columns([1.8, 0.8, 1.05, 0.65])
        with search_col:
            search_term = st.text_input("Buscar loja por nome ou endereco", placeholder="Buscar loja por nome ou endereco", key="stores_search", label_visibility="collapsed")
        with status_col:
            status_filter = st.selectbox("Status", ["Todas", "Ativa", "Pendente"], key="stores_status_filter", label_visibility="collapsed")
        with sort_col:
            sort_option = st.selectbox(
                "Ordenar",
                [
                    "Nome A-Z",
                    "Nome Z-A",
                    "Cidade A-Z",
                    "Cidade Z-A",
                    "Status: Ativas primeiro",
                    "Status: Pendentes primeiro",
                ],
                key="stores_sort",
                label_visibility="collapsed",
            )
        with page_size_col:
            page_size = int(
                st.selectbox(
                    "Itens por pagina",
                    [5, 10, 25, 50],
                    format_func=lambda value: f"{value} por pagina",
                    key="stores_page_size",
                    label_visibility="collapsed",
                )
            )

        filtered_stores = filter_store_rows(stores, search_term, status_filter, sort_option)
        total_filtered = len(filtered_stores)
        total_pages = max(1, (total_filtered + page_size - 1) // page_size)
        st.session_state["stores_page"] = min(max(int(st.session_state.get("stores_page", 1)), 1), total_pages)
        current_page = st.session_state["stores_page"]
        start_index = (current_page - 1) * page_size
        page_stores = filtered_stores.iloc[start_index:start_index + page_size]

        st.markdown('<div class="stores-table-head"><div>Loja</div><div>Endereco</div><div>Cidade/UF</div><div>Status</div><div>Acoes</div></div>', unsafe_allow_html=True)
        if page_stores.empty:
            st.info("Nenhuma loja encontrada.")
        else:
            for _, store in page_stores.iterrows():
                store_id = int(store["id"])
                with st.container(key=f"store_row_{store_id}"):
                    row_col_name, row_col_address, row_col_city, row_col_status, row_col_actions = st.columns([1.1, 1.75, 0.95, 0.75, 0.7])
                    edit_pill = '<div class="stores-row-edit-pill">Em edicao</div>' if editing_id and store_id == int(editing_id) else ""
                    row_col_name.markdown(f'<div class="stores-row-name">{escape(str(store.get("nome", "")))}</div>{edit_pill}', unsafe_allow_html=True)
                    row_col_address.markdown(escape(str(store.get("endereco", "-"))), unsafe_allow_html=True)
                    row_col_city.markdown(escape(store_city_state(store)), unsafe_allow_html=True)
                    row_col_status.markdown(store_status_html(store), unsafe_allow_html=True)
                    action_col_edit, action_col_delete = row_col_actions.columns(2)
                    if action_col_edit.button("Editar", key=f"store_edit_{store_id}", help="Editar loja"):
                        request_load_store_form_state(store_id)
                        st.rerun()
                    if action_col_delete.button("Excluir", key=f"store_delete_{store_id}", help="Excluir loja"):
                        st.session_state["stores_delete_id"] = store_id
                        st.rerun()

        first_item = 0 if total_filtered == 0 else start_index + 1
        last_item = min(start_index + page_size, total_filtered)
        pagination_container = st.container(key="stores_pagination")
        pagination_caption_col, pagination_prev_col, pagination_status_col, pagination_next_col = pagination_container.columns([1.7, 0.5, 0.55, 0.5], gap="small")
        total_label = f"{total_filtered} de {total_stores}" if total_filtered != total_stores else str(total_stores)
        pagination_caption_col.markdown(f'<div class="stores-count-caption">Mostrando {first_item} a {last_item} de {total_label} lojas</div>', unsafe_allow_html=True)
        if pagination_prev_col.button("‹ Anterior", disabled=current_page <= 1, key="stores_page_prev", use_container_width=True):
            st.session_state["stores_page"] = max(1, current_page - 1)
            st.rerun()
        pagination_status_col.markdown(f'<div class="stores-page-status">Página {current_page} de {total_pages}</div>', unsafe_allow_html=True)
        if pagination_next_col.button("Próxima ›", disabled=current_page >= total_pages, key="stores_page_next", use_container_width=True):
            st.session_state["stores_page"] = min(total_pages, current_page + 1)
            st.rerun()

        delete_id = st.session_state.get("stores_delete_id")
        if delete_id:
            store_match = stores.loc[stores["id"].astype(int) == int(delete_id)]
            if not store_match.empty:
                store_name = str(store_match.iloc[0].get("nome", "esta loja"))
                st.warning(f"Confirmar exclusao de {store_name}?")
                confirm_col, cancel_col = st.columns(2)
                if confirm_col.button("Excluir loja definitivamente", use_container_width=True, key="stores_confirm_delete"):
                    updated_stores = stores.loc[stores["id"].astype(int) != int(delete_id)].copy()
                    save_stores(updated_stores)
                    delete_routes_for_store(int(delete_id))
                    st.session_state["stores_delete_id"] = None
                    st.success("Loja excluida com sucesso.")
                    st.rerun()
                if cancel_col.button("Cancelar exclusao", use_container_width=True, key="stores_cancel_delete"):
                    st.session_state["stores_delete_id"] = None
                    st.rerun()


def render_report_filters(report: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    """Renderiza filtros reutilizaveis para relatórios internos."""
    if report.empty:
        return report

    filter_col_search, filter_col_status, filter_col_sort = st.columns([2, 1, 1])
    with filter_col_search:
        search_term = st.text_input(
            "Buscar loja ou endereco",
            placeholder="Digite parte do nome da loja ou endereco",
            key=f"{key_prefix}_busca_loja",
        )
    with filter_col_status:
        status_options = ["Todas"] + sorted(
            [str(status) for status in report["Status rota"].dropna().unique().tolist()]
        )
        selected_status = st.selectbox(
            "Status da rota",
            options=status_options,
            key=f"{key_prefix}_status_rota",
        )
    with filter_col_sort:
        sort_option = st.selectbox(
            "Ordenar por",
            options=[
                "Nome A-Z",
                "Maior distancia",
                "Menor distancia",
                "Maior custo",
                "Menor custo",
                "Maior tempo",
            ],
            key=f"{key_prefix}_ordenacao",
        )

    filtered_report = report.copy()

    if search_term.strip():
        normalized_search = normalize_text(search_term)
        filtered_report = filtered_report.loc[
            filtered_report["Nome da loja"].map(normalize_text).str.contains(normalized_search, na=False, regex=False)
            | filtered_report["Endereco"].map(normalize_text).str.contains(normalized_search, na=False, regex=False)
        ]

    if selected_status != "Todas":
        filtered_report = filtered_report.loc[filtered_report["Status rota"] == selected_status]

    sort_map = {
        "Nome A-Z": ("Nome da loja", True),
        "Maior distancia": ("Distancia considerada em km", False),
        "Menor distancia": ("Distancia considerada em km", True),
        "Maior custo": ("Custo estimado", False),
        "Menor custo": ("Custo estimado", True),
        "Maior tempo": ("Tempo estimado min", False),
    }
    sort_column, ascending = sort_map[sort_option]
    if sort_column == "Nome da loja":
        filtered_report = filtered_report.sort_values(
            sort_column,
            ascending=ascending,
            key=lambda series: series.map(normalize_text),
        )
    elif sort_column in filtered_report.columns:
        filtered_report = filtered_report.assign(
            _sort_value=pd.to_numeric(filtered_report[sort_column], errors="coerce")
        ).sort_values("_sort_value", ascending=ascending, na_position="last").drop(columns=["_sort_value"])

    st.caption(f"Mostrando {len(filtered_report)} de {len(report)} loja(s).")
    return filtered_report

def filter_stores_by_report(stores: pd.DataFrame, filtered_report: pd.DataFrame) -> pd.DataFrame:
    if stores.empty or filtered_report.empty:
        return stores.iloc[0:0].copy()

    selected_ids = filtered_report["id"].dropna().astype(int).tolist()
    filtered_stores = stores.loc[stores["id"].astype(int).isin(selected_ids)].copy()
    id_order = {store_id: index for index, store_id in enumerate(selected_ids)}
    filtered_stores["_order"] = filtered_stores["id"].map(id_order)
    return filtered_stores.sort_values("_order").drop(columns=["_order"])

def render_recalculate_routes_button(cd: pd.Series | None, stores: pd.DataFrame, key: str) -> None:
    disabled = cd is None or stores.empty
    help_text = None
    if cd is None:
        help_text = "Cadastre o CD antes de recalcular as rotas."
    elif stores.empty:
        help_text = "Cadastre ao menos uma loja antes de recalcular as rotas."

    force_recalculate = st.checkbox(
        "Forcar recalculo de rotas ja calculadas",
        value=False,
        help="Se desmarcado, o sistema usa o cache salvo no SQLite quando CD e loja continuam com as mesmas coordenadas.",
        key=f"{key}_force",
        disabled=disabled,
    )

    if st.button("Recalcular rotas", type="primary", disabled=disabled, help=help_text, key=key):
        with st.spinner("Calculando rotas reais pelo OSRM..."):
            calculated, failed, skipped_cache = calculate_and_save_real_routes(
                cd, stores, force=force_recalculate
            )

        message_parts = []
        if calculated:
            message_parts.append(f"{calculated} calculada(s)")
        if skipped_cache:
            message_parts.append(f"{skipped_cache} mantida(s) em cache")
        if failed:
            message_parts.append(f"{failed} com erro")

        if calculated and not failed:
            st.success("Rotas processadas: " + ", ".join(message_parts) + ".")
        elif calculated or skipped_cache:
            st.warning("Rotas processadas: " + ", ".join(message_parts) + ".")
        else:
            st.error("Nenhuma rota real foi calculada. Confira as coordenadas e tente novamente.")
        st.rerun()

def select_destination_store(cd: pd.Series, stores: pd.DataFrame, report: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Permite escolher uma unica loja destino para o mapa."""
    if st.session_state.pop("map_clear_selection_requested", False):
        st.session_state.pop("mapa_loja_destino_unica", None)

    if stores.empty:
        return stores.iloc[0:0].copy(), report.iloc[0:0].copy()

    st.markdown(
        (
            '<div class="map-fixed-origin">'
            f'<div class="map-fixed-origin-icon">{app_icon("home", 18)}</div>'
            '<div>'
            '<div class="map-fixed-origin-label">Buscar CD</div>'
            f'<div class="map-fixed-origin-name">{escape(str(cd.get("nome", "CD")))}</div>'
            f'<div class="map-fixed-origin-address">{escape(str(cd.get("endereco", "")))}</div>'
            '</div></div>'
        ),
        unsafe_allow_html=True,
    )

    stores_with_coordinates = stores.loc[stores.apply(has_valid_coordinates, axis=1)].copy()
    pending_coordinates = len(stores) - len(stores_with_coordinates)
    if pending_coordinates:
        st.markdown(
            f'<div class="map-warning-note">{pending_coordinates} loja(s) pendente(s) de coordenada nao aparecem no mapa.</div>',
            unsafe_allow_html=True,
        )

    if stores_with_coordinates.empty:
        st.warning("Nenhuma loja possui latitude/longitude valida para aparecer no mapa.")
        return stores.iloc[0:0].copy(), report.iloc[0:0].copy()

    available_stores = stores_with_coordinates.sort_values(
        "nome",
        key=lambda series: series.map(normalize_text),
    )
    store_options = available_stores["id"].astype(int).tolist()
    stores_by_id = available_stores.set_index("id").to_dict("index")

    def format_store_option(store_id: int) -> str:
        store_data = stores_by_id.get(int(store_id), {})
        return str(store_data.get("nome", "Loja"))

    selected_id = st.selectbox(
        "Loja destino",
        options=store_options,
        format_func=format_store_option,
        key="mapa_loja_destino_unica",
    )

    selected_store = stores.loc[stores["id"].astype(int) == int(selected_id)].copy()
    if report.empty:
        selected_report = report.iloc[0:0].copy()
    else:
        selected_report = report.loc[report["id"].astype(int) == int(selected_id)].copy()

    return selected_store, selected_report

def render_recalculate_selected_route_button(
    cd: pd.Series | None,
    selected_store: pd.DataFrame,
    key: str,
) -> None:
    disabled = cd is None or selected_store.empty
    help_text = None
    if cd is None:
        help_text = "Cadastre o CD antes de recalcular a rota."
    elif selected_store.empty:
        help_text = "Selecione uma loja destino antes de recalcular a rota."

    force_recalculate = st.checkbox(
        "Forcar recálculo dessa rota",
        value=False,
        help="Se desmarcado, o sistema reutiliza a rota salva em cache quando CD e loja continuam com as mesmas coordenadas.",
        key=f"{key}_force",
        disabled=disabled,
    )

    if st.button(
        "Recalcular rota do destino selecionado",
        type="primary",
        disabled=disabled,
        help=help_text,
        key=key,
    ):
        with st.spinner("Calculando a rota real pelo OSRM..."):
            calculated, failed, skipped_cache = calculate_and_save_real_routes(
                cd,
                selected_store,
                force=force_recalculate,
            )

        if calculated and not failed:
            st.success("Rota do destino selecionado calculada com sucesso.")
        elif skipped_cache and not calculated and not failed:
            st.info("A rota selecionada ja estava calculada e foi mantida em cache.")
        elif failed:
            st.error("Nao foi possivel calcular a rota desse destino. Confira as coordenadas/endereco.")
        else:
            st.warning("Nenhuma rota foi atualizada.")
        st.rerun()

def render_selected_destination_summary(selected_store: pd.DataFrame, selected_report: pd.DataFrame) -> None:
    if selected_store.empty:
        return

    store = selected_store.iloc[0]
    st.markdown(f"#### Destino selecionado: {store['nome']}")
    st.caption(str(store["endereco"]))

    if selected_report.empty:
        metric_status, metric_distance, metric_time, metric_cost = st.columns(4)
        metric_status.metric("Status", "Pendente")
        metric_distance.metric("Distancia", "-")
        metric_time.metric("Tempo", "-")
        metric_cost.metric("Custo", "-")
        st.info("Clique em Recalcular rota do destino selecionado para calcular essa rota.")
        return

    row = selected_report.iloc[0]
    status = str(row.get("Status rota", "Pendente"))
    metric_status, metric_distance, metric_time, metric_cost = st.columns(4)
    metric_status.metric("Status", status)

    if status == "Calculada":
        metric_distance.metric(
            "Distancia considerada",
            format_optional_number(row.get("Distancia considerada em km"), decimals=2, suffix=" km"),
        )
        metric_time.metric(
            "Tempo estimado",
            format_duration_minutes(row.get("Tempo estimado min")),
        )
        metric_cost.metric("Custo estimado", currency_brl(row.get("Custo estimado", 0)))
    else:
        metric_distance.metric("Distancia", "-")
        metric_time.metric("Tempo", "-")
        metric_cost.metric("Custo", "-")
        error_message = str(row.get("Erro rota", "Clique em Recalcular rota do destino selecionado."))
        if status == "Erro":
            st.warning(error_message)
        else:
            st.info(error_message)


def map_stat_card(icon: str, color: str, label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="map-stat-sub">{escape(sub)}</div>' if sub else ""
    return (
        '<div class="map-card map-stat-card">'
        f'<div class="map-icon-bubble {color}">{app_icon(icon, 30)}</div>'
        '<div>'
        f'<div class="map-stat-label">{escape(label)}</div>'
        f'<div class="map-stat-value">{escape(value)}</div>'
        f'{sub_html}'
        '</div></div>'
    )


def map_metric_card(icon: str, color: str, value: str, label: str) -> str:
    return (
        '<div class="map-metric-card">'
        f'<div class="map-metric-icon {color}">{app_icon(icon, 16)}</div>'
        '<div>'
        f'<div class="map-metric-value">{escape(value)}</div>'
        f'<div class="map-metric-label">{escape(label)}</div>'
        '</div></div>'
    )


def route_status_class(status: str) -> str:
    normalized = normalize_text(status)
    if normalized == "calculada":
        return ""
    if normalized == "erro":
        return " error"
    return " pending"


def confirmed_activity_ids() -> set[int]:
    raw_ids = st.session_state.get(CONFIRMED_ACTIVITIES_KEY, [])
    if isinstance(raw_ids, set):
        candidates = raw_ids
    elif isinstance(raw_ids, (list, tuple)):
        candidates = raw_ids
    else:
        candidates = []

    confirmed_ids: set[int] = set()
    for raw_id in candidates:
        try:
            confirmed_ids.add(int(raw_id))
        except (TypeError, ValueError):
            continue
    return confirmed_ids


def is_activity_confirmed(store_id: object) -> bool:
    try:
        return int(store_id) in confirmed_activity_ids()
    except (TypeError, ValueError):
        return False


def confirm_activity(store_id: object) -> None:
    confirmed_ids = confirmed_activity_ids()
    confirmed_ids.add(int(store_id))
    st.session_state[CONFIRMED_ACTIVITIES_KEY] = sorted(confirmed_ids)


def confirmed_activity_report(report: pd.DataFrame) -> pd.DataFrame:
    if report.empty or "id" not in report.columns:
        return report.iloc[0:0].copy()

    confirmed_ids = confirmed_activity_ids()
    if not confirmed_ids:
        return report.iloc[0:0].copy()

    return report.loc[report["id"].astype(int).isin(confirmed_ids)].copy()


def format_duration_minutes(value: object) -> str:
    if pd.isna(value):
        return "-"

    total_minutes = int(round(float(value)))
    if total_minutes < 60:
        return f"{total_minutes} min"

    hours, minutes = divmod(total_minutes, 60)
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}min"


def selected_route_values(selected_report: pd.DataFrame, config: dict[str, Any] | None = None) -> dict[str, str]:
    route_type = "Ida e volta" if config and bool(config.get("ida_e_volta")) else "Ida"
    if selected_report.empty:
        return {
            "status": "Pendente",
            "time": "-",
            "distance": "-",
            "liters": "-",
            "cost": "-",
            "considered": "-",
            "route_type": route_type,
        }

    row = selected_report.iloc[0]
    status = str(row.get("Status rota", "Pendente"))
    if status != "Calculada":
        return {
            "status": status,
            "time": "-",
            "distance": "-",
            "liters": "-",
            "cost": "-",
            "considered": "-",
            "route_type": route_type,
        }

    return {
        "status": status,
        "time": format_duration_minutes(row.get("Tempo estimado min")),
        "distance": format_optional_number(row.get("Distancia ida em km"), decimals=2, suffix=" km"),
        "liters": format_optional_number(row.get("Litros estimados"), decimals=2, suffix=" L"),
        "cost": currency_brl(row.get("Custo estimado", 0)),
        "considered": format_optional_number(row.get("Distancia considerada em km"), decimals=2, suffix=" km"),
        "route_type": route_type,
    }


def render_map_header(selected_report: pd.DataFrame, stores: pd.DataFrame, report: pd.DataFrame, config: dict[str, Any]) -> None:
    route_values = selected_route_values(selected_report, config)
    total_stores = len(stores)
    calculated_count = int(report["Status rota"].eq("Calculada").sum()) if "Status rota" in report else 0
    pending_count = int(report["Status rota"].isin(["Pendente", "Sem coordenada"]).sum()) if "Status rota" in report else 0
    stats_html = "".join(
        [
            map_stat_card("store", "blue", "Lojas com rota", str(calculated_count), f"de {total_stores} lojas"),
            map_stat_card("alert", "orange", "Pendentes", str(pending_count), "lojas"),
            map_stat_card("route", "purple", "Distancia selecionada", route_values["considered"], "distancia considerada"),
        ]
    )
    header_html = dedent(
        f"""
        <main class="map-shell">
            <section class="summary-topbar">
                <div class="summary-title">
                    <div class="summary-h1">Mapa</div>
                    <p>Visualize o CD, as lojas e as rotas calculadas</p>
                </div>
            </section>
            <section class="map-stats-grid">{stats_html}</section>
        </main>
        """
    ).strip()
    header_html = re.sub(r">\s+<", "><", header_html)
    header_html = re.sub(r"\s*\n\s*", " ", header_html)
    st.markdown(header_html, unsafe_allow_html=True)


def render_map_route_actions(cd: pd.Series | None, selected_store: pd.DataFrame, key: str) -> None:
    disabled = cd is None or selected_store.empty
    force_col, view_col, clear_col = st.columns([1.65, 0.9, 0.9], gap="small")
    with force_col:
        force_recalculate = st.checkbox(
            "Forcar recalculo dessa rota",
            value=False,
            help="Se desmarcado, o sistema reutiliza a rota salva em cache quando CD e loja continuam com as mesmas coordenadas.",
            key=f"{key}_force",
            disabled=disabled,
        )
    with view_col:
        if st.button("Visualizar rota", type="primary", disabled=disabled, use_container_width=True, key="map_view_route"):
            with st.spinner("Calculando a rota real pelo OSRM..."):
                calculated, failed, skipped_cache = calculate_and_save_real_routes(
                    cd,
                    selected_store,
                    force=force_recalculate,
                )
            if calculated and not failed:
                st.success("Rota do destino selecionado calculada com sucesso.")
            elif skipped_cache and not calculated and not failed:
                st.info("A rota selecionada ja estava calculada e foi mantida em cache.")
            elif failed:
                st.error("Nao foi possivel calcular a rota desse destino. Confira as coordenadas/endereco.")
            else:
                st.warning("Nenhuma rota foi atualizada.")
            st.rerun()
    with clear_col:
        if st.button("Limpar selecao", use_container_width=True, key="map_clear_selection"):
            st.session_state["map_clear_selection_requested"] = True
            st.rerun()


def render_activity_confirmation(selected_store: pd.Series, status: str) -> None:
    store_id = int(selected_store.get("id"))
    route_calculated = status == "Calculada"
    already_confirmed = is_activity_confirmed(store_id)
    checkbox_key = "map_activity_done"
    selected_key = "map_activity_selected_store_id"

    if st.session_state.get(selected_key) != store_id:
        st.session_state[selected_key] = store_id
        st.session_state[checkbox_key] = already_confirmed
    elif already_confirmed:
        st.session_state[checkbox_key] = True

    with st.container(key="map_activity_panel"):
        if already_confirmed:
            st.markdown(
                f"""
                <div class="map-activity-confirmed">
                    <div class="map-activity-confirmed-icon">{app_icon("check", 18)}</div>
                    <div>
                        <div class="map-activity-title">Deslocamento confirmado</div>
                        <div class="map-activity-note">Esta rota ja esta contabilizada no Resumo.</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            return

        st.markdown(
            """
            <div class="map-activity-header">
                <div>
                    <div class="map-activity-title">Atividade Realizada</div>
                    <div class="map-activity-note">Marque apos concluir o deslocamento.</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        check_col, confirm_col = st.columns([1.15, 1], gap="small")
        with check_col:
            activity_done = st.checkbox(
                "Atividade realizada",
                key=checkbox_key,
                disabled=not route_calculated,
                help="Marque esta opcao somente apos realizar o deslocamento.",
            )
        confirm_disabled = not route_calculated or not activity_done or already_confirmed
        with confirm_col:
            if st.button(
                "Confirmar Deslocamento",
                type="primary",
                disabled=confirm_disabled,
                use_container_width=True,
                key="map_confirm_displacement",
            ):
                confirm_activity(store_id)
                st.session_state[checkbox_key] = True
                st.rerun()

        if not route_calculated:
            st.caption("Calcule e visualize a rota antes de confirmar o deslocamento.")


def render_map_route_details(selected_store: pd.DataFrame, selected_report: pd.DataFrame, config: dict[str, Any]) -> None:
    if selected_store.empty:
        st.markdown('<div class="map-panel-title">Detalhes da rota</div>', unsafe_allow_html=True)
        st.info("Selecione uma loja destino para visualizar os detalhes da rota.")
        return

    store = selected_store.iloc[0]
    route_values = selected_route_values(selected_report, config)
    status = route_values["status"]
    status_class = route_status_class(status)
    route_notice = ""
    if status != "Calculada":
        notice_text = "Clique em Visualizar rota para calcular esta rota."
        if not selected_report.empty and status == "Erro":
            notice_text = str(selected_report.iloc[0].get("Erro rota", notice_text) or notice_text)
        route_notice = f'<div class="map-route-empty-note">{escape(notice_text)}</div>'
    metric_html = "".join(
        [
            map_metric_card("clock", "blue", route_values["time"], "Tempo estimado"),
            map_metric_card("route", "blue", route_values["distance"], "Distancia da rota"),
            map_metric_card("fuel", "green", route_values["liters"], "Combustivel est."),
            map_metric_card("money", "green", route_values["cost"], "Custo estimado"),
            map_metric_card("refresh", "blue", route_values["route_type"], "Tipo de rota"),
        ]
    )
    details_html = dedent(
        f"""
        <div class="map-panel-title">Detalhes da rota</div>
        <div class="map-route-title-row">
            <div>
                <div class="map-eyebrow">Destino selecionado</div>
                <div class="map-route-name">{escape(str(store.get("nome", "Loja")))}</div>
                <div class="map-route-address">{app_icon("map-pin", 14)}<span>{escape(str(store.get("endereco", "")))}</span></div>
            </div>
            <div class="map-status-pill{status_class}">{escape(status)}</div>
        </div>
        {route_notice}
        <div class="map-metric-grid">{metric_html}</div>
        """
    ).strip()
    details_html = re.sub(r">\s+<", "><", details_html)
    details_html = re.sub(r"\s*\n\s*", " ", details_html)
    st.markdown(details_html, unsafe_allow_html=True)
    render_activity_confirmation(store, status)


def render_map_route_points(cd: pd.Series, selected_store: pd.DataFrame, selected_report: pd.DataFrame, config: dict[str, Any]) -> None:
    if selected_store.empty:
        return

    store = selected_store.iloc[0]
    route_values = selected_route_values(selected_report, config)
    points_html = dedent(
        f"""
        <div class="map-route-points">
            <div class="map-card map-point-card">
                <div class="map-icon-bubble blue">{app_icon("home", 24)}</div>
                <div>
                    <div class="map-point-label">Origem</div>
                    <div class="map-point-value">CD - {escape(str(cd.get("nome", "CD")))}</div>
                    <div class="map-point-sub">{escape(str(cd.get("endereco", "")))}</div>
                </div>
            </div>
            <div class="map-card map-point-card">
                <div class="map-icon-bubble green">{app_icon("store", 24)}</div>
                <div>
                    <div class="map-point-label">Destino</div>
                    <div class="map-point-value">{escape(str(store.get("nome", "Loja")))}</div>
                    <div class="map-point-sub">{escape(str(store.get("endereco", "")))}</div>
                </div>
            </div>
            <div class="map-card map-point-card">
                <div class="map-icon-bubble orange">{app_icon("clock", 24)}</div>
                <div>
                    <div class="map-point-label">Resumo da rota</div>
                    <div class="map-point-value">{escape(route_values["time"])} · {escape(route_values["considered"])}</div>
                    <div class="map-point-sub">Custo estimado: {escape(route_values["cost"])}</div>
                </div>
            </div>
        </div>
        """
    ).strip()
    points_html = re.sub(r">\s+<", "><", points_html)
    points_html = re.sub(r"\s*\n\s*", " ", points_html)
    st.markdown(points_html, unsafe_allow_html=True)


def add_route_map_legend(map_obj: folium.Map) -> None:
    legend = MacroElement()
    legend._template = Template(
        """
        {% macro html(this, kwargs) %}
        <style>
        .route-map-legend {
            min-width: 136px;
            padding: 14px 16px;
            background: rgba(255, 255, 255, 0.97);
            border: 1px solid #dbe7f6;
            border-radius: 10px;
            box-shadow: 0 8px 22px rgba(12, 32, 68, 0.16);
            color: #18336f;
            font: 700 13px Inter, Arial, sans-serif;
            line-height: 1;
        }
        .route-map-legend-row {
            display: flex;
            align-items: center;
            gap: 10px;
            min-height: 24px;
            margin-bottom: 10px;
            white-space: nowrap;
        }
        .route-map-legend-row:last-child { margin-bottom: 0; }
        .route-map-legend-icon {
            width: 23px;
            height: 23px;
            border-radius: 50%;
            display: inline-grid;
            place-items: center;
            color: #ffffff;
            flex: 0 0 23px;
        }
        .route-map-legend-icon.cd { background: #0b63f6; }
        .route-map-legend-icon.store { background: #4aa43f; }
        .route-map-legend-line {
            width: 32px;
            height: 4px;
            border-radius: 999px;
            background: #2563eb;
            flex: 0 0 32px;
        }
        </style>
        {% endmacro %}
        {% macro script(this, kwargs) %}
        var routeMapLegend = L.control({position: 'topright'});
        routeMapLegend.onAdd = function(map) {
            var div = L.DomUtil.create('div', 'route-map-legend leaflet-control');
            div.setAttribute('style', 'min-width:136px;padding:14px 16px;background:rgba(255,255,255,.97);border:1px solid #dbe7f6;border-radius:10px;box-shadow:0 8px 22px rgba(12,32,68,.16);color:#18336f;font:700 13px Inter,Arial,sans-serif;line-height:1;');
            div.innerHTML = `
                <div style="display:flex;align-items:center;gap:10px;min-height:24px;margin-bottom:10px;white-space:nowrap;">
                    <span style="width:23px;height:23px;border-radius:50%;display:inline-grid;place-items:center;color:#fff;background:#0b63f6;flex:0 0 23px;">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m3 11 9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/></svg>
                    </span>
                    <span>CD</span>
                </div>
                <div style="display:flex;align-items:center;gap:10px;min-height:24px;margin-bottom:10px;white-space:nowrap;">
                    <span style="width:23px;height:23px;border-radius:50%;display:inline-grid;place-items:center;color:#fff;background:#4aa43f;flex:0 0 23px;">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10h16l-1.5-6h-13L4 10Z"/><path d="M4 10v2a3 3 0 0 0 5 2.2A3 3 0 0 0 12 15a3 3 0 0 0 3-0.8A3 3 0 0 0 20 12v-2"/><path d="M6 15v6h12v-6"/></svg>
                    </span>
                    <span>Loja</span>
                </div>
                <div style="display:flex;align-items:center;gap:10px;min-height:24px;white-space:nowrap;">
                    <span style="width:32px;height:4px;border-radius:999px;background:#2563eb;flex:0 0 32px;"></span>
                    <span>Rota calculada</span>
                </div>`;
            L.DomEvent.disableClickPropagation(div);
            return div;
        };
        routeMapLegend.addTo({{this._parent.get_name()}});
        {% endmacro %}
        """
    )
    map_obj.add_child(legend)


def map_marker_icon(kind: str) -> folium.DivIcon:
    if kind == "cd":
        background = "#0b63f6"
        svg_path = '<path d="m3 11 9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/>'
    else:
        background = "#4aa43f"
        svg_path = '<path d="M4 10h16l-1.5-6h-13L4 10Z"/><path d="M4 10v2a3 3 0 0 0 5 2.2A3 3 0 0 0 12 15a3 3 0 0 0 3-0.8A3 3 0 0 0 20 12v-2"/><path d="M6 15v6h12v-6"/>'

    return folium.DivIcon(
        html=(
            f'<div style="width:34px;height:34px;border-radius:50% 50% 50% 0;'
            f'background:{background};display:grid;place-items:center;transform:rotate(-45deg);'
            'box-shadow:0 5px 12px rgba(12,32,68,.24);border:2px solid #fff;">'
            '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" '
            'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" '
            f'style="transform:rotate(45deg);">{svg_path}</svg></div>'
        ),
        icon_size=(34, 34),
        icon_anchor=(17, 34),
        popup_anchor=(0, -34),
    )


def style_folium_embed(map_obj: folium.Map) -> None:
    map_obj.get_root().html.add_child(
        folium.Element(
            """
            <style>
            html, body {
                width: 100%;
                height: 100%;
                margin: 0;
                padding: 0;
                font-size: 0;
                line-height: 0;
                background: #f7fbff;
                overflow: hidden;
            }

            #root,
            #parent,
            .float-container,
            .float-child,
            #map_div,
            #map_div2,
            .folium-map,
            .leaflet-container {
                width: 100% !important;
                height: 100% !important;
                background: #f7fbff !important;
                border-radius: 18px;
                overflow: hidden;
            }

            body > span,
            .leaflet-control-container + span {
                display: none !important;
            }

            body > div[style*="z-index: 9999"] {
                display: none !important;
            }

            .leaflet-pane,
            .leaflet-map-pane,
            .leaflet-tile-pane,
            .leaflet-overlay-pane {
                background: transparent !important;
            }
            </style>
            """
        )
    )


def render_map(cd: pd.Series | None, stores: pd.DataFrame, report: pd.DataFrame, config: dict[str, Any]) -> None:
    if cd is None:
        st.warning("Cadastre um Centro de Distribuicao para visualizar o mapa.")
        return

    map_center = [float(cd["latitude"]), float(cd["longitude"])]
    route_map = folium.Map(location=map_center, zoom_start=11, tiles="OpenStreetMap")

    folium.Marker(
        location=map_center,
        popup=f"<strong>{cd['nome']}</strong><br>{cd['endereco']}",
        tooltip="Centro de Distribuicao",
        icon=map_marker_icon("cd"),
    ).add_to(route_map)

    report_by_id = report.set_index("id") if not report.empty else pd.DataFrame()
    distance_label = "Ida e volta" if config["ida_e_volta"] else "Somente ida"

    pending_count = int(report["Status rota"].eq("Pendente").sum()) if "Status rota" in report else 0
    error_count = int(report["Status rota"].eq("Erro").sum()) if "Status rota" in report else 0
    if pending_count:
        st.info(f"{pending_count} loja(s) ainda estao sem rota calculada. Clique em Recalcular rota do destino selecionado.")
    if error_count:
        st.warning(f"{error_count} loja(s) tiveram erro no calculo de rota real.")

    for _, store in stores.iterrows():
        store_location = [float(store["latitude"]), float(store["longitude"])]
        metrics = report_by_id.loc[int(store["id"])] if not report_by_id.empty else None

        if metrics is not None and metrics["Status rota"] == "Calculada":
            duration_text = format_duration_minutes(metrics.get("Tempo estimado min"))
            if duration_text == "-":
                duration_text = "Nao disponivel"
            popup = f"""
            <strong>{store['nome']}</strong><br>
            {store['endereco']}<br>
            Tipo de calculo: Rota real via OSRM<br>
            Distancia ida: {metrics['Distancia ida em km']:.2f} km<br>
            Distancia considerada: {distance_label}<br>
            Tempo estimado: {duration_text}<br>
            Litros estimados: {metrics['Litros estimados']:.2f} L<br>
            Custo estimado: {currency_brl(metrics['Custo estimado'])}
            """
            route_points = metrics.get("Geometry", [])
        else:
            status_text = "Pendente" if metrics is None else metrics.get("Status rota", "Pendente")
            error_text = "Clique em Recalcular rota do destino selecionado." if metrics is None else metrics.get("Erro rota", "")
            popup = f"""
            <strong>{store['nome']}</strong><br>
            {store['endereco']}<br>
            Status da rota: {status_text}<br>
            {error_text}
            """
            route_points = []

        folium.Marker(
            location=store_location,
            popup=folium.Popup(popup, max_width=320),
            tooltip=str(store["nome"]),
            icon=map_marker_icon("store"),
        ).add_to(route_map)

        if route_points:
            folium.PolyLine(
                locations=route_points,
                color="#2563eb",
                weight=6,
                opacity=0.9,
            ).add_to(route_map)

    if not stores.empty:
        bounds = [map_center] + [
            [float(store["latitude"]), float(store["longitude"])] for _, store in stores.iterrows()
        ]
        route_map.fit_bounds(bounds, padding=(24, 24))

    route_map.get_root().html.add_child(
        folium.Element(
            """
            <div style="position: fixed; top: 24px; right: 24px; z-index: 9999; background: white; border: 1px solid #dbe7f6; border-radius: 10px; padding: 14px 16px; box-shadow: 0 8px 20px rgba(12,32,68,.14); color: #18336f; font: 600 13px Inter, Arial, sans-serif;">
                <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px;"><span style="width:22px;height:22px;border-radius:50%;background:#0b63f6;color:white;display:inline-grid;place-items:center;font-size:12px;">⌂</span>CD</div>
                <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px;"><span style="width:22px;height:22px;border-radius:50%;background:#4aa43f;color:white;display:inline-grid;place-items:center;font-size:12px;">■</span>Loja</div>
                <div style="display:flex; align-items:center; gap:10px;"><span style="width:28px;height:4px;border-radius:999px;background:#2563eb;display:inline-block;"></span>Rota calculada</div>
            </div>
            """
        )
    )
    route_map.get_root().html.add_child(
        folium.Element(
            """
            <style>
            .route-map-legend {
                position: fixed;
                top: 24px;
                right: 24px;
                z-index: 10000;
                min-width: 136px;
                padding: 14px 16px;
                background: rgba(255, 255, 255, 0.97);
                border: 1px solid #dbe7f6;
                border-radius: 10px;
                box-shadow: 0 8px 22px rgba(12, 32, 68, 0.16);
                color: #18336f;
                font: 700 13px Inter, Arial, sans-serif;
                line-height: 1;
            }
            .route-map-legend-row {
                display: flex;
                align-items: center;
                gap: 10px;
                min-height: 24px;
                margin-bottom: 10px;
                white-space: nowrap;
            }
            .route-map-legend-row:last-child {
                margin-bottom: 0;
            }
            .route-map-legend-icon {
                width: 23px;
                height: 23px;
                border-radius: 50%;
                display: inline-grid;
                place-items: center;
                color: #ffffff;
                flex: 0 0 23px;
            }
            .route-map-legend-icon.cd { background: #0b63f6; }
            .route-map-legend-icon.store { background: #4aa43f; }
            .route-map-legend-line {
                width: 32px;
                height: 4px;
                border-radius: 999px;
                background: #2563eb;
                flex: 0 0 32px;
            }
            </style>
            <div class="route-map-legend">
                <div class="route-map-legend-row">
                    <span class="route-map-legend-icon cd">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m3 11 9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/></svg>
                    </span>
                    <span>CD</span>
                </div>
                <div class="route-map-legend-row">
                    <span class="route-map-legend-icon store">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10h16l-1.5-6h-13L4 10Z"/><path d="M4 10v2a3 3 0 0 0 5 2.2A3 3 0 0 0 12 15a3 3 0 0 0 3-0.8A3 3 0 0 0 20 12v-2"/><path d="M6 15v6h12v-6"/></svg>
                    </span>
                    <span>Loja</span>
                </div>
                <div class="route-map-legend-row">
                    <span class="route-map-legend-line"></span>
                    <span>Rota calculada</span>
                </div>
            </div>
            """
        )
    )
    add_route_map_legend(route_map)
    style_folium_embed(route_map)
    with st.container(key="map_shell"):
        st_folium(route_map, height=610, use_container_width=True, key="route_map_main")


def render_map_page(cd: pd.Series | None, stores: pd.DataFrame, report: pd.DataFrame, config: dict[str, Any]) -> None:
    selected_map_stores = stores.iloc[0:0].copy()
    selected_map_report = report.iloc[0:0].copy()

    if cd is not None and not stores.empty:
        selected_id = st.session_state.get("mapa_loja_destino_unica")
        if selected_id is not None:
            selected_map_report = report.loc[report["id"].astype(int) == int(selected_id)].copy() if not report.empty else selected_map_report

    render_map_header(selected_map_report, stores, report, config)

    if cd is None:
        st.warning("Cadastre um Centro de Distribuicao para visualizar o mapa.")
        return
    if stores.empty:
        st.info("Cadastre ao menos uma loja para selecionar um destino.")
        return

    filter_col, detail_col = st.columns([0.96, 1.18], gap="small")
    with filter_col:
        with st.container(key="map_filters_panel"):
            st.markdown('<div class="map-panel-title">Filtros do mapa</div>', unsafe_allow_html=True)
            selected_map_stores, selected_map_report = select_destination_store(cd, stores, report)
            if not selected_map_stores.empty:
                render_map_route_actions(
                    cd,
                    selected_map_stores,
                    key="recalcular_rota_destino_mapa",
                )

    with detail_col:
        with st.container(key="map_details_panel"):
            render_map_route_details(selected_map_stores, selected_map_report, config)

    if not selected_map_stores.empty:
        render_map(cd, selected_map_stores, selected_map_report, config)
        render_map_route_points(cd, selected_map_stores, selected_map_report, config)


def app_icon(name: str, size: int = 28) -> str:
    icons = {
        "map-pin": '<path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
        "home": '<path d="m3 11 9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/><path d="M9 14h6"/>',
        "store": '<path d="M4 10h16l-1.5-6h-13L4 10Z"/><path d="M4 10v2a3 3 0 0 0 5 2.2A3 3 0 0 0 12 15a3 3 0 0 0 3-0.8A3 3 0 0 0 20 12v-2"/><path d="M6 15v6h12v-6"/><path d="M9 21v-3h6v3"/>',
        "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v6l4 2"/>',
        "settings": '<path d="M12 15.5A3.5 3.5 0 1 0 12 8a3.5 3.5 0 0 0 0 7.5Z"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.05.05a2 2 0 1 1-2.83 2.83l-.05-.05A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6l-.08.08a2 2 0 1 1-2.83-2.83l.08-.08a1.7 1.7 0 0 0 .6-1 1.7 1.7 0 0 0-.34-1.88l-.05-.05a2 2 0 1 1 2.83-2.83l.05.05a1.7 1.7 0 0 0 1.88.34 1.7 1.7 0 0 0 1-.6l.08-.08a2 2 0 1 1 2.83 2.83l-.08.08a1.7 1.7 0 0 0-.6 1Z"/>',
        "map": '<path d="m9 18-6 3V6l6-3 6 3 6-3v15l-6 3-6-3Z"/><path d="M9 3v15"/><path d="M15 6v15"/>',
        "alert": '<circle cx="12" cy="12" r="9"/><path d="M12 7v6"/><path d="M12 17h.01"/>',
        "check": '<circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/>',
        "upload": '<path d="M12 3v12"/><path d="m7 8 5-5 5 5"/><path d="M5 21h14"/>',
        "route": '<circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><path d="M8 18h3a3 3 0 0 0 0-6h2a3 3 0 0 0 3-3V8"/>',
        "fuel": '<path d="M4 3h9v18H4Z"/><path d="M8 7h1"/><path d="M13 7h2l3 3v8a2 2 0 0 0 4 0v-6l-3-3"/><path d="M7 21v-4h3v4"/>',
        "money": '<circle cx="12" cy="12" r="9"/><path d="M12 7v10"/><path d="M15 9.5c-.8-.7-1.7-1-3-1-1.7 0-3 .8-3 2s1.3 1.8 3 1.8 3 .6 3 1.9-1.3 2.3-3 2.3c-1.3 0-2.4-.4-3.2-1.2"/>',
        "refresh": '<path d="M20 12a8 8 0 0 1-14.8 4.2"/><path d="M4 16v5h5"/><path d="M4 12A8 8 0 0 1 18.8 7.8"/><path d="M20 8V3h-5"/>',
        "save": '<path d="M5 3h12l2 2v16H5V3Z"/><path d="M7 3v7h9V3"/><path d="M9 17h6"/>',
        "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
        "trash": '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 15h10l1-15"/><path d="M10 11v6"/><path d="M14 11v6"/>',
        "eraser": '<path d="m7 21 10-10"/><path d="m21 7-4-4L4 16l5 5h12"/><path d="M14 6l4 4"/>',
        "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
        "card": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 9h.01"/><path d="M7 13h10"/>',
        "crosshair": '<circle cx="12" cy="12" r="8"/><path d="M12 2v4"/><path d="M12 18v4"/><path d="M2 12h4"/><path d="M18 12h4"/><circle cx="12" cy="12" r="2"/>',
        "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18"/><path d="M12 3a14 14 0 0 0 0 18"/>',
        "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
        "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.7 2.7 0 0 1 5 1.4c0 1.9-2.5 2.1-2.5 4"/><path d="M12 18h.01"/>',
        "chevron": '<path d="m9 18 6-6-6-6"/>',
    }
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
        f'{icons.get(name, icons["home"])}</svg>'
    )


def render_dashboard_styles() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

        :root {
            --summary-ink: #071244;
            --summary-text: #18336f;
            --summary-muted: #5b6f9c;
            --summary-line: #e2e8f3;
            --summary-blue: #0b63f6;
            --summary-purple: #7b31d8;
            --summary-orange: #f7a51c;
            --summary-green: #14a64a;
            --summary-card: #ffffff;
            --summary-bg: #fbfdff;
        }

        html, body, [class*="css"] {
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }

        .stApp {
            background:
                radial-gradient(circle at 73% 17%, rgba(11, 99, 246, 0.04), transparent 30%),
                linear-gradient(180deg, #ffffff 0%, #fbfdff 100%);
        }

        [data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"] {
            display: none;
        }

        [data-testid="stSidebar"] {
            width: 238px !important;
            min-width: 238px !important;
            max-width: 238px !important;
            background: #ffffff;
            border-right: 1px solid #dbe3f0;
            box-shadow: 16px 0 34px rgba(12, 32, 68, 0.06);
        }

        [data-testid="stSidebarHeader"],
        [data-testid="stSidebarCollapsedControl"] {
            display: none !important;
        }

        [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
            padding-top: 0 !important;
        }

        [data-testid="stSidebar"] > div:first-child {
            padding: 0 8px 24px !important;
        }

        [data-testid="stSidebarContent"],
        [data-testid="stSidebarUserContent"] {
            padding: 28px 8px 24px !important;
        }

        [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
            gap: 0;
        }

        .block-container {
            max-width: 1580px;
            padding: 20px 46px 28px 38px !important;
        }

        .mobile-nav {
            display: none;
        }

        .sidebar-logo {
            display: flex;
            align-items: center;
            gap: 16px;
            color: var(--summary-ink);
            font-size: 22px;
            font-weight: 800;
            margin: 0 16px 70px;
        }

        .sidebar-logo-mark {
            width: 48px;
            height: 48px;
            border-radius: 13px;
            display: grid;
            place-items: center;
            color: #ffffff;
            background: linear-gradient(135deg, #0b63f6, #0451df);
            box-shadow: 0 12px 26px rgba(11, 99, 246, 0.22);
        }

        .sidebar-nav {
            display: flex;
            flex-direction: column;
            gap: 14px;
        }

        [data-testid="stSidebar"] .stButton {
            margin-bottom: 14px;
        }

        [data-testid="stSidebar"] .stButton > button {
            position: relative;
            justify-content: flex-start;
            width: 100%;
            height: 58px;
            padding: 0 12px 0 56px;
            border: 0;
            border-radius: 10px;
            background: transparent;
            color: #19346b;
            box-shadow: none;
            font-size: 16px;
            font-weight: 500;
        }

        [data-testid="stSidebar"] .stButton > button p {
            margin: 0;
            font-size: 16px;
            line-height: 1;
            white-space: nowrap;
        }

        [data-testid="stSidebar"] .stButton > button:after {
            content: "";
            position: absolute;
            left: 24px;
            top: 50%;
            width: 25px;
            height: 25px;
            transform: translateY(-50%);
            background: #37538b;
            mask-position: center;
            mask-repeat: no-repeat;
            mask-size: contain;
            -webkit-mask-position: center;
            -webkit-mask-repeat: no-repeat;
            -webkit-mask-size: contain;
        }

        [data-testid="stSidebar"] .stButton > button[kind="primary"]:after,
        [data-testid="stSidebar"] .stButton > button:hover:after {
            background: #0b63f6;
        }

        .st-key-nav_resumo button:after {
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cpath d='M12 3v9h9'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cpath d='M12 3v9h9'/%3E%3C/svg%3E");
        }

        .st-key-nav_configuracoes button:after {
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M12 15.5A3.5 3.5 0 1 0 12 8a3.5 3.5 0 0 0 0 7.5Z'/%3E%3Cpath d='M19.4 13.5c.1-.5.1-1 .1-1.5s0-1-.1-1.5l2.1-1.6-2-3.5-2.5 1a8 8 0 0 0-2.6-1.5L14 2h-4l-.4 2.9A8 8 0 0 0 7 6.4l-2.5-1-2 3.5 2.1 1.6A8.8 8.8 0 0 0 4.5 12c0 .5 0 1 .1 1.5l-2.1 1.6 2 3.5 2.5-1a8 8 0 0 0 2.6 1.5L10 22h4l.4-2.9a8 8 0 0 0 2.6-1.5l2.5 1 2-3.5-2.1-1.6Z'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M12 15.5A3.5 3.5 0 1 0 12 8a3.5 3.5 0 0 0 0 7.5Z'/%3E%3Cpath d='M19.4 13.5c.1-.5.1-1 .1-1.5s0-1-.1-1.5l2.1-1.6-2-3.5-2.5 1a8 8 0 0 0-2.6-1.5L14 2h-4l-.4 2.9A8 8 0 0 0 7 6.4l-2.5-1-2 3.5 2.1 1.6A8.8 8.8 0 0 0 4.5 12c0 .5 0 1 .1 1.5l-2.1 1.6 2 3.5 2.5-1a8 8 0 0 0 2.6 1.5L10 22h4l.4-2.9a8 8 0 0 0 2.6-1.5l2.5 1 2-3.5-2.1-1.6Z'/%3E%3C/svg%3E");
        }

        .st-key-nav_cd button:after {
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='m3 10 9-7 9 7'/%3E%3Cpath d='M5 9v12h14V9'/%3E%3Cpath d='M9 21v-6h6v6'/%3E%3Cpath d='M9 12h.01M15 12h.01'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='m3 10 9-7 9 7'/%3E%3Cpath d='M5 9v12h14V9'/%3E%3Cpath d='M9 21v-6h6v6'/%3E%3Cpath d='M9 12h.01M15 12h.01'/%3E%3C/svg%3E");
        }

        .st-key-nav_lojas button:after {
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M4 10h16l-1.5-6h-13L4 10Z'/%3E%3Cpath d='M4 10v2a3 3 0 0 0 5 2.2A3 3 0 0 0 12 15a3 3 0 0 0 3-0.8A3 3 0 0 0 20 12v-2'/%3E%3Cpath d='M6 15v6h12v-6'/%3E%3Cpath d='M9 21v-3h6v3'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M4 10h16l-1.5-6h-13L4 10Z'/%3E%3Cpath d='M4 10v2a3 3 0 0 0 5 2.2A3 3 0 0 0 12 15a3 3 0 0 0 3-0.8A3 3 0 0 0 20 12v-2'/%3E%3Cpath d='M6 15v6h12v-6'/%3E%3Cpath d='M9 21v-3h6v3'/%3E%3C/svg%3E");
        }

        .st-key-nav_mapa button:after {
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='m9 18-6 3V6l6-3 6 3 6-3v15l-6 3-6-3Z'/%3E%3Cpath d='M9 3v15'/%3E%3Cpath d='M15 6v15'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='m9 18-6 3V6l6-3 6 3 6-3v15l-6 3-6-3Z'/%3E%3Cpath d='M9 3v15'/%3E%3Cpath d='M15 6v15'/%3E%3C/svg%3E");
        }

        .st-key-nav_ajuda {
            position: fixed;
            left: 21px;
            bottom: 34px;
            width: 178px;
            padding-top: 22px;
            border-top: 1px solid #dbe3f0;
        }

        .st-key-nav_ajuda button:after {
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cpath d='M9.5 9a2.7 2.7 0 0 1 5 1.4c0 1.9-2.5 2.1-2.5 4'/%3E%3Cpath d='M12 18h.01'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cpath d='M9.5 9a2.7 2.7 0 0 1 5 1.4c0 1.9-2.5 2.1-2.5 4'/%3E%3Cpath d='M12 18h.01'/%3E%3C/svg%3E");
        }

        [data-testid="stSidebar"] .stButton > button:hover {
            border: 0;
            background: rgba(11, 99, 246, 0.06);
            color: #0057ff;
        }

        [data-testid="stSidebar"] .stButton > button:focus:not(:active) {
            border: 0;
            box-shadow: none;
            color: #0057ff;
        }

        [data-testid="stSidebar"] .stButton > button[kind="primary"] {
            color: #0057ff;
            background: linear-gradient(90deg, rgba(11, 99, 246, 0.10), rgba(11, 99, 246, 0.045));
            font-weight: 700;
        }

        [data-testid="stSidebar"] .stButton > button[kind="primary"]:before {
            content: "";
            position: absolute;
            left: -18px;
            top: 0;
            bottom: 0;
            width: 6px;
            border-radius: 0 10px 10px 0;
            background: #0b63f6;
        }

        .sidebar-link {
            position: relative;
            display: flex;
            align-items: center;
            gap: 18px;
            height: 58px;
            padding: 0 18px;
            border-radius: 10px;
            color: #19346b !important;
            text-decoration: none !important;
            font-size: 16px;
            font-weight: 500;
        }

        .sidebar-link svg {
            color: #37538b;
            width: 27px;
            height: 27px;
        }

        .sidebar-link.active {
            color: #0057ff !important;
            background: linear-gradient(90deg, rgba(11, 99, 246, 0.10), rgba(11, 99, 246, 0.045));
            font-weight: 700;
        }

        .sidebar-link.active:before {
            content: "";
            position: absolute;
            left: -18px;
            top: 0;
            bottom: 0;
            width: 6px;
            border-radius: 0 10px 10px 0;
            background: #0b63f6;
        }

        .sidebar-link.active svg {
            color: #0b63f6;
        }

        .sidebar-help {
            position: fixed;
            left: 21px;
            bottom: 34px;
            width: 178px;
            padding-top: 22px;
            border-top: 1px solid #dbe3f0;
        }

        .help-shell {
            color: var(--summary-text);
        }

        .help-grid {
            display: grid;
            grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.05fr);
            gap: 22px;
            align-items: start;
        }

        .help-column {
            display: grid;
            gap: 22px;
        }

        .help-card {
            padding: 24px 28px;
            border: 1px solid #dce4f0;
            border-radius: 10px;
            background: #ffffff;
            box-shadow: 0 6px 18px rgba(12, 32, 68, 0.06);
        }

        .help-card-title {
            color: var(--summary-ink);
            font-size: 20px;
            font-weight: 800;
            margin-bottom: 22px;
        }

        .help-steps {
            display: grid;
        }

        .help-step {
            position: relative;
            display: grid;
            grid-template-columns: 36px minmax(0, 1fr);
            gap: 18px;
            align-items: center;
            min-height: 66px;
        }

        .help-step:not(:last-child) {
            border-bottom: 1px solid #e4eaf4;
        }

        .help-step:not(:last-child):before {
            content: "";
            position: absolute;
            left: 17px;
            top: 45px;
            bottom: -21px;
            border-left: 1px dashed #c7d4ea;
        }

        .help-step-number {
            position: relative;
            z-index: 1;
            width: 31px;
            height: 31px;
            border-radius: 50%;
            display: grid;
            place-items: center;
            color: #ffffff;
            background: #0b63f6;
            font-size: 14px;
            font-weight: 800;
        }

        .help-step-text, .help-chip, .help-tip-text, .help-practice-text {
            color: #4963a5;
            font-size: 14px;
            font-weight: 700;
            line-height: 1.35;
        }

        .help-sheet-copy {
            color: #4963a5;
            font-size: 14px;
            font-weight: 700;
            margin-bottom: 18px;
        }

        .help-chips {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 20px;
        }

        .help-chip {
            min-width: 58px;
            text-align: center;
            padding: 9px 13px;
            border-radius: 8px;
            color: #0b63f6;
            background: #eef5ff;
        }

        .help-info-line {
            display: flex;
            align-items: center;
            gap: 12px;
            min-height: 42px;
            padding: 10px 14px;
            border: 1px solid #bfd8ff;
            border-radius: 8px;
            color: #285fb8;
            background: #f3f8ff;
            font-size: 14px;
            font-weight: 700;
        }

        .help-problem-list {
            display: grid;
        }

        .help-problem {
            display: grid;
            grid-template-columns: 56px minmax(0, 1fr);
            gap: 16px;
            align-items: center;
            min-height: 72px;
            border-bottom: 1px solid #e4eaf4;
        }

        .help-problem:last-child {
            border-bottom: 0;
        }

        .help-problem-icon {
            width: 42px;
            height: 42px;
            border-radius: 50%;
            display: grid;
            place-items: center;
        }

        .help-problem-icon.purple { color: #8d36e8; background: #f2e8ff; }
        .help-problem-icon.orange { color: #f59e0b; background: #fff0d6; }
        .help-problem-icon.blue { color: #0b63f6; background: #eaf2ff; }

        .help-problem-title {
            color: var(--summary-ink);
            font-size: 15px;
            font-weight: 800;
            margin-bottom: 5px;
        }

        .help-problem-copy {
            color: #4963a5;
            font-size: 14px;
            font-weight: 700;
            line-height: 1.3;
        }

        .help-compact-list {
            display: grid;
            gap: 10px;
        }

        .help-tip, .help-practice {
            display: grid;
            grid-template-columns: 24px minmax(0, 1fr);
            gap: 10px;
            align-items: start;
        }

        .help-check-icon {
            color: #10a24a;
        }

        .help-practices {
            display: grid;
            grid-template-columns: 52px minmax(0, 1fr);
            align-items: center;
            gap: 16px;
        }

        .help-shield {
            width: 42px;
            height: 42px;
            border-radius: 50%;
            display: grid;
            place-items: center;
            color: #0b63f6;
            background: #eaf2ff;
        }

        .summary-shell {
            color: var(--summary-text);
        }

        .summary-topbar {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            margin-bottom: 20px;
        }

        .summary-title h1,
        .summary-h1 {
            color: var(--summary-ink);
            font-size: 35px;
            line-height: 1.08;
            font-weight: 800;
            margin: 0 0 9px;
            letter-spacing: 0;
        }

        .summary-title p {
            color: #223b78;
            font-size: 16px;
            margin: 0;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 24px;
            margin-bottom: 24px;
        }

        .summary-card {
            background: rgba(255, 255, 255, 0.94);
            border: 1px solid var(--summary-line);
            border-radius: 9px;
            box-shadow: 0 4px 14px rgba(12, 32, 68, 0.07);
        }

        .stat-card {
            height: 102px;
            display: flex;
            align-items: center;
            gap: 24px;
            padding: 0 28px;
        }

        .icon-bubble {
            width: 56px;
            height: 56px;
            flex: 0 0 56px;
            border-radius: 50%;
            display: grid;
            place-items: center;
        }

        .icon-bubble.blue { color: #0b63f6; background: #eaf2ff; }
        .icon-bubble.orange { color: #f7a51c; background: #fff2d9; }
        .icon-bubble.purple { color: #7b31d8; background: #f0e8ff; }
        .icon-bubble.green { color: #10a24a; background: #dcf7e7; }

        .stat-label {
            color: #27427f;
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 7px;
        }

        .stat-value {
            color: var(--summary-ink);
            font-size: 27px;
            line-height: 1;
            font-weight: 800;
        }

        .dashboard-grid {
            display: grid;
            grid-template-columns: 1fr 1.02fr;
            gap: 18px;
            margin-bottom: 18px;
        }

        .secondary-grid {
            display: grid;
            grid-template-columns: 0.82fr 1fr;
            gap: 18px;
            margin-bottom: 18px;
        }

        .panel {
            padding: 21px 28px 14px;
        }

        .panel h2,
        .panel-title {
            color: var(--summary-ink);
            font-size: 18px;
            line-height: 1.2;
            font-weight: 800;
            margin: 0 0 16px;
            letter-spacing: 0;
        }

        .info-row, .status-row, .activity-row {
            display: grid;
            align-items: center;
            min-height: 52px;
            border-bottom: 1px solid var(--summary-line);
        }

        .info-row {
            grid-template-columns: 58px 1fr 1fr;
        }

        .info-row:last-child, .status-row:last-child, .activity-row:last-child {
            border-bottom: 0;
        }

        .mini-icon {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            display: grid;
            place-items: center;
        }

        .mini-icon svg {
            width: 23px;
            height: 23px;
        }

        .info-name, .status-name, .activity-name {
            color: #18336f;
            font-size: 15px;
            font-weight: 500;
        }

        .info-value, .activity-detail, .activity-date {
            color: #243f7a;
            font-size: 15px;
            font-weight: 500;
        }

        .status-row {
            grid-template-columns: 58px 1fr auto;
        }

        .pill {
            min-width: 92px;
            text-align: center;
            border-radius: 8px;
            padding: 8px 14px;
            font-size: 15px;
            font-weight: 500;
        }

        .pill.green { color: #08833d; background: #def6e8; border: 1px solid #bfe9cf; }
        .pill.orange { color: #f08a00; background: #fff2dc; border: 1px solid #ffe0aa; }
        .pill.blue { color: #0057ff; background: #edf5ff; border: 1px solid #dcecff; }

        .store-summary {
            display: grid;
            grid-template-columns: 185px 1fr;
            align-items: center;
            gap: 24px;
            padding-top: 4px;
        }

        .donut {
            width: 143px;
            height: 143px;
            border-radius: 50%;
            background: conic-gradient(#0b63f6 0 360deg, #f7a51c 360deg 360deg);
            display: grid;
            place-items: center;
            margin-left: 12px;
            box-shadow: inset 0 0 0 1px rgba(12, 32, 68, 0.04);
        }

        .donut:before {
            content: "";
            width: 84px;
            height: 84px;
            border-radius: 50%;
            background: #ffffff;
            position: absolute;
        }

        .donut-center {
            position: relative;
            z-index: 1;
            text-align: center;
            color: var(--summary-ink);
        }

        .donut-total {
            font-size: 27px;
            line-height: 1;
            font-weight: 800;
        }

        .donut-caption {
            font-size: 15px;
            margin-top: 6px;
            color: #314878;
        }

        .legend-row {
            display: grid;
            grid-template-columns: 16px 1fr auto;
            align-items: center;
            gap: 10px;
            min-height: 56px;
            border-bottom: 1px solid var(--summary-line);
        }

        .legend-dot {
            width: 13px;
            height: 13px;
            border-radius: 50%;
        }

        .legend-dot.blue { background: #0b63f6; }
        .legend-dot.orange { background: #f7a51c; }
        .legend-name {
            font-size: 15px;
            color: #18336f;
        }

        .legend-value {
            color: var(--summary-ink);
            font-size: 22px;
            font-weight: 800;
        }

        .actions-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 18px;
        }

        .action-card {
            min-height: 104px;
            padding: 20px 14px 16px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 12px;
            text-align: center;
            color: #18336f !important;
            text-decoration: none !important;
            border-radius: 9px;
            transition: transform 150ms ease, box-shadow 150ms ease;
        }

        a.action-card {
            cursor: pointer;
        }

        .action-card:hover {
            transform: translateY(-1px);
            box-shadow: 0 8px 20px rgba(12, 32, 68, 0.10);
        }

        .action-label {
            max-width: 120px;
            font-size: 16px;
            line-height: 1.18;
            font-weight: 500;
        }

        .activity-panel {
            padding: 18px 28px 16px;
        }

        .summary-attention-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 12px;
        }

        .summary-attention-item {
            min-height: 72px;
            display: grid;
            grid-template-columns: 40px minmax(0, 1fr) 20px;
            align-items: center;
            gap: 12px;
            padding: 14px;
            border: 1px solid var(--summary-line);
            border-radius: 9px;
            background: #fbfdff;
            color: #18336f !important;
            text-decoration: none !important;
        }

        .summary-attention-title {
            color: var(--summary-ink);
            font-size: 15px;
            font-weight: 800;
            line-height: 1.15;
        }

        .summary-attention-detail {
            color: #5b6f9c;
            font-size: 13px;
            line-height: 1.25;
            margin-top: 4px;
        }

        .summary-attention-go {
            color: #5b6f9c;
            display: grid;
            place-items: center;
        }

        .activity-head, .activity-row {
            grid-template-columns: 0.42fr 0.5fr 0.22fr;
            column-gap: 24px;
        }

        .activity-head {
            display: grid;
            color: #233f78;
            font-size: 13px;
            font-weight: 500;
            padding: 8px 8px 10px;
            border-bottom: 1px solid var(--summary-line);
        }

        .activity-row {
            min-height: 43px;
            padding: 0 8px;
        }

        .activity-title {
            display: flex;
            align-items: center;
            gap: 14px;
        }

        .activity-row .mini-icon {
            width: 32px;
            height: 32px;
        }

        .activity-row .mini-icon svg {
            width: 19px;
            height: 19px;
        }

        .config-shell {
            color: var(--summary-text);
        }

        .config-stats-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 24px;
            margin-bottom: 24px;
        }

        .config-stat-card {
            min-height: 146px;
            display: flex;
            align-items: center;
            gap: 28px;
            padding: 22px 28px;
        }

        .config-stat-card .icon-bubble {
            width: 78px;
            height: 78px;
            flex-basis: 78px;
        }

        .config-stat-card .icon-bubble svg {
            width: 40px;
            height: 40px;
        }

        .config-stat-value {
            color: var(--summary-ink);
            font-size: 28px;
            line-height: 1.05;
            font-weight: 800;
            margin-top: 12px;
        }

        .config-stat-sub {
            margin-top: 8px;
            color: var(--summary-muted);
            font-size: 15px;
            font-weight: 500;
        }

        .config-columns {
            gap: 24px;
        }

        .config-panel {
            background: rgba(255, 255, 255, 0.94);
            border: 1px solid var(--summary-line);
            border-radius: 9px;
            box-shadow: 0 4px 14px rgba(12, 32, 68, 0.07);
            padding: 28px;
        }

        .st-key-config_form_panel {
            background: rgba(255, 255, 255, 0.94);
            border: 1px solid var(--summary-line);
            border-radius: 9px;
            box-shadow: 0 4px 14px rgba(12, 32, 68, 0.07);
            padding: 30px 28px 36px;
        }

        .st-key-config_form_panel .stMarkdown .panel-title {
            margin-bottom: 28px;
        }

        .config-section-label {
            color: #314878;
            font-size: 13px;
            font-weight: 800;
            letter-spacing: 0;
            margin: 22px 0 10px;
            text-transform: uppercase;
        }

        .config-inline-notice {
            display: grid;
            grid-template-columns: 34px minmax(0, 1fr);
            align-items: center;
            gap: 12px;
            min-height: 48px;
            padding: 10px 12px;
            margin: 0 0 12px;
            border-radius: 8px;
            color: #18336f;
            font-size: 14px;
            font-weight: 700;
            line-height: 1.35;
        }

        .config-inline-notice.success {
            border: 1px solid #c7eed4;
            background: #f2fbf5;
        }

        .config-inline-notice.warning {
            border: 1px solid #ffe0a6;
            background: #fff8e8;
        }

        .st-key-config_form_panel label p,
        .st-key-config_form_panel [data-testid="stWidgetLabel"] p {
            color: var(--summary-ink);
            font-size: 16px;
            font-weight: 700;
        }

        .st-key-config_form_panel [data-testid="stTextInput"] div[data-baseweb="input"] {
            height: 56px;
            border-radius: 8px;
            border: 1px solid #d7deea;
            background: #ffffff;
            box-shadow: inset 0 0 0 1px rgba(215, 222, 234, 0.25);
        }

        .st-key-config_form_panel [data-testid="stTextInput"] input {
            height: 56px;
            background: #ffffff !important;
            color: var(--summary-ink);
            -webkit-text-fill-color: var(--summary-ink);
            font-size: 21px;
            font-weight: 500;
            padding-left: 16px;
            caret-color: var(--summary-ink);
        }

        .st-key-config_form_panel [data-testid="stTextInput"] [data-testid="InputInstructions"] {
            display: none;
        }

        .config-suffix {
            height: 56px;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-top: 31px;
            border: 1px solid #d7deea;
            border-left: 0;
            border-radius: 0 8px 8px 0;
            color: #233f78;
            font-size: 16px;
            background: #ffffff;
        }

        .st-key-config_form_panel [data-testid="stCheckbox"] {
            margin: 18px 0 0;
        }

        .st-key-config_form_panel [data-testid="stCheckbox"] label {
            display: flex;
            align-items: center;
            gap: 14px;
        }

        .st-key-config_form_panel [data-testid="stCheckbox"] label > div:first-child {
            position: relative;
            width: 52px;
            min-width: 52px;
            height: 31px;
            border-radius: 999px;
            border: 0;
            background: #cfd8e6;
            box-shadow: none;
            transition: background 0.18s ease;
        }

        .st-key-config_form_panel [data-testid="stCheckbox"] label:has(input[aria-checked="true"]) > div:first-child {
            background: #0b63f6;
        }

        .st-key-config_form_panel [data-testid="stCheckbox"] label > div:first-child > div {
            display: none;
        }

        .st-key-config_form_panel [data-testid="stCheckbox"] label > div:first-child:after {
            content: "";
            position: absolute;
            top: 4px;
            left: 4px;
            width: 23px;
            height: 23px;
            border-radius: 50%;
            background: #ffffff;
            box-shadow: 0 2px 7px rgba(12, 32, 68, 0.18);
            transition: transform 0.18s ease;
        }

        .st-key-config_form_panel [data-testid="stCheckbox"] label:has(input[aria-checked="true"]) > div:first-child:after {
            transform: translateX(21px);
        }

        .st-key-config_form_panel .stButton > button,
        .st-key-config_form_panel [data-testid="stFormSubmitButton"] > button {
            height: 63px;
            width: 100%;
            border-radius: 8px;
            font-size: 20px;
            font-weight: 700;
        }

        .st-key-config_form_panel .stButton > button[kind="primary"],
        .st-key-config_form_panel [data-testid="stFormSubmitButton"] > button[kind="primaryFormSubmit"] {
            border: 0;
            color: #ffffff;
            background: linear-gradient(135deg, #0b63f6, #0051ee);
            box-shadow: 0 12px 24px rgba(11, 99, 246, 0.18);
        }

        .st-key-config_form_panel .stButton > button[kind="secondary"],
        .st-key-config_form_panel [data-testid="stFormSubmitButton"] > button[kind="secondaryFormSubmit"] {
            border: 1px solid #cfd8e6;
            color: var(--summary-ink);
            background: #ffffff;
            box-shadow: none;
        }

        .st-key-config_form_panel .stButton > button {
            position: relative;
        }

        .st-key-config_form_panel .stButton > button:before {
            content: "";
            width: 23px;
            height: 23px;
            margin-right: 12px;
            display: inline-block;
            vertical-align: -4px;
            background: currentColor;
            mask-position: center;
            mask-repeat: no-repeat;
            mask-size: contain;
            -webkit-mask-position: center;
            -webkit-mask-repeat: no-repeat;
            -webkit-mask-size: contain;
        }

        .st-key-config_save button:before {
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M5 3h12l2 2v16H5V3Zm2 2v5h9V5H7Zm2 11v3h6v-3H9Zm7-11v4h1V6l-1-1Z'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M5 3h12l2 2v16H5V3Zm2 2v5h9V5H7Zm2 11v3h6v-3H9Zm7-11v4h1V6l-1-1Z'/%3E%3C/svg%3E");
        }

        .st-key-config_restore button:before {
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M12 5a7 7 0 1 1-6.3 4H3l4-4 4 4H8a5 5 0 1 0 4-2V5Z'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M12 5a7 7 0 1 1-6.3 4H3l4-4 4 4H8a5 5 0 1 0 4-2V5Z'/%3E%3C/svg%3E");
        }

        .config-summary-row {
            display: grid;
            grid-template-columns: 42px 1fr;
            align-items: center;
            gap: 16px;
            min-height: 58px;
            border-bottom: 1px solid var(--summary-line);
            color: var(--summary-ink);
            font-size: 17px;
            font-weight: 500;
        }

        .config-summary-row:last-child {
            border-bottom: 0;
        }

        .config-preview-box {
            border: 1px solid #dbe7f6;
            border-radius: 8px;
            background: #fbfdff;
            overflow: hidden;
        }

        .config-preview-title {
            padding: 13px 16px;
            border-bottom: 1px solid #dbe7f6;
            color: var(--summary-ink);
            font-size: 14px;
            font-weight: 800;
            background: #f6f9fe;
        }

        .config-preview-row {
            display: grid;
            grid-template-columns: minmax(0, 1fr) minmax(128px, auto);
            align-items: center;
            gap: 18px;
            padding: 14px 16px;
            border-bottom: 1px solid #e8eef7;
        }

        .config-preview-row:last-child {
            border-bottom: 0;
        }

        .config-preview-label {
            color: #314878;
            font-size: 14px;
            font-weight: 700;
        }

        .config-preview-value {
            color: var(--summary-ink);
            font-size: 17px;
            font-weight: 800;
            text-align: right;
            white-space: nowrap;
        }

        .config-preview-sub {
            color: #5b6f9c;
            font-size: 12px;
            font-weight: 600;
            margin-top: 4px;
            text-align: right;
        }

        .calc-effect-card {
            display: grid;
            grid-template-columns: 62px 1fr;
            align-items: center;
            gap: 20px;
            min-height: 82px;
            padding: 14px 18px;
            margin-top: 12px;
            border: 1px solid var(--summary-line);
            border-radius: 9px;
            background: #ffffff;
            box-shadow: 0 4px 12px rgba(12, 32, 68, 0.05);
        }

        .calc-effect-title {
            color: var(--summary-ink);
            font-size: 16px;
            font-weight: 800;
            margin-bottom: 6px;
        }

        .calc-effect-text {
            color: #2b457e;
            font-size: 15px;
            font-weight: 500;
        }

        .cd-shell {
            color: var(--summary-text);
        }

        .cd-stats-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 24px;
            margin-bottom: 24px;
        }

        .cd-stats-grid .config-stat-card {
            min-height: 110px;
            padding: 18px 28px;
        }

        .cd-stats-grid .config-stat-card .icon-bubble {
            width: 68px;
            height: 68px;
            flex-basis: 68px;
        }

        .cd-stats-grid .config-stat-card .icon-bubble svg {
            width: 34px;
            height: 34px;
        }

        .cd-columns {
            gap: 24px;
        }

        .cd-panel,
        .st-key-cd_form_panel {
            background: rgba(255, 255, 255, 0.94);
            border: 1px solid var(--summary-line);
            border-radius: 9px;
            box-shadow: 0 4px 14px rgba(12, 32, 68, 0.07);
            padding: 28px;
        }

        .st-key-cd_form_panel .stMarkdown .panel-title,
        .cd-panel .panel-title {
            margin-bottom: 28px;
        }

        .st-key-cd_form_panel label p,
        .st-key-cd_form_panel [data-testid="stWidgetLabel"] p {
            color: var(--summary-ink);
            font-size: 15px;
            font-weight: 800;
        }

        .st-key-cd_form_panel [data-testid="stTextInput"],
        .st-key-cd_form_panel [data-testid="stNumberInput"],
        .st-key-cd_form_panel [data-testid="stSelectbox"] {
            margin-bottom: 18px;
        }

        .st-key-cd_form_panel [data-testid="stTextInput"] div[data-baseweb="input"],
        .st-key-cd_form_panel [data-testid="stNumberInput"] div[data-baseweb="input"] {
            min-height: 46px;
            border-radius: 8px;
            background: #ffffff !important;
            background-color: #ffffff !important;
            border: 1px solid #d7deea;
            box-shadow: inset 0 0 0 1px rgba(215, 222, 234, 0.2);
            transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
        }

        .st-key-cd_form_panel [data-testid="stSelectbox"] > div,
        .st-key-cd_form_panel [data-testid="stSelectbox"] div[data-baseweb="select"] {
            min-height: 46px;
            border-radius: 8px !important;
            background: transparent !important;
            border: 0 !important;
            box-shadow: none !important;
        }

        .st-key-cd_form_panel [data-testid="stSelectbox"] div[data-baseweb="select"] > div {
            min-height: 46px;
            border: 1px solid #d7deea !important;
            border-radius: 8px !important;
            background: #ffffff !important;
            background-color: #ffffff !important;
            box-shadow: inset 0 0 0 1px rgba(215, 222, 234, 0.2) !important;
            transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
        }

        .st-key-cd_form_panel [data-testid="stTextInput"] input,
        .st-key-cd_form_panel [data-testid="stNumberInput"] input {
            background: #ffffff !important;
            background-color: #ffffff !important;
            color: var(--summary-text);
            -webkit-text-fill-color: var(--summary-text);
            caret-color: #0b63f6 !important;
            font-size: 16px;
            font-weight: 500;
        }

        .st-key-cd_form_panel [data-testid="stSelectbox"] div[data-baseweb="select"] {
            background: transparent !important;
            background-color: transparent !important;
            color: var(--summary-text);
            -webkit-text-fill-color: var(--summary-text);
            font-size: 16px;
            font-weight: 500;
        }

        .st-key-cd_form_panel [data-testid="stSelectbox"] div[data-baseweb="select"] > div,
        .st-key-cd_form_panel [data-testid="stSelectbox"] div[data-baseweb="select"] span,
        .st-key-cd_form_panel [data-testid="stSelectbox"] div[data-baseweb="select"] input {
            color: var(--summary-text) !important;
            -webkit-text-fill-color: var(--summary-text) !important;
            background: transparent !important;
            background-color: transparent !important;
        }

        .st-key-cd_form_panel [data-testid="stSelectbox"] svg {
            fill: var(--summary-ink) !important;
            color: var(--summary-ink) !important;
        }

        div[data-baseweb="popover"] ul,
        div[data-baseweb="popover"] [role="listbox"] {
            background: #ffffff !important;
            background-color: #ffffff !important;
            color: var(--summary-text) !important;
            border: 1px solid #d7deea !important;
            box-shadow: 0 14px 30px rgba(12, 32, 68, 0.12) !important;
        }

        div[data-baseweb="popover"] li,
        div[data-baseweb="popover"] [role="option"] {
            background: #ffffff !important;
            background-color: #ffffff !important;
            color: var(--summary-text) !important;
        }

        div[data-baseweb="popover"] li * ,
        div[data-baseweb="popover"] [role="option"] * {
            color: var(--summary-text) !important;
            -webkit-text-fill-color: var(--summary-text) !important;
        }

        div[data-baseweb="popover"] li[aria-selected="true"],
        div[data-baseweb="popover"] [role="option"][aria-selected="true"] {
            background: #eef4ff !important;
            background-color: #eef4ff !important;
            color: #0b63f6 !important;
        }

        div[data-baseweb="popover"] li:hover,
        div[data-baseweb="popover"] [role="option"]:hover {
            background: #f4f8ff !important;
            background-color: #f4f8ff !important;
            color: var(--summary-text) !important;
        }

        .st-key-cd_form_panel [data-testid="stTextInput"] div[data-baseweb="input"]:focus-within,
        .st-key-cd_form_panel [data-testid="stNumberInput"] div[data-baseweb="input"]:focus-within {
            border-color: #0b63f6 !important;
            box-shadow:
                0 0 0 3px rgba(11, 99, 246, 0.16),
                inset 0 0 0 1px rgba(11, 99, 246, 0.22);
            transform: translateY(-1px);
        }

        .st-key-cd_form_panel [data-testid="stSelectbox"] div[data-baseweb="select"]:focus-within > div {
            border-color: #0b63f6 !important;
            box-shadow:
                0 0 0 3px rgba(11, 99, 246, 0.16),
                inset 0 0 0 1px rgba(11, 99, 246, 0.22) !important;
            transform: translateY(-1px);
        }

        .st-key-cd_form_panel [data-testid="InputInstructions"] {
            display: none;
        }

        .st-key-cd_form_panel [data-testid="stCaptionContainer"] {
            margin-top: -10px;
            margin-bottom: 14px;
        }

        .st-key-cd_form_panel [data-testid="stCaptionContainer"] p {
            color: #c2410c !important;
            font-size: 12px !important;
            font-weight: 600 !important;
            line-height: 1.45 !important;
        }

        .cd-help-line {
            display: flex;
            align-items: center;
            gap: 10px;
            color: var(--summary-muted);
            font-size: 13px;
            font-weight: 500;
            margin: -2px 0 26px;
        }

        .cd-help-line svg {
            color: #37538b;
            flex: 0 0 auto;
        }

        .cd-subtle-note {
            margin: -10px 0 18px;
            padding: 10px 12px;
            border-radius: 10px;
            border: 1px solid #d8e4f5;
            background: #f7fafe;
            color: #5e7397;
            font-size: 13px;
            line-height: 1.45;
        }

        .cd-section-label {
            color: #314878;
            font-size: 13px;
            font-weight: 800;
            letter-spacing: 0;
            margin: 22px 0 10px;
            text-transform: uppercase;
        }

        .cd-preview-box {
            padding: 15px 16px;
            border: 1px solid #dbe7f6;
            border-radius: 8px;
            background: #fbfdff;
            margin-bottom: 14px;
        }

        .cd-preview-label {
            color: #314878;
            font-size: 13px;
            font-weight: 800;
            margin-bottom: 6px;
        }

        .cd-preview-value {
            color: var(--summary-ink);
            font-size: 15px;
            font-weight: 700;
            line-height: 1.4;
        }

        .cd-coordinate-status {
            display: grid;
            grid-template-columns: 36px minmax(0, 1fr);
            align-items: center;
            gap: 12px;
            min-height: 54px;
            padding: 12px 14px;
            margin: -2px 0 22px;
            border-radius: 8px;
            line-height: 1.25;
        }

        .cd-coordinate-status.neutral {
            border: 1px solid #cfe0fb;
            background: #f5f9ff;
        }

        .cd-coordinate-status.success {
            border: 1px solid #c7eed4;
            background: #f2fbf5;
        }

        .cd-coordinate-status.warning {
            border: 1px solid #ffe0a6;
            background: #fff8e8;
        }

        .cd-coordinate-title {
            color: var(--summary-ink);
            font-size: 14px;
            font-weight: 800;
            margin-bottom: 4px;
        }

        .cd-coordinate-detail {
            color: #5b6f9c;
            font-size: 13px;
            font-weight: 600;
        }

        .st-key-cd_clear_confirm {
            min-height: 34px;
            margin: -4px 0 12px;
            padding: 8px 12px;
            border: 1px solid #e2e8f3;
            border-radius: 8px;
            background: #fbfdff;
        }

        .st-key-cd_clear_confirm p {
            color: #314878 !important;
            font-size: 13px !important;
            font-weight: 700 !important;
        }

        .st-key-cd_save button,
        .st-key-cd_clear button {
            height: 51px;
            border-radius: 8px;
            font-size: 18px;
            font-weight: 800;
        }

        .st-key-cd_save button {
            color: #ffffff;
            background: linear-gradient(135deg, #0b63f6, #0051ee);
            border: 0;
            box-shadow: 0 12px 24px rgba(11, 99, 246, 0.18);
        }

        .st-key-cd_clear button {
            color: var(--summary-ink);
            background: #ffffff;
            border: 1px solid #cfd8e6;
            box-shadow: none;
        }

        .st-key-cd_save button:disabled,
        .st-key-cd_clear button:disabled {
            color: #8797b7 !important;
            background: #f4f7fb !important;
            border: 1px solid #d7deea !important;
            box-shadow: none !important;
            opacity: 1 !important;
        }

        .st-key-cd_save button:before,
        .st-key-cd_clear button:before {
            content: "";
            width: 22px;
            height: 22px;
            margin-right: 12px;
            display: inline-block;
            vertical-align: -4px;
            background: currentColor;
            mask-position: center;
            mask-repeat: no-repeat;
            mask-size: contain;
            -webkit-mask-position: center;
            -webkit-mask-repeat: no-repeat;
            -webkit-mask-size: contain;
        }

        .st-key-cd_save button:before {
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M5 3h12l2 2v16H5V3Zm2 2v5h9V5H7Zm2 11v3h6v-3H9Zm7-11v4h1V6l-1-1Z'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M5 3h12l2 2v16H5V3Zm2 2v5h9V5H7Zm2 11v3h6v-3H9Zm7-11v4h1V6l-1-1Z'/%3E%3C/svg%3E");
        }

        .st-key-cd_clear button:before {
            mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='m7 21 10-10m4-4-4-4L4 16l5 5h12M14 6l4 4'/%3E%3C/svg%3E");
            -webkit-mask-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='m7 21 10-10m4-4-4-4L4 16l5 5h12M14 6l4 4'/%3E%3C/svg%3E");
        }

        .cd-card-box {
            border: 1px solid #cfe0fb;
            border-radius: 9px 9px 0 0;
            overflow: hidden;
            background: #ffffff;
        }

        .cd-card-head {
            display: grid;
            grid-template-columns: 74px 1fr;
            align-items: center;
            gap: 18px;
            padding: 20px 20px 18px;
            background: linear-gradient(180deg, #f7fbff 0%, #ffffff 100%);
            border-bottom: 1px solid #cfe0fb;
        }

        .cd-card-title {
            color: var(--summary-ink);
            font-size: 20px;
            font-weight: 800;
            margin-bottom: 6px;
        }

        .cd-card-sub {
            color: #2e467c;
            font-size: 14px;
            line-height: 1.55;
            font-weight: 500;
        }

        .cd-detail-list {
            padding: 14px 20px 18px;
        }

        .cd-detail-row {
            display: grid;
            grid-template-columns: 28px 1fr auto;
            align-items: center;
            gap: 12px;
            min-height: 36px;
            color: #334b82;
            font-size: 15px;
            font-weight: 500;
        }

        .cd-detail-row svg {
            color: #37538b;
        }

        .cd-detail-value {
            color: #233f78;
            text-align: right;
        }

        .cd-info-box {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-top: 20px;
            padding: 15px 18px;
            border: 1px solid #b9d5ff;
            border-radius: 8px;
            color: #183b7c;
            background: #f5f9ff;
            font-size: 14px;
            font-weight: 600;
        }

        .empty-note {
            color: var(--summary-muted);
            font-size: 15px;
            padding: 12px 0;
        }

        .map-shell {
            color: var(--summary-text);
        }

        .map-stats-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 18px;
            margin-bottom: 18px;
        }

        .map-card {
            background: #ffffff;
            border: 1px solid #dbe7f6;
            border-radius: 9px;
            box-shadow: 0 6px 18px rgba(12, 32, 68, 0.06);
        }

        .map-stat-card {
            min-height: 104px;
            display: grid;
            grid-template-columns: 72px 1fr;
            align-items: center;
            gap: 18px;
            padding: 18px 22px;
        }

        .map-icon-bubble {
            width: 56px;
            height: 56px;
            border-radius: 50%;
            display: grid;
            place-items: center;
        }

        .map-icon-bubble.blue { color: #0b63f6; background: #eaf2ff; }
        .map-icon-bubble.orange { color: #f7a51c; background: #fff2d9; }
        .map-icon-bubble.purple { color: #7b31d8; background: #f0e8ff; }
        .map-icon-bubble.green { color: #10a24a; background: #dcf7e7; }

        .map-stat-label {
            color: #314878;
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 7px;
        }

        .map-stat-value {
            color: var(--summary-ink);
            font-size: 25px;
            line-height: 1;
            font-weight: 800;
        }

        .map-stat-sub {
            color: #5b6f9c;
            font-size: 13px;
            font-weight: 500;
            margin-top: 8px;
        }

        .map-work-grid {
            display: grid;
            grid-template-columns: 0.96fr 1.18fr;
            gap: 14px;
            margin-bottom: 16px;
        }

        .st-key-map_filters_panel,
        .st-key-map_details_panel {
            min-height: 274px;
            padding: 20px;
            background: #ffffff;
            border: 1px solid #dbe7f6;
            border-radius: 9px;
            box-shadow: 0 6px 18px rgba(12, 32, 68, 0.06);
        }

        .st-key-map_details_panel {
            display: flex;
            flex-direction: column;
        }

        .map-panel-title {
            color: var(--summary-ink);
            font-size: 18px;
            font-weight: 800;
            margin: 0 0 16px;
        }

        .map-fixed-origin {
            display: grid;
            grid-template-columns: 42px minmax(0, 1fr);
            align-items: center;
            gap: 12px;
            min-height: 74px;
            padding: 12px 14px;
            margin-bottom: 18px;
            border: 1px solid #dbe7f6;
            border-radius: 8px;
            background: #fbfdff;
        }

        .map-fixed-origin-icon {
            width: 38px;
            height: 38px;
            border-radius: 50%;
            display: grid;
            place-items: center;
            color: #0b63f6;
            background: #eaf2ff;
        }

        .map-fixed-origin-label {
            color: #5b6f9c;
            font-size: 12px;
            font-weight: 700;
            margin-bottom: 4px;
        }

        .map-fixed-origin-name {
            color: var(--summary-ink);
            font-size: 14px;
            font-weight: 800;
            line-height: 1.2;
        }

        .map-fixed-origin-address {
            color: #5b6f9c;
            font-size: 12px;
            line-height: 1.3;
            margin-top: 4px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .map-route-title-row {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            align-items: start;
            justify-content: space-between;
            gap: 18px;
            margin-bottom: 14px;
        }

        .map-eyebrow {
            color: #5b6f9c;
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 5px;
        }

        .map-route-name {
            color: var(--summary-ink);
            font-size: 20px;
            font-weight: 800;
            line-height: 1.2;
        }

        .map-route-address {
            display: flex;
            align-items: flex-start;
            gap: 6px;
            color: #314878;
            font-size: 13px;
            line-height: 1.4;
            margin-top: 5px;
            max-width: 100%;
        }

        .map-status-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            min-height: 28px;
            padding: 0 12px;
            border-radius: 999px;
            color: #08833d;
            background: #dcf7e7;
            font-size: 13px;
            font-weight: 800;
            white-space: nowrap;
        }

        .map-status-pill.pending { color: #c77700; background: #fff2d9; }
        .map-status-pill.error { color: #dc2626; background: #fff1f1; }

        .map-route-empty-note {
            margin: 0 0 12px;
            padding: 10px 12px;
            border: 1px solid #dbe7f6;
            border-radius: 8px;
            color: #314878;
            background: #f7fbff;
            font-size: 13px;
            font-weight: 600;
        }

        .map-metric-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
            align-items: stretch;
            max-width: 650px;
            margin-top: 12px;
        }

        .map-metric-card {
            min-height: 70px;
            display: grid;
            grid-template-columns: 36px minmax(0, 1fr);
            align-items: center;
            gap: 12px;
            padding: 13px 14px;
            border: 1px solid #dbe7f6;
            border-radius: 8px;
            background: #fbfdff;
        }

        .map-metric-card:nth-child(4),
        .map-metric-card:nth-child(5) {
            grid-column: span 1;
        }

        .map-metric-icon {
            width: 30px;
            height: 30px;
            border-radius: 50%;
            display: grid;
            place-items: center;
        }

        .map-metric-icon.blue { color: #0b63f6; background: #eaf2ff; }
        .map-metric-icon.green { color: #10a24a; background: #dcf7e7; }
        .map-metric-icon.purple { color: #7b31d8; background: #f0e8ff; }

        .map-metric-value {
            color: var(--summary-ink);
            font-size: 16px;
            font-weight: 800;
            line-height: 1.1;
            white-space: nowrap;
        }

        .map-metric-label {
            color: #5b6f9c;
            font-size: 12px;
            margin-top: 5px;
            line-height: 1.25;
        }

        .st-key-map_activity_panel {
            margin-top: 16px;
            padding-top: 15px;
            border-top: 1px solid #e2e8f3;
            background: transparent;
        }

        .map-activity-header {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 14px;
            margin-bottom: 10px;
        }

        .map-activity-title {
            color: var(--summary-ink);
            font-size: 15px;
            font-weight: 800;
            line-height: 1.2;
            margin-bottom: 4px;
        }

        .map-activity-note {
            color: #5b6f9c;
            font-size: 13px;
            font-weight: 500;
            line-height: 1.35;
        }

        .map-activity-confirmed {
            display: grid;
            grid-template-columns: 38px minmax(0, 1fr);
            align-items: center;
            gap: 12px;
            padding: 12px 14px;
            border: 1px solid #c7eed4;
            border-radius: 8px;
            background: #f2fbf5;
        }

        .map-activity-confirmed-icon {
            width: 34px;
            height: 34px;
            border-radius: 50%;
            display: grid;
            place-items: center;
            color: #0f8f43;
            background: #dcf7e7;
        }

        .st-key-map_activity_panel .stCheckbox {
            min-height: 44px;
            display: flex;
            align-items: center;
            padding: 0 12px;
            border: 1px solid #dbe7f6;
            border-radius: 8px;
            background: #fbfdff;
        }

        .st-key-map_activity_panel .stCheckbox label {
            color: #18336f !important;
            font-weight: 700;
        }

        .st-key-map_activity_panel .stCheckbox p {
            font-size: 14px;
        }

        .st-key-map_confirm_displacement button {
            min-height: 44px;
            border-radius: 8px;
            background: #0b63f6 !important;
            color: #ffffff !important;
            border-color: #0b63f6 !important;
            box-shadow: 0 9px 18px rgba(11, 99, 246, 0.18);
            font-weight: 800;
        }

        .st-key-map_confirm_displacement button:disabled {
            background: #d9e7fb !important;
            color: #6a7da8 !important;
            border-color: #d9e7fb !important;
            box-shadow: none;
        }

        .st-key-map_filters_panel .stButton button {
            min-height: 42px;
            border-radius: 8px;
            font-weight: 800;
        }

        .st-key-map_view_route button {
            background: #0b63f6 !important;
            color: #ffffff !important;
            border-color: #0b63f6 !important;
            box-shadow: 0 8px 16px rgba(11, 99, 246, 0.16);
        }

        .map-warning-note {
            margin: 0 0 14px;
            padding: 12px 14px;
            border-radius: 8px;
            background: #fff8df;
            color: #8a5d00;
            font-size: 13px;
            font-weight: 600;
        }

        .st-key-map_shell {
            border-radius: 12px;
            overflow: hidden;
            background: #ffffff;
            border: 1px solid #dbe7f6;
            box-shadow: 0 6px 18px rgba(12, 32, 68, 0.06);
            margin-bottom: 16px;
        }

        .st-key-map_shell [data-testid="stElementContainer"] {
            border: 0 !important;
            border-radius: inherit;
        }

        .st-key-map_shell iframe[title="streamlit_folium.st_folium"] {
            height: 610px !important;
        }

        .map-route-points {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 14px;
        }

        .map-point-card {
            min-height: 78px;
            display: grid;
            grid-template-columns: 48px 1fr;
            align-items: center;
            gap: 14px;
            padding: 14px 16px;
        }

        .map-point-label {
            color: #314878;
            font-size: 13px;
            font-weight: 700;
            margin-bottom: 5px;
        }

        .map-point-value {
            color: var(--summary-ink);
            font-size: 15px;
            font-weight: 800;
            line-height: 1.2;
        }

        .map-point-sub {
            color: #5b6f9c;
            font-size: 12px;
            margin-top: 5px;
            line-height: 1.25;
        }

        div[data-testid="stElementContainer"]:has(iframe[title="streamlit_folium.st_folium"]) {
            border-radius: 18px;
            overflow: hidden;
            background: #f7fbff;
            border: 1px solid #dbe7f6;
        }

        div[data-testid="stElementContainer"]:has(iframe[title="streamlit_folium.st_folium"]) > div {
            border-radius: inherit;
            overflow: hidden;
            background: #f7fbff;
        }

        iframe[title="streamlit_folium.st_folium"] {
            display: block;
            width: 100%;
            border: 0;
            border-radius: 18px;
            background: #f7fbff;
        }

        .st-key-cd_real_map_shell {
            border-radius: 18px;
            overflow: hidden;
            background: #f7fbff;
        }

        .st-key-cd_real_map_shell [data-testid="stElementContainer"] {
            border-radius: inherit;
            overflow: hidden;
            background: inherit;
            line-height: 0;
        }

        .st-key-cd_real_map_shell iframe[title="streamlit_folium.st_folium"] {
            border-radius: inherit;
            background: inherit;
        }

        @media (max-width: 1180px) {
            .stats-grid, .dashboard-grid, .secondary-grid, .map-stats-grid, .help-grid {
                grid-template-columns: 1fr 1fr;
            }
            .map-work-grid, .map-route-points {
                grid-template-columns: 1fr;
            }
            .map-metric-grid {
                grid-template-columns: repeat(2, minmax(160px, 1fr));
                max-width: none;
            }
            .actions-grid {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }
            .secondary-grid > .summary-card:nth-child(2) {
                grid-column: span 2;
            }
        }

        @media (max-width: 760px) {
            html, body {
                overflow-x: hidden;
            }

            .stApp {
                background: #fbfdff;
            }

            [data-testid="stSidebar"] {
                display: none !important;
            }

            [data-testid="stHeader"] {
                display: none !important;
            }

            .block-container {
                width: 100% !important;
                max-width: 100% !important;
                padding: 14px 14px 88px !important;
            }

            .mobile-nav {
                position: sticky;
                top: 0;
                z-index: 999;
                display: block;
                margin: -14px -14px 18px;
                padding: 12px 14px 10px;
                background: rgba(255, 255, 255, 0.96);
                border-bottom: 1px solid #dbe3f0;
                box-shadow: 0 8px 22px rgba(12, 32, 68, 0.08);
                backdrop-filter: blur(12px);
            }

            .mobile-nav-head {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 12px;
                margin-bottom: 10px;
            }

            .mobile-nav-brand {
                display: flex;
                align-items: center;
                gap: 10px;
                min-width: 0;
                color: var(--summary-ink);
                font-size: 18px;
                font-weight: 800;
            }

            .mobile-nav-mark {
                width: 36px;
                height: 36px;
                flex: 0 0 36px;
                border-radius: 10px;
                display: grid;
                place-items: center;
                color: #ffffff;
                background: #0b63f6;
            }

            .mobile-nav-current {
                color: #5b6f9c;
                font-size: 12px;
                font-weight: 800;
                text-transform: uppercase;
                white-space: nowrap;
            }

            .mobile-nav-scroll {
                display: flex;
                gap: 8px;
                overflow-x: auto;
                padding-bottom: 2px;
                scrollbar-width: none;
                -webkit-overflow-scrolling: touch;
            }

            .mobile-nav-scroll::-webkit-scrollbar {
                display: none;
            }

            .mobile-nav-link {
                flex: 0 0 auto;
                display: inline-flex;
                align-items: center;
                gap: 8px;
                min-height: 38px;
                padding: 0 12px;
                border: 1px solid #dbe3f0;
                border-radius: 999px;
                color: #18336f !important;
                background: #ffffff;
                text-decoration: none !important;
                font-size: 13px;
                font-weight: 800;
                line-height: 1;
            }

            .mobile-nav-link.active {
                color: #ffffff !important;
                border-color: #0b63f6;
                background: #0b63f6;
                box-shadow: 0 8px 16px rgba(11, 99, 246, 0.18);
            }

            .mobile-nav-link svg {
                width: 17px;
                height: 17px;
            }

            .sidebar-logo {
                margin-bottom: 18px;
            }
            .sidebar-nav {
                gap: 8px;
            }
            .sidebar-help {
                position: static;
                width: auto;
                margin-top: 18px;
            }
            .st-key-nav_ajuda {
                position: static;
                width: auto;
                padding-top: 0;
                border-top: 0;
            }
            .stats-grid, .dashboard-grid, .secondary-grid, .actions-grid, .store-summary, .map-stats-grid, .map-metric-grid, .summary-attention-grid, .help-grid, .help-practices {
                grid-template-columns: 1fr;
            }
            .config-stats-grid {
                grid-template-columns: 1fr;
            }
            .cd-stats-grid {
                grid-template-columns: 1fr;
            }
            .map-stats-grid, .map-metric-grid {
                grid-template-columns: 1fr;
            }

            [data-testid="stHorizontalBlock"] {
                flex-wrap: wrap;
                gap: 0.75rem !important;
            }

            [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
                flex: 1 1 100% !important;
                min-width: 100% !important;
                width: 100% !important;
            }

            .st-key-map_shell iframe[title="streamlit_folium.st_folium"] {
                height: 430px !important;
            }

            .st-key-cd_real_map_shell iframe[title="streamlit_folium.st_folium"] {
                height: 300px !important;
            }

            .secondary-grid > .summary-card:nth-child(2) {
                grid-column: auto;
            }

            .stat-card {
                height: auto;
                min-height: 92px;
                padding: 18px;
                gap: 16px;
            }

            .icon-bubble,
            .map-icon-bubble {
                width: 46px;
                height: 46px;
                flex-basis: 46px;
            }

            .summary-h1,
            .stores-title {
                font-size: 28px;
                line-height: 1.1;
            }

            .summary-title p,
            .stores-subtitle {
                font-size: 14px;
                line-height: 1.35;
            }

            .summary-card,
            .config-panel,
            .cd-panel,
            .st-key-config_form_panel,
            .st-key-cd_form_panel,
            .st-key-map_filters_panel,
            .st-key-map_details_panel,
            .st-key-stores_import_panel,
            .st-key-stores_form_panel,
            .st-key-stores_table_panel,
            .help-card {
                border-radius: 8px;
                padding: 18px !important;
                box-shadow: 0 4px 14px rgba(12, 32, 68, 0.05);
            }

            .config-stat-card,
            .cd-stats-grid .config-stat-card,
            .map-stat-card,
            .stores-stat-card {
                min-height: 88px;
                grid-template-columns: 58px minmax(0, 1fr);
                gap: 14px;
                padding: 16px !important;
            }

            .config-stat-card .icon-bubble,
            .cd-stats-grid .config-stat-card .icon-bubble {
                width: 50px;
                height: 50px;
                flex-basis: 50px;
            }

            .config-stat-value,
            .map-stat-value,
            .stores-stat-value,
            .stat-value {
                font-size: 23px;
                line-height: 1.08;
                overflow-wrap: anywhere;
            }

            .panel,
            .activity-panel {
                padding: 18px !important;
            }

            .info-row, .status-row, .activity-head, .activity-row {
                grid-template-columns: 1fr;
                gap: 8px;
                padding: 12px 0;
            }
            .info-row, .status-row {
                min-height: auto;
            }
            .summary-topbar {
                display: block;
                margin-bottom: 16px;
            }

            .store-summary {
                gap: 16px;
            }

            .donut {
                width: 126px;
                height: 126px;
                margin: 0 auto;
            }

            .donut:before {
                width: 76px;
                height: 76px;
            }

            .config-preview-row,
            .cd-detail-row {
                grid-template-columns: 1fr;
                gap: 4px;
                align-items: start;
            }

            .config-preview-value,
            .config-preview-sub,
            .cd-detail-value {
                text-align: left;
                white-space: normal;
            }

            .map-route-title-row,
            .map-activity-header {
                grid-template-columns: 1fr;
                display: grid;
                gap: 10px;
            }

            .map-fixed-origin-address,
            .map-point-sub,
            .cd-card-sub {
                white-space: normal;
                overflow: visible;
                text-overflow: clip;
            }

            .map-metric-value {
                white-space: normal;
                overflow-wrap: anywhere;
            }

            .cd-card-head,
            .map-point-card {
                grid-template-columns: 54px minmax(0, 1fr);
                gap: 12px;
                padding: 16px;
            }

            .st-key-stores_upload {
                left: 18px;
                right: 18px;
                top: 62px;
            }

            .stores-table-head {
                display: none;
            }

            div[class*="st-key-store_row_"] {
                border: 1px solid #dce4f0;
                border-radius: 8px;
                margin-bottom: 10px;
                padding: 12px;
                background: #ffffff;
            }

            div[class*="st-key-store_row_"] [data-testid="stMarkdownContainer"] p {
                font-size: 14px;
                line-height: 1.35;
            }

            .stores-count-caption,
            .stores-page-status {
                line-height: 1.35;
                text-align: left;
                white-space: normal;
            }

            .st-key-stores_pagination [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
                flex: 1 1 100% !important;
                min-width: 100% !important;
            }

            .st-key-config_form_panel .stButton > button,
            .st-key-config_form_panel [data-testid="stFormSubmitButton"] > button,
            .st-key-cd_save button,
            .st-key-cd_clear button {
                min-height: 48px;
                height: auto;
                font-size: 16px;
                padding: 10px 12px;
                white-space: normal;
            }

            button p {
                white-space: normal;
                overflow-wrap: anywhere;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def nav_href(view: str) -> str:
    return f"?view={view}"


def render_mobile_nav(current_view: str) -> None:
    items = [
        ("resumo", "Resumo", "route"),
        ("configuracoes", "Config", "settings"),
        ("cd", "CD", "home"),
        ("lojas", "Lojas", "store"),
        ("mapa", "Mapa", "map"),
        ("ajuda", "Ajuda", "help"),
    ]
    current_label = next((label for key, label, _ in items if key == current_view), "Resumo")
    links = "".join(
        (
            f'<a class="mobile-nav-link{" active" if key == current_view else ""}" '
            f'href="{escape(nav_href(key))}" target="_self">{app_icon(icon, 17)}<span>{escape(label)}</span></a>'
        )
        for key, label, icon in items
    )
    st.markdown(
        (
            '<nav class="mobile-nav">'
            '<div class="mobile-nav-head">'
            f'<div class="mobile-nav-brand"><span class="mobile-nav-mark">{app_icon("map-pin", 20)}</span><span>Oscar 3</span></div>'
            f'<div class="mobile-nav-current">{escape(current_label)}</div>'
            '</div>'
            f'<div class="mobile-nav-scroll">{links}</div>'
            '</nav>'
        ),
        unsafe_allow_html=True,
    )


def render_sidebar(current_view: str) -> None:
    items = [
        ("resumo", "◷  Resumo"),
        ("configuracoes", "⚙  Configuracoes"),
        ("cd", "⌂  Cadastro do CD"),
        ("lojas", "▣  Cadastro de Lojas"),
        ("mapa", "◇  Mapa"),
    ]
    items = [
        ("resumo", "Resumo"),
        ("configuracoes", "Configurações"),
        ("cd", "Cadastro do CD"),
        ("lojas", "Cadastro de Lojas"),
        ("mapa", "Mapa"),
    ]
    with st.sidebar:
        st.markdown(
            f"""
            <div class="sidebar-logo">
                <div class="sidebar-logo-mark">{app_icon("map-pin", 30)}</div>
                <span>Oscar KM</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        for key, label in items:
            button_type = "primary" if current_view == key else "secondary"
            if st.button(label, key=f"nav_{key}", type=button_type, use_container_width=True):
                st.query_params["view"] = key
                st.rerun()

        help_button_type = "primary" if current_view == "ajuda" else "secondary"
        if st.button("Ajuda", key="nav_ajuda", type=help_button_type, use_container_width=True):
            st.query_params["view"] = "ajuda"
            st.rerun()


def current_view_from_query() -> str:
    view = st.query_params.get("view", "resumo")
    if isinstance(view, list):
        view = view[0] if view else "resumo"
    return view if view in {"resumo", "configuracoes", "cd", "lojas", "mapa", "ajuda"} else "resumo"


def render_help_dashboard() -> None:
    steps_html = "".join(
        [
            '<div class="help-step"><div class="help-step-number">1</div><div class="help-step-text">Cadastre o Centro de Distribuição.</div></div>',
            '<div class="help-step"><div class="help-step-number">2</div><div class="help-step-text">Cadastre lojas manualmente ou importe uma planilha.</div></div>',
            '<div class="help-step"><div class="help-step-number">3</div><div class="help-step-text">Verifique lojas pendentes de coordenada.</div></div>',
            '<div class="help-step"><div class="help-step-number">4</div><div class="help-step-text">Acesse o mapa para visualizar as rotas.</div></div>',
            '<div class="help-step"><div class="help-step-number">5</div><div class="help-step-text">Consulte o resumo para acompanhar distância, litros e custo.</div></div>',
        ]
    )
    sheet_columns = "".join(
        f'<span class="help-chip">{escape(column)}</span>'
        for column in ["Filial", "CEP", "Endereço", "Número", "Bairro", "Cidade", "UF"]
    )
    problems_html = "".join(
        [
            (
                '<div class="help-problem">'
                f'<div class="help-problem-icon purple">{app_icon("card", 20)}</div>'
                '<div><div class="help-problem-title">CEP inválido</div>'
                '<div class="help-problem-copy">Use o formato 00000-000.</div></div></div>'
            ),
            (
                '<div class="help-problem">'
                f'<div class="help-problem-icon orange">{app_icon("alert", 20)}</div>'
                '<div><div class="help-problem-title">Loja pendente</div>'
                '<div class="help-problem-copy">A loja está sem latitude/longitude válida.</div></div></div>'
            ),
            (
                '<div class="help-problem">'
                f'<div class="help-problem-icon blue">{app_icon("map-pin", 20)}</div>'
                '<div><div class="help-problem-title">Mapa em local errado</div>'
                '<div class="help-problem-copy">Confira endereço, cidade, UF e coordenadas.</div></div></div>'
            ),
            (
                '<div class="help-problem">'
                f'<div class="help-problem-icon purple">{app_icon("refresh", 20)}</div>'
                '<div><div class="help-problem-title">Rota não calculada</div>'
                '<div class="help-problem-copy">Verifique se CD e loja possuem coordenadas válidas.</div></div></div>'
            ),
        ]
    )
    tips_html = "".join(
        [
            f'<div class="help-tip"><span class="help-check-icon">{app_icon("check", 18)}</span><span class="help-tip-text">Use endereços completos para melhorar a precisão.</span></div>',
            f'<div class="help-tip"><span class="help-check-icon">{app_icon("check", 18)}</span><span class="help-tip-text">Revise lojas pendentes antes de analisar o mapa.</span></div>',
            f'<div class="help-tip"><span class="help-check-icon">{app_icon("check", 18)}</span><span class="help-tip-text">Evite duplicar lojas com o mesmo nome e endereço.</span></div>',
        ]
    )
    practices_html = "".join(
        [
            f'<div class="help-practice"><span class="help-check-icon">{app_icon("check", 18)}</span><span class="help-practice-text">Revise o cadastro do CD antes de calcular rotas.</span></div>',
            f'<div class="help-practice"><span class="help-check-icon">{app_icon("check", 18)}</span><span class="help-practice-text">Mantenha lojas com endereço completo e UF correta.</span></div>',
            f'<div class="help-practice"><span class="help-check-icon">{app_icon("check", 18)}</span><span class="help-practice-text">Atualize as rotas após mudanças importantes nos dados.</span></div>',
        ]
    )
    help_html = dedent(
        f"""
        <main class="help-shell">
            <section class="summary-topbar">
                <div class="summary-title">
                    <div class="summary-h1">Ajuda</div>
                    <p>Guia rápido para usar o sistema de rotas entre CD e lojas.</p>
                </div>
            </section>
            <section class="help-grid">
                <div class="help-column">
                    <div class="help-card">
                        <div class="help-card-title">Como usar o sistema</div>
                        <div class="help-steps">{steps_html}</div>
                    </div>
                    <div class="help-card">
                        <div class="help-card-title">Formato da planilha</div>
                        <div class="help-sheet-copy">Sua planilha deve conter as seguintes colunas:</div>
                        <div class="help-chips">{sheet_columns}</div>
                        <div class="help-info-line">{app_icon("info", 20)}<span>Importe sempre em formato .xlsx, .xls ou .csv.</span></div>
                    </div>
                </div>
                <div class="help-column">
                    <div class="help-card">
                        <div class="help-card-title">Problemas comuns</div>
                        <div class="help-problem-list">{problems_html}</div>
                    </div>
                    <div class="help-card">
                        <div class="help-card-title">Dicas</div>
                        <div class="help-compact-list">{tips_html}</div>
                    </div>
                    <div class="help-card">
                        <div class="help-card-title">Boas práticas</div>
                        <div class="help-practices">
                            <div class="help-shield">{app_icon("check", 24)}</div>
                            <div class="help-compact-list">{practices_html}</div>
                        </div>
                    </div>
                </div>
            </section>
        </main>
        """
    ).strip()
    help_html = re.sub(r">\s+<", "><", help_html)
    help_html = re.sub(r"\s*\n\s*", " ", help_html)
    st.markdown(help_html, unsafe_allow_html=True)


def icon_bubble(icon: str, color: str, size: int = 28) -> str:
    return f'<div class="icon-bubble {color}">{app_icon(icon, size)}</div>'


def mini_icon(icon: str, color: str) -> str:
    return f'<div class="mini-icon icon-bubble {color}">{app_icon(icon, 22)}</div>'


def stat_card(icon: str, color: str, label: str, value: str) -> str:
    return (
        '<div class="summary-card stat-card">'
        f'{icon_bubble(icon, color, 30)}'
        '<div>'
        f'<div class="stat-label">{escape(label)}</div>'
        f'<div class="stat-value">{escape(value)}</div>'
        '</div></div>'
    )


def config_stat_card(icon: str, color: str, label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="config-stat-sub">{escape(sub)}</div>' if sub else ""
    return (
        '<div class="summary-card config-stat-card">'
        f'{icon_bubble(icon, color, 40)}'
        '<div>'
        f'<div class="stat-label">{escape(label)}</div>'
        f'<div class="config-stat-value">{escape(value)}</div>'
        f'{sub_html}'
        '</div></div>'
    )


def cd_detail_row(icon: str, label: str, value: str) -> str:
    return (
        '<div class="cd-detail-row">'
        f'{app_icon(icon, 19)}'
        f'<div>{escape(label)}</div>'
        f'<div class="cd-detail-value">{escape(value or "-")}</div>'
        '</div>'
    )


def render_cd_real_map(cd: pd.Series | None) -> None:
    if cd is None or not has_valid_coordinates(cd):
        st.info("Salve um CD com coordenadas validas para visualizar o mapa.")
        return

    map_center = [float(cd["latitude"]), float(cd["longitude"])]
    cd_map = folium.Map(location=map_center, zoom_start=16, tiles="OpenStreetMap")
    folium.Marker(
        location=map_center,
        popup=f"<strong>{cd['nome']}</strong><br>{cd['endereco']}",
        tooltip="Centro de Distribuicao",
        icon=folium.Icon(color="blue", icon="home", prefix="fa"),
    ).add_to(cd_map)
    style_folium_embed(cd_map)
    with st.container(key="cd_real_map_shell"):
        st_folium(cd_map, height=288, use_container_width=True, key="cd_real_map_preview")


def info_row(icon: str, color: str, label: str, value: str) -> str:
    return (
        '<div class="info-row">'
        f'{mini_icon(icon, color)}'
        f'<div class="info-name">{escape(label)}</div>'
        f'<div class="info-value">{escape(value)}</div>'
        '</div>'
    )


def config_summary_row(icon: str, color: str, text: str) -> str:
    return (
        '<div class="config-summary-row">'
        f'{mini_icon(icon, color)}'
        f'<div>{escape(text)}</div>'
        '</div>'
    )


def calc_effect_card(icon: str, color: str, title: str, text: str) -> str:
    return (
        '<div class="calc-effect-card">'
        f'{icon_bubble(icon, color, 30)}'
        '<div>'
        f'<div class="calc-effect-title">{escape(title)}</div>'
        f'<div class="calc-effect-text">{escape(text)}</div>'
        '</div></div>'
    )


def status_row(icon: str, color: str, label: str, pill: str, pill_color: str) -> str:
    return (
        '<div class="status-row">'
        f'{mini_icon(icon, color)}'
        f'<div class="status-name">{escape(label)}</div>'
        f'<div class="pill {pill_color}">{escape(pill)}</div>'
        '</div>'
    )


def activity_row(icon: str, color: str, activity: str, detail: str, date_text: str) -> str:
    return (
        '<div class="activity-row">'
        f'<div class="activity-title">{mini_icon(icon, color)}<span class="activity-name">{escape(activity)}</span></div>'
        f'<div class="activity-detail">{escape(detail)}</div>'
        f'<div class="activity-date">{escape(date_text)}</div>'
        '</div>'
    )


def attention_item(icon: str, color: str, title: str, detail: str, href: str) -> str:
    return (
        f'<a class="summary-attention-item" href="{escape(href)}" target="_self">'
        f'{mini_icon(icon, color)}'
        '<div>'
        f'<div class="summary-attention-title">{escape(title)}</div>'
        f'<div class="summary-attention-detail">{escape(detail)}</div>'
        '</div>'
        f'<span class="summary-attention-go">{app_icon("chevron", 16)}</span>'
        '</a>'
    )


def extract_city_uf(address: str) -> str:
    matches = re.findall(r"([^,]+?)\s*-\s*([A-Z]{2})(?=,|$)", address)
    if matches:
        city, state = matches[-1]
        return f"{city.strip()} / {state.strip()}"
    return "-"


def latest_route_label(routes: pd.DataFrame) -> str:
    if routes.empty or "updated_at" not in routes.columns:
        return "-"

    dates = pd.to_datetime(routes["updated_at"], errors="coerce").dropna()
    if dates.empty:
        return "-"

    latest = dates.max().to_pydatetime()
    if latest.date() == datetime.now().date():
        return f"Hoje, {latest:%H:%M}"
    return latest.strftime("%d/%m/%Y, %H:%M")


def format_update_label(value: object) -> str:
    updated_at = pd.to_datetime(value, errors="coerce")
    if pd.isna(updated_at):
        return "Sem atualização"

    timestamp = updated_at.to_pydatetime()
    if timestamp.date() == datetime.now().date():
        return f"Hoje, {timestamp:%H:%M}"
    return timestamp.strftime("%d/%m/%Y, %H:%M")


def sync_config_widget_drafts() -> None:
    for widget_key, draft_key in CONFIG_WIDGET_DRAFT_KEYS.items():
        if widget_key in st.session_state:
            st.session_state[draft_key] = st.session_state[widget_key]


def hydrate_config_widget_state(
    km_l: float,
    fuel_value: float,
    round_trip: bool,
    config_signature: tuple[float, float, bool],
    force_defaults: bool,
) -> None:
    default_values = {
        "config_km_l": format_decimal_input(km_l, 1),
        "config_fuel_value": format_decimal_input(fuel_value, 2, "R$ "),
        "config_round_trip": round_trip,
    }
    draft_missing = any(draft_key not in st.session_state for draft_key in CONFIG_WIDGET_DRAFT_KEYS.values())
    saved_config_changed = st.session_state.get("config_saved_signature") != config_signature

    if force_defaults or saved_config_changed or draft_missing:
        for widget_key, draft_key in CONFIG_WIDGET_DRAFT_KEYS.items():
            st.session_state[draft_key] = default_values[widget_key]
            st.session_state[widget_key] = default_values[widget_key]
        st.session_state["config_saved_signature"] = config_signature
        return

    for widget_key, draft_key in CONFIG_WIDGET_DRAFT_KEYS.items():
        if widget_key not in st.session_state:
            st.session_state[widget_key] = st.session_state[draft_key]


def parse_config_draft() -> tuple[float | None, float | None, bool, str]:
    try:
        km_l = parse_decimal_input(st.session_state.get("config_km_l", ""), "KM/L do veiculo", 0.01)
        fuel_value = parse_decimal_input(
            st.session_state.get("config_fuel_value", ""),
            "Valor do combustivel",
            0.01,
        )
    except ValueError as exc:
        return None, None, bool(st.session_state.get("config_round_trip")), str(exc)
    return km_l, fuel_value, bool(st.session_state.get("config_round_trip")), ""


def build_config_notice(kind: str, text: str) -> str:
    icon = "check" if kind == "success" else "alert"
    color = "green" if kind == "success" else "orange"
    return (
        f'<div class="config-inline-notice {kind}">'
        f'{mini_icon(icon, color)}'
        f'<span>{escape(text)}</span>'
        '</div>'
    )


def config_preview_row(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="config-preview-sub">{escape(sub)}</div>' if sub else ""
    return (
        '<div class="config-preview-row">'
        f'<div class="config-preview-label">{escape(label)}</div>'
        '<div>'
        f'<div class="config-preview-value">{escape(value)}</div>'
        f'{sub_html}'
        '</div>'
        '</div>'
    )


def build_config_preview_html(km_l: float | None, fuel_value: float | None, round_trip: bool, error: str) -> str:
    if error or km_l is None or fuel_value is None:
        return build_config_notice("warning", error or "Informe valores validos para visualizar a previa.")

    one_way_distance = 10.0
    considered_distance = one_way_distance * (2 if round_trip else 1)
    liters = considered_distance / km_l
    cost = liters * fuel_value
    mode = "ida e volta" if round_trip else "somente ida"
    rows = "".join(
        [
            config_preview_row("Distancia considerada", format_optional_number(considered_distance, decimals=2, suffix=" km"), f"simulacao de {mode}"),
            config_preview_row("Litros estimados", format_optional_number(liters, decimals=2, suffix=" L"), f"{km_l:g} km/l"),
            config_preview_row("Custo estimado", currency_brl(cost), f"{currency_brl(fuel_value)} por litro"),
        ]
    )
    return (
        '<div class="config-preview-box">'
        '<div class="config-preview-title">Prévia com rota exemplo de 10 km</div>'
        f'{rows}'
        '</div>'
    )


def render_summary_dashboard(
    cd: pd.Series | None,
    stores: pd.DataFrame,
    report: pd.DataFrame,
    routes: pd.DataFrame,
    config: dict[str, Any],
) -> None:
    activity_report = confirmed_activity_report(report)
    total_stores = len(stores)
    pending_coordinates = 0 if stores.empty else int((~stores.apply(has_valid_coordinates, axis=1)).sum())
    active_stores = max(total_stores - pending_coordinates, 0)
    cd_name = "-" if cd is None else str(cd.get("nome", "-"))
    cd_city = "-" if cd is None else extract_city_uf(str(cd.get("endereco", "")))
    distance_mode = "Ida e volta" if config["ida_e_volta"] else "Somente ida"
    active_angle = 0 if total_stores == 0 else round(active_stores / total_stores * 360, 2)
    calculated_routes = int(report["Status rota"].eq("Calculada").sum()) if "Status rota" in report else 0
    pending_routes = int(report["Status rota"].eq("Pendente").sum()) if "Status rota" in report else 0
    error_routes = int(report["Status rota"].eq("Erro").sum()) if "Status rota" in report else 0
    confirmed_activities = int(activity_report["Status rota"].eq("Calculada").sum()) if "Status rota" in activity_report else 0
    latest_route = latest_route_label(routes)
    last_route_status = latest_route if latest_route != "-" else "Sem rotas"
    route_pill = "Ativo" if calculated_routes else "Pronto para calcular"
    route_pill_color = "green" if calculated_routes else "blue"
    total_cost = float(pd.to_numeric(activity_report.get("Custo estimado", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not activity_report.empty else 0.0
    total_distance = float(pd.to_numeric(activity_report.get("Distancia considerada em km", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not activity_report.empty else 0.0
    total_time = float(pd.to_numeric(activity_report.get("Tempo estimado min", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not activity_report.empty else 0.0
    cd_updated_at = "Pendente" if cd is None else format_update_label(cd.get("updated_at", ""))
    pending_store_name = "-"
    if not stores.empty and pending_coordinates:
        pending_store = stores.loc[~stores.apply(has_valid_coordinates, axis=1)].iloc[0]
        pending_store_name = str(pending_store.get("nome", "-"))

    stats_html = "".join(
        [
            stat_card("home", "blue", "CD cadastrado", "1" if cd is not None else "0"),
            stat_card("home", "blue", "Lojas cadastradas", str(total_stores)),
            stat_card("route", "purple", "Rotas calculadas", str(calculated_routes)),
            stat_card("money", "green", "Custo final", currency_brl(total_cost)),
        ]
    )

    operational_html = "".join(
        [
            info_row("home", "blue", "Nome do CD", cd_name),
            info_row("map-pin", "blue", "Cidade/UF", cd_city),
            info_row("refresh", "purple", "Distancia considerada", distance_mode),
            info_row("clock", "purple", "Ultima rota calculada", last_route_status),
            info_row("check", "green", "Atividades realizadas", str(confirmed_activities)),
            info_row("clock", "orange", "CD atualizado", cd_updated_at),
        ]
    )

    cost_time_html = "".join(
        [
            info_row("money", "green", "Custo final", currency_brl(total_cost)),
            info_row("route", "purple", "Distancia percorrida", format_optional_number(total_distance, decimals=2, suffix=" km")),
            info_row("clock", "blue", "Tempo real", format_duration_minutes(total_time) if total_time else "-"),
            info_row("fuel", "green", "Consumo do veiculo", f"{float(config['km_por_litro']):g} km/l"),
            info_row("money", "orange", "Combustivel", f"{currency_brl(float(config['valor_combustivel']))} / litro"),
        ]
    )

    status_html = "".join(
        [
            status_row("check", "green", "CD configurado", "Ativo" if cd is not None else "Pendente", "green" if cd is not None else "orange"),
            status_row("upload", "blue", "Importacao de lojas", "Concluida" if total_stores else "Pendente", "green" if total_stores else "orange"),
            status_row("alert", "orange", "Geocodificacao", f"{pending_coordinates} pendentes", "orange" if pending_coordinates else "green"),
            status_row("route", "purple", "Rotas calculadas", f"{calculated_routes} de {total_stores}", route_pill_color),
            status_row("alert", "orange", "Rotas com erro", str(error_routes), "orange" if error_routes else "green"),
        ]
    )

    attention_items: list[str] = []
    if cd is None:
        attention_items.append(attention_item("home", "orange", "Cadastrar CD", "Necessario para calcular rotas.", nav_href("cd")))
    if pending_coordinates:
        attention_items.append(attention_item("alert", "orange", "Corrigir coordenadas", f"{pending_coordinates} loja(s) pendente(s). Ex.: {pending_store_name}", nav_href("lojas")))
    if error_routes:
        attention_items.append(attention_item("alert", "orange", "Revisar rotas com erro", f"{error_routes} rota(s) precisam de atencao.", nav_href("mapa")))
    if pending_routes:
        attention_items.append(attention_item("route", "purple", "Calcular rotas pendentes", f"{pending_routes} rota(s) ainda nao calculadas.", nav_href("mapa")))
    if not attention_items:
        attention_items.append(attention_item("check", "green", "Tudo em ordem", "CD, lojas e rotas estao prontos para consulta.", nav_href("mapa")))
    attention_html = "".join(attention_items[:4])

    summary_html = dedent(
        f"""
        <main class="summary-shell">
            <section class="summary-topbar">
                <div class="summary-title">
                    <div class="summary-h1">Resumo</div>
                    <p>Visao geral do sistema de rotas entre CD e lojas</p>
                </div>
            </section>

            <section class="stats-grid">{stats_html}</section>

            <section class="dashboard-grid">
                <div class="summary-card panel">
                    <div class="panel-title">Visao geral operacional</div>
                    {operational_html}
                </div>
                <div class="summary-card panel">
                    <div class="panel-title">Custos e tempo</div>
                    {cost_time_html}
                </div>
            </section>

            <section class="secondary-grid">
                <div class="summary-card panel">
                    <div class="panel-title">Resumo das lojas</div>
                    <div class="store-summary">
                        <div class="donut" style="background: conic-gradient(#0b63f6 0 {active_angle}deg, #f7a51c {active_angle}deg 360deg);">
                            <div class="donut-center">
                                <div class="donut-total">{total_stores}</div>
                                <div class="donut-caption">Total</div>
                            </div>
                        </div>
                        <div>
                            <div class="legend-row">
                                <span class="legend-dot blue"></span>
                                <span class="legend-name">Ativas</span>
                                <span class="legend-value">{active_stores}</span>
                            </div>
                            <div class="legend-row">
                                <span class="legend-dot orange"></span>
                                <span class="legend-name">Pendentes</span>
                                <span class="legend-value">{pending_coordinates}</span>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="summary-card panel">
                    <div class="panel-title">Status do sistema</div>
                    {status_html}
                </div>
            </section>

            <section class="dashboard-grid">
                <div class="summary-card panel">
                    <div class="panel-title">Acoes rapidas</div>
                    <div class="actions-grid">
                        <a class="summary-card action-card" href="{nav_href("configuracoes")}" target="_self">{icon_bubble("money", "green", 30)}<span class="action-label">Configurar combustivel</span></a>
                        <a class="summary-card action-card" href="{nav_href("cd")}" target="_self">{icon_bubble("home", "blue", 30)}<span class="action-label">Cadastrar CD</span></a>
                        <a class="summary-card action-card" href="{nav_href("lojas")}" target="_self">{icon_bubble("upload", "purple", 30)}<span class="action-label">Importar lojas</span></a>
                        <a class="summary-card action-card" href="{nav_href("mapa")}" target="_self">{icon_bubble("map", "blue", 30)}<span class="action-label">Abrir mapa</span></a>
                    </div>
                </div>
                <div class="summary-card panel">
                    <div class="panel-title">Prioridades</div>
                    <div class="summary-attention-grid">
                        {attention_html}
                    </div>
                </div>
            </section>
        </main>
        """
    ).strip()
    summary_html = re.sub(r">\s+<", "><", summary_html)
    summary_html = re.sub(r"\s*\n\s*", " ", summary_html)

    st.markdown(
        summary_html,
        unsafe_allow_html=True,
    )


def render_config_dashboard(config: dict[str, Any], routes: pd.DataFrame) -> None:
    km_l = float(config["km_por_litro"])
    fuel_value = float(config["valor_combustivel"])
    round_trip = bool(config["ida_e_volta"])
    config_signature = (km_l, fuel_value, round_trip)
    force_defaults = bool(st.session_state.pop("config_force_defaults", False))
    hydrate_config_widget_state(km_l, fuel_value, round_trip, config_signature, force_defaults)
    draft_km_l, draft_fuel_value, draft_round_trip, draft_error = parse_config_draft()
    has_unsaved_changes = (
        not draft_error
        and draft_km_l is not None
        and draft_fuel_value is not None
        and (
            round(float(draft_km_l), 6) != round(km_l, 6)
            or round(float(draft_fuel_value), 6) != round(fuel_value, 6)
            or bool(draft_round_trip) != round_trip
        )
    )
    calculated_routes_count = int(routes["status"].eq("ok").sum()) if not routes.empty and "status" in routes else 0
    config_notice = st.session_state.pop("config_notice", "")
    config_notice_kind = st.session_state.pop("config_notice_kind", "success")
    if st.session_state.pop("config_restore_confirm_reset", False):
        st.session_state["config_restore_confirm"] = False

    distance_mode = "Ida e volta" if round_trip else "Somente ida"
    updated_at = latest_route_label(routes)
    if updated_at == "-":
        updated_at = "Sem rotas salvas"

    stats_html = "".join(
        [
            config_stat_card("fuel", "blue", "Consumo do veiculo", f"{km_l:g} km/l"),
            config_stat_card("money", "green", "Combustivel", currency_brl(fuel_value), "valor por litro"),
            config_stat_card("refresh", "purple", "Distancia considerada", distance_mode),
        ]
    )
    summary_html = "".join(
        [
            config_summary_row("fuel", "blue", f"Consumo: {km_l:g} km/l"),
            config_summary_row("money", "green", f"Combustivel: {currency_brl(fuel_value)} por litro"),
            config_summary_row("refresh", "purple", f"Distancia: {distance_mode}"),
            config_summary_row("clock", "orange", f"Atualizado: {updated_at}"),
        ]
    )
    effects_html = "".join(
        [
            calc_effect_card("route", "blue", "Rota real", "a geometria salva da rota nao e alterada por estes parametros"),
            calc_effect_card("fuel", "green", "Litros estimados", "distancia considerada dividida pelo consumo do veiculo"),
            calc_effect_card("money", "orange", "Custo estimado", "litros estimados multiplicados pelo valor do combustivel"),
        ]
    )
    preview_html = build_config_preview_html(draft_km_l, draft_fuel_value, draft_round_trip, draft_error)

    header_html = dedent(
        f"""
        <main class="config-shell">
            <section class="summary-topbar">
                <div class="summary-title">
                    <div class="summary-h1">Configurações</div>
                    <p>Defina os parâmetros utilizados no cálculo das rotas e custos</p>
                </div>
            </section>
            <section class="config-stats-grid">{stats_html}</section>
        </main>
        """
    ).strip()
    header_html = re.sub(r">\s+<", "><", header_html)
    header_html = re.sub(r"\s*\n\s*", " ", header_html)
    st.markdown(header_html, unsafe_allow_html=True)

    left_col, right_col = st.columns([1.02, 1], gap="large")

    with left_col:
        with st.container(key="config_form_panel"):
            st.markdown('<div class="panel-title">Parâmetros de cálculo</div>', unsafe_allow_html=True)
            if config_notice:
                st.markdown(build_config_notice(config_notice_kind, config_notice), unsafe_allow_html=True)
            if draft_error:
                st.markdown(build_config_notice("warning", draft_error), unsafe_allow_html=True)
            elif has_unsaved_changes:
                st.markdown(build_config_notice("warning", "Existem alterações não salvas."), unsafe_allow_html=True)
            if calculated_routes_count:
                st.markdown(
                    build_config_notice(
                        "warning",
                        "Alterar estes parâmetros atualiza custos e totais exibidos, sem recalcular a rota real já salva.",
                    ),
                    unsafe_allow_html=True,
                )
            st.markdown('<div class="config-section-label">Valores principais</div>', unsafe_allow_html=True)

            km_input_col, km_suffix_col = st.columns([5, 1])
            with km_input_col:
                st.text_input(
                    "KM/L do veículo",
                    key="config_km_l",
                    on_change=sync_config_widget_drafts,
                )
            with km_suffix_col:
                st.markdown('<div class="config-suffix">km/l</div>', unsafe_allow_html=True)

            fuel_input_col, fuel_suffix_col = st.columns([5, 1])
            with fuel_input_col:
                st.text_input(
                    "Valor do combustível",
                    key="config_fuel_value",
                    on_change=sync_config_widget_drafts,
                )
            with fuel_suffix_col:
                st.markdown('<div class="config-suffix">por litro</div>', unsafe_allow_html=True)

            st.markdown('<div class="config-section-label">Modo de distância</div>', unsafe_allow_html=True)
            st.toggle("Considerar ida e volta", key="config_round_trip", on_change=sync_config_widget_drafts)

            st.markdown('<div class="config-section-label">Ações</div>', unsafe_allow_html=True)

            save_clicked = st.button(
                "Salvar configurações",
                type="primary",
                use_container_width=True,
                key="config_save",
                disabled=bool(draft_error) or not has_unsaved_changes,
            )
            restore_confirmed = st.checkbox(
                "Confirmar restauração para o padrão",
                key="config_restore_confirm",
            )
            restore_clicked = st.button(
                "Restaurar padrão",
                use_container_width=True,
                key="config_restore",
                disabled=not restore_confirmed,
            )

        if save_clicked:
            try:
                new_km_l = parse_decimal_input(st.session_state["config_km_l"], "KM/L do veiculo", 0.01)
                new_fuel_value = parse_decimal_input(
                    st.session_state["config_fuel_value"],
                    "Valor do combustivel",
                    0.01,
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                save_config(new_km_l, new_fuel_value, bool(st.session_state["config_round_trip"]))
                sync_config_widget_drafts()
                st.session_state["config_notice"] = "Configurações salvas com sucesso."
                st.session_state["config_notice_kind"] = "success"
                st.rerun()

        if restore_clicked:
            save_config(
                DEFAULT_CONFIG["km_por_litro"],
                DEFAULT_CONFIG["valor_combustivel"],
                DEFAULT_CONFIG["ida_e_volta"],
            )
            st.session_state["config_force_defaults"] = True
            st.session_state["config_restore_confirm_reset"] = True
            st.session_state["config_notice"] = "Configurações restauradas para o padrão."
            st.session_state["config_notice_kind"] = "success"
            st.rerun()

    with right_col:
        side_html = dedent(
            f"""
            <div class="config-panel">
                <div class="panel-title">Resumo das configurações atuais</div>
                {summary_html}
            </div>
            <div style="height: 24px;"></div>
            <div class="config-panel">
                <div class="panel-title">Prévia antes de salvar</div>
                {preview_html}
            </div>
            <div style="height: 24px;"></div>
            <div class="config-panel">
                <div class="panel-title">Como isso afeta o cálculo</div>
                {effects_html}
            </div>
            """
        ).strip()
        side_html = re.sub(r">\s+<", "><", side_html)
        side_html = re.sub(r"\s*\n\s*", " ", side_html)
        st.markdown(side_html, unsafe_allow_html=True)


def render_cd_dashboard(cd: pd.Series | None, routes: pd.DataFrame) -> None:
    current_name = "" if cd is None else str(cd["nome"])
    current_address = "" if cd is None else str(cd["endereco"])
    current_lat = "" if cd is None else format_coordinate_for_input(cd["latitude"])
    current_lon = "" if cd is None else format_coordinate_for_input(cd["longitude"])
    parsed_address = split_saved_address(current_address)
    cd_signature = (current_name, current_address, current_lat, current_lon)
    loaded_location_signature = (
        parsed_address["cep"],
        parsed_address["address"],
        parsed_address["number"],
        parsed_address["neighborhood"],
        parsed_address["city"],
        parsed_address["state"],
    )
    clear_fields = bool(st.session_state.pop("cd_clear_fields", False))

    if clear_fields or st.session_state.get("cd_saved_signature") != cd_signature:
        st.session_state["cd_name"] = "" if clear_fields else current_name
        st.session_state["cd_cep"] = "" if clear_fields else parsed_address["cep"]
        st.session_state["cd_address"] = "" if clear_fields else parsed_address["address"]
        st.session_state["cd_number"] = "" if clear_fields else parsed_address["number"]
        st.session_state["cd_neighborhood"] = "" if clear_fields else parsed_address["neighborhood"]
        st.session_state["cd_city"] = "" if clear_fields else parsed_address["city"]
        st.session_state["cd_state"] = "" if clear_fields else parsed_address["state"]
        st.session_state["cd_lat"] = "" if clear_fields else current_lat
        st.session_state["cd_lon"] = "" if clear_fields else current_lon
        st.session_state["cd_saved_signature"] = cd_signature
        st.session_state["cd_loaded_location_signature"] = ("", "", "", "", "", "") if clear_fields else loaded_location_signature
        st.session_state["cd_loaded_coordinate_signature"] = ("", "") if clear_fields else (current_lat, current_lon)
        if clear_fields:
            st.session_state.pop("cd_form_notice", None)
            st.session_state.pop("cd_cep_notice", None)
            st.session_state.pop("cd_number_notice", None)

    refresh_cd_inline_notices()

    last_update = "Sem atualização" if cd is None else format_update_label(cd.get("updated_at", ""))
    status_label = "Ativo" if cd is not None else "Pendente"
    stats_html = "".join(
        [
            config_stat_card("home", "blue", "CD cadastrado", "1" if cd is not None else "0"),
            config_stat_card("check", "green", "Status", status_label),
            config_stat_card("clock", "purple", "Última atualização", last_update),
        ]
    )
    header_html = dedent(
        f"""
        <main class="cd-shell">
            <section class="summary-topbar">
                <div class="summary-title">
                    <div class="summary-h1">Cadastro do CD</div>
                    <p>Cadastre o centro de distribuição que será usado como origem das rotas</p>
                </div>
            </section>
            <section class="cd-stats-grid">{stats_html}</section>
        </main>
        """
    ).strip()
    header_html = re.sub(r">\s+<", "><", header_html)
    header_html = re.sub(r"\s*\n\s*", " ", header_html)
    st.markdown(header_html, unsafe_allow_html=True)

    left_col, right_col = st.columns([1.05, 1], gap="large")
    states = [
        "",
        "AC",
        "AL",
        "AP",
        "AM",
        "BA",
        "CE",
        "DF",
        "ES",
        "GO",
        "MA",
        "MT",
        "MS",
        "MG",
        "PA",
        "PB",
        "PR",
        "PE",
        "PI",
        "RJ",
        "RN",
        "RS",
        "RO",
        "RR",
        "SC",
        "SP",
        "SE",
        "TO",
    ]
    if st.session_state.get("cd_state") not in states:
        st.session_state["cd_state"] = ""

    if st.session_state.pop("cd_clear_confirm_reset", False):
        st.session_state["cd_clear_confirm"] = False

    cd_values = current_cd_form_values()
    cd_location_signature = build_cd_location_signature(
        cd_values["cep"],
        cd_values["address"],
        cd_values["number"],
        cd_values["neighborhood"],
        cd_values["city"],
        cd_values["state"],
    )
    saved_form_signature = cd_saved_signature_from_values(current_name, parsed_address, current_lat, current_lon)
    current_form_signature = cd_form_signature_from_values(cd_values)
    has_cd_unsaved_changes = current_form_signature != saved_form_signature
    cd_save_blocker = cd_form_blocker(cd_values)
    cd_notice = st.session_state.pop("cd_notice", "")
    cd_notice_kind = st.session_state.pop("cd_notice_kind", "success")
    calculated_routes_count = int(routes["status"].eq("ok").sum()) if not routes.empty and "status" in routes else 0

    with left_col:
        with st.container(key="cd_form_panel"):
            st.markdown('<div class="panel-title">Dados do Centro de Distribuição</div>', unsafe_allow_html=True)
            if cd_notice:
                st.markdown(build_config_notice(cd_notice_kind, cd_notice), unsafe_allow_html=True)
            if has_cd_unsaved_changes:
                st.markdown(build_config_notice("warning", "Existem alterações não salvas no CD."), unsafe_allow_html=True)
            if calculated_routes_count and has_cd_unsaved_changes:
                st.markdown(
                    build_config_notice(
                        "warning",
                        "Alterar o CD muda a origem das rotas. Depois de salvar, recalcule as rotas na aba Mapa.",
                    ),
                    unsafe_allow_html=True,
                )
            if cd_save_blocker:
                st.markdown(build_config_notice("warning", cd_save_blocker), unsafe_allow_html=True)
            st.markdown('<div class="cd-section-label">Identificação</div>', unsafe_allow_html=True)
            st.text_input("Nome do CD", key="cd_name")

            st.markdown('<div class="cd-section-label">Endereço</div>', unsafe_allow_html=True)
            cep_col, address_col = st.columns([1, 2], gap="large")
            with cep_col:
                st.text_input("CEP", key="cd_cep", placeholder="03110-000", max_chars=9, on_change=normalize_cd_cep)
                cd_cep_notice = str(st.session_state.get("cd_cep_notice") or "").strip()
                if cd_cep_notice:
                    st.caption(cd_cep_notice)
            with address_col:
                st.text_input("Endereço", key="cd_address", placeholder="Rua Oscar de Souza Silva", on_change=normalize_cd_address)

            number_col, neighborhood_col = st.columns([1, 2], gap="large")
            with number_col:
                st.text_input("Número", key="cd_number", placeholder="120", on_change=normalize_cd_number)
                cd_number_notice = str(st.session_state.get("cd_number_notice") or "").strip()
                if cd_number_notice:
                    st.caption(cd_number_notice)
            with neighborhood_col:
                st.text_input("Bairro", key="cd_neighborhood", placeholder="Vila Guilherme", on_change=normalize_cd_neighborhood)

            city_col, state_col = st.columns([2, 0.8], gap="large")
            with city_col:
                st.text_input("Cidade", key="cd_city", placeholder="São Paulo", on_change=normalize_cd_city)
            with state_col:
                st.selectbox("UF", states, key="cd_state")

            st.markdown('<div class="cd-section-label">Coordenadas opcionais</div>', unsafe_allow_html=True)
            lat_col, lon_col = st.columns(2, gap="large")
            with lat_col:
                st.text_input("Latitude (opcional)", key="cd_lat", placeholder="-23.509142")
            with lon_col:
                st.text_input("Longitude (opcional)", key="cd_lon", placeholder="-46.633308")

            st.markdown(
                cd_coordinate_status_html(cd_values, cd_location_signature),
                unsafe_allow_html=True,
            )

            cd_form_notice = str(st.session_state.get("cd_form_notice") or "").strip()
            if cd_form_notice:
                st.markdown(f'<div class="cd-subtle-note">{escape(cd_form_notice)}</div>', unsafe_allow_html=True)

            st.markdown('<div class="cd-section-label">Ações</div>', unsafe_allow_html=True)
            clear_confirmed = st.checkbox("Confirmar limpeza dos campos", key="cd_clear_confirm")
            save_col, clear_col = st.columns([1, 1], gap="large")
            with save_col:
                save_clicked = st.button(
                    "Salvar CD",
                    type="primary",
                    use_container_width=True,
                    key="cd_save",
                    disabled=bool(cd_save_blocker) or not has_cd_unsaved_changes,
                )
            with clear_col:
                clear_clicked = st.button(
                    "Limpar",
                    use_container_width=True,
                    key="cd_clear",
                    disabled=not clear_confirmed,
                )

        if save_clicked:
            try:
                refresh_cd_inline_notices()
                raw_cd_cep = clean_text_value(st.session_state.get("cd_cep", ""))
                raw_cd_number = clean_text_value(st.session_state.get("cd_number", ""))
                cd_cep = clean_cep(raw_cd_cep)
                cd_address = normalize_cd_address_value_for_save(st.session_state.get("cd_address", ""))
                cd_number = raw_cd_number
                cd_neighborhood = normalize_cd_neighborhood_value_for_save(st.session_state.get("cd_neighborhood", ""))
                cd_city = normalize_cd_city_value_for_save(st.session_state.get("cd_city", ""))
                cd_location_signature = build_cd_location_signature(
                    cd_cep,
                    cd_address,
                    cd_number,
                    cd_neighborhood,
                    cd_city,
                    st.session_state.get("cd_state", ""),
                )
                cd_lat_value = clean_text_value(st.session_state.get("cd_lat", ""))
                cd_lon_value = clean_text_value(st.session_state.get("cd_lon", ""))
                if should_discard_stale_cd_coordinates(cd_location_signature, cd_lat_value, cd_lon_value):
                    cd_lat_value = ""
                    cd_lon_value = ""
                validate_cd_form_values(raw_cd_cep, raw_cd_number)
                validate_cd_location_inputs(
                    cd_address,
                    cd_number,
                    cd_city,
                    st.session_state.get("cd_state", ""),
                    cd_cep,
                    cd_lat_value,
                    cd_lon_value,
                )
                st.session_state.pop("cd_form_notice", None)
                with st.spinner("Validando CD e buscando coordenadas quando necessário..."):
                    cd_data = resolve_location_data(
                        st.session_state["cd_name"],
                        cd_address,
                        cd_lat_value,
                        cd_lon_value,
                        number=cd_number,
                        neighborhood=cd_neighborhood,
                        city=cd_city,
                        state=st.session_state["cd_state"],
                        cep=cd_cep,
                        entity_label="CD",
                    )

                save_cd(cd_data["nome"], cd_data["endereco"], cd_data["latitude"], cd_data["longitude"])
                warnings = coordinate_warning_messages(cd_data["latitude"], cd_data["longitude"])
                if warnings:
                    st.session_state["coordinate_warning"] = "CD salvo. " + " ".join(warnings)
                st.session_state["cd_notice"] = "Centro de Distribuição salvo com sucesso."
                st.session_state["cd_notice_kind"] = "success"
                st.rerun()
            except ValueError as exc:
                message = str(exc)
                if "CEP deve estar no formato 00000-000." in message:
                    st.session_state["cd_cep_notice"] = "CEP inválido. Use o formato 00000-000."
                    st.session_state["cd_form_notice"] = "Corrija os campos destacados antes de salvar o CD."
                    st.rerun()
                elif "Numero deve conter apenas digitos." in message:
                    st.session_state["cd_number_notice"] = "Número inválido. Use apenas números."
                    st.session_state["cd_form_notice"] = "Corrija os campos destacados antes de salvar o CD."
                    st.rerun()
                elif message in {
                    "Informe o numero do endereco do CD.",
                    "Informe o endereco do CD.",
                    "Informe a cidade do CD.",
                    "Selecione a UF do CD.",
                    "Informe um endereco valido para o CD.",
                    "Informe uma cidade valida para o CD.",
                    "Informe latitude e longitude juntas ou deixe ambas em branco.",
                }:
                    st.session_state["cd_form_notice"] = "Corrija os campos destacados antes de salvar o CD."
                    st.rerun()
                elif is_cd_address_lookup_error(message):
                    st.session_state["cd_form_notice"] = (
                        "Não encontramos esse CEP/endereço com os dados informados. "
                        "Confira CEP, endereço, número, bairro e cidade."
                    )
                    st.rerun()
                else:
                    st.error(message)

        if clear_clicked:
            st.session_state["cd_clear_fields"] = True
            st.session_state["cd_clear_confirm_reset"] = True
            st.rerun()

    with right_col:
        if cd is None:
            cd_name = "Nenhum CD cadastrado"
            address_line = "Preencha os dados ao lado para definir a origem das rotas."
            city_state = "-"
            cep_value = "-"
            lat_value = "-"
            lon_value = "-"
        else:
            card_parts = split_saved_address(current_address)
            cd_name = current_name
            composed_preview = build_full_address(
                card_parts["address"],
                card_parts["number"],
                card_parts["neighborhood"],
                card_parts["city"],
                card_parts["state"],
                card_parts["cep"],
            )
            address_line = composed_preview.replace(", Brasil", "") or current_address
            city_state = (
                f"{card_parts['city']} / {card_parts['state']}"
                if card_parts["city"] or card_parts["state"]
                else extract_city_uf(current_address)
            )
            cep_value = card_parts["cep"] or "-"
            lat_value = format_coordinate_for_input(cd["latitude"])
            lon_value = format_coordinate_for_input(cd["longitude"])

        card_html = dedent(
            f"""
            <div class="cd-panel">
                <div class="panel-title">Prévia antes de salvar</div>
                <div class="cd-preview-box">
                    <div class="cd-preview-label">Endereço completo</div>
                    <div class="cd-preview-value">{escape(cd_form_preview_address(cd_values))}</div>
                </div>
                {cd_coordinate_status_html(cd_values, cd_location_signature)}
            </div>
            <div style="height: 24px;"></div>
            <div class="cd-panel">
                <div class="panel-title">CD cadastrado</div>
                <div class="cd-card-box">
                    <div class="cd-card-head">
                        {icon_bubble("home", "blue", 32)}
                        <div>
                            <div class="cd-card-title">{escape(cd_name)}</div>
                            <div class="cd-card-sub">{escape(address_line)}</div>
                        </div>
                    </div>
                    <div class="cd-detail-list">
                        {cd_detail_row("map-pin", "Cidade / UF", city_state)}
                        {cd_detail_row("card", "CEP", cep_value)}
                        {cd_detail_row("crosshair", "Latitude", lat_value)}
                        {cd_detail_row("globe", "Longitude", lon_value)}
                    </div>
                </div>
                <div class="cd-info-box">
                    {app_icon("info", 24)}
                    <span>Este CD será utilizado como origem para o cálculo das rotas.</span>
                </div>
            </div>
            """
        ).strip()
        card_html = re.sub(r">\s+<", "><", card_html)
        card_html = re.sub(r"\s*\n\s*", " ", card_html)
        st.markdown(card_html, unsafe_allow_html=True)
        render_cd_real_map(cd)

def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="auto")
    render_dashboard_styles()
    ensure_storage()

    config = load_config()
    cd_df = load_cd()
    stores = load_stores()
    routes = load_routes()
    cd = get_current_cd(cd_df)
    report = compute_report(cd, stores, config, routes)
    current_view = current_view_from_query()
    render_sidebar(current_view)
    render_mobile_nav(current_view)

    coordinate_warning = st.session_state.pop("coordinate_warning", None)
    if coordinate_warning:
        st.warning(coordinate_warning)

    if current_view == "resumo":
        render_summary_dashboard(cd, stores, report, routes, config)
        return

    if current_view == "configuracoes":
        render_config_dashboard(config, routes)
        return

    if current_view == "cd":
        render_cd_dashboard(cd, routes)
        return

    if current_view == "lojas":
        stores_editor(stores)
        return

    if current_view == "mapa":
        render_map_page(cd, stores, report, config)
        return

    if current_view == "ajuda":
        render_help_dashboard()
        return
