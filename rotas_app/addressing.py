from __future__ import annotations

import re
from typing import Any

from .utils import clean_cep, clean_text_value, normalize_text

def number_is_already_in_street(street: str, number_text: str) -> bool:
    """Evita montar endereços como 'Rua X, 149, 149'."""
    if not street or not number_text:
        return False

    number_digits = "".join(char for char in str(number_text) if char.isdigit())
    if not number_digits:
        return False

    return bool(re.search(rf"(?<!\d){re.escape(number_digits)}(?!\d)", str(street)))

def split_street_complement(street: str) -> tuple[str, str]:
    """Separa complemento de loja quando o número está em coluna separada.

    Exemplo:
    'Av. Dep. Benedito Matarazzo, lj 509/10' + número 9403
    vira base 'Av. Dep. Benedito Matarazzo' e complemento 'lj 509/10'.
    """
    text = clean_text_value(street)
    if not text:
        return "", ""

    pattern = re.compile(
        r"^(?P<base>.*?)(?:,?\s*-\s*|,\s*|\s+)"
        r"(?P<complement>(?:loja|lj|sala|box|quiosque|suc|suc\.|loja\s+s/?|piso|mall)\b.*)$",
        flags=re.IGNORECASE,
    )
    match = pattern.match(text)
    if not match:
        return text, ""

    base = match.group("base").strip(" ,-")
    complement = match.group("complement").strip(" ,-")
    if not base:
        return text, ""
    return base, complement

def remove_store_complement_for_geocode(street: str) -> str:
    """Remove loja/sala/complemento para melhorar a chance do geocodificador achar o prédio."""
    text = clean_text_value(street)
    if not text:
        return ""

    patterns = [
        r"\s*-\s*(loja|lj|sala|box|quiosque|suc|suc\.|loja\s+s/?|piso|mall)\b.*$",
        r"\s+(loja|lj|sala|box|quiosque|suc|suc\.|loja\s+s/?|piso|mall)\b.*$",
    ]
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip(" ,-")
    return cleaned

def expand_address_abbreviations(text: str) -> str:
    """Expande abreviações comuns que atrapalham geocodificação."""
    value = clean_text_value(text)
    replacements = [
        (r"\bAv\.?(?=\s|,|$)", "Avenida"),
        (r"\bR\.?(?=\s|,|$)", "Rua"),
        (r"\bPca\.?(?=\s|,|$)", "Praça"),
        (r"\bPraca\b", "Praça"),
        (r"\bJd\.?(?=\s|,|$)", "Jardim"),
        (r"\bVl\.?(?=\s|,|$)", "Vila"),
        (r"\bDep\.?(?=\s|,|$)", "Deputado"),
    ]
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    return " ".join(value.split())

def build_street_part(address: Any, number: Any = "") -> str:
    street = clean_text_value(address)
    number_text = clean_text_value(number)

    if not street:
        return number_text

    if not number_text or number_is_already_in_street(street, number_text):
        return street

    base, complement = split_street_complement(street)
    if complement:
        return f"{base}, {number_text} - {complement}"

    return f"{street}, {number_text}"

def compose_address_parts(
    street_part: str = "",
    neighborhood: Any = "",
    city: Any = "",
    state: Any = "",
    cep: Any = "",
    include_brazil: bool = True,
) -> str:
    neighborhood_text = clean_text_value(neighborhood)
    city_text = clean_text_value(city)
    state_text = clean_text_value(state).upper()
    cep_text = clean_cep(cep)

    address_parts: list[str] = []
    if street_part:
        address_parts.append(clean_text_value(street_part))
    elif cep_text:
        address_parts.append(cep_text)

    if neighborhood_text:
        address_parts.append(neighborhood_text)

    city_state = ""
    if city_text and state_text:
        city_state = f"{city_text} - {state_text}"
    elif city_text:
        city_state = city_text
    elif state_text:
        city_state = state_text
    if city_state:
        address_parts.append(city_state)

    if cep_text and cep_text not in address_parts:
        address_parts.append(cep_text)

    if address_parts and include_brazil:
        address_parts.append("Brasil")

    return ", ".join(part for part in address_parts if part)

def build_full_address(
    address: Any,
    number: Any = "",
    neighborhood: Any = "",
    city: Any = "",
    state: Any = "",
    cep: Any = "",
) -> str:
    street_part = build_street_part(address, number)
    return compose_address_parts(street_part, neighborhood, city, state, cep)

def unique_addresses(addresses: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for address in addresses:
        clean_address = clean_text_value(address)
        key = normalize_text(clean_address)
        if clean_address and key not in seen:
            seen.add(key)
            unique.append(clean_address)
    return unique

def build_geocode_candidates(
    address: Any,
    number: Any = "",
    neighborhood: Any = "",
    city: Any = "",
    state: Any = "",
    cep: Any = "",
) -> list[str]:
    """Gera tentativas de busca para endereços vindos da planilha.

    A primeira opção é o endereço completo salvo no sistema.
    As demais removem complemento de loja e expandem abreviações como Av./Jd./Dep.
    """
    full_address = build_full_address(address, number, neighborhood, city, state, cep)
    street_part = build_street_part(address, number)
    street_without_complement = remove_store_complement_for_geocode(street_part)
    street_expanded = expand_address_abbreviations(street_without_complement or street_part)
    neighborhood_expanded = expand_address_abbreviations(clean_text_value(neighborhood))

    candidates = [
        full_address,
        compose_address_parts(street_without_complement, neighborhood, city, state, cep),
        compose_address_parts(street_expanded, neighborhood_expanded, city, state, cep),
        compose_address_parts(street_expanded, "", city, state, cep),
        compose_address_parts(street_expanded, "", city, state, ""),
        compose_address_parts(street_expanded, neighborhood_expanded, city, state, ""),
    ]

    cep_text = clean_cep(cep)
    city_text = clean_text_value(city)
    state_text = clean_text_value(state).upper()
    if cep_text:
        candidates.append(compose_address_parts(cep_text, "", city_text, state_text, ""))

    expanded_candidates = candidates + [expand_address_abbreviations(candidate) for candidate in candidates]
    return unique_addresses(expanded_candidates)
