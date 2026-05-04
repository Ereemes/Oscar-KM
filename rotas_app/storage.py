from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import pandas as pd

from .constants import (
    CD_COLUMNS,
    CD_FILE,
    CONFIG_FILE,
    DATA_DIR,
    DB_FILE,
    DEFAULT_CONFIG,
    ROUTE_COLUMNS,
    ROUTES_FILE,
    STORE_COLUMNS,
    STORES_FILE,
)
from .utils import has_valid_coordinates, normalize_text, optional_float_value

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def count_rows(conn: sqlite3.Connection, table_name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])

def ensure_lojas_schema(conn: sqlite3.Connection) -> None:
    """Mantem a tabela de lojas compatível com lojas pendentes de coordenada."""
    columns_info = conn.execute("PRAGMA table_info(lojas)").fetchall()
    if not columns_info:
        return

    columns = {row["name"]: row for row in columns_info}
    latitude_not_null = bool(columns.get("latitude") and columns["latitude"]["notnull"])
    longitude_not_null = bool(columns.get("longitude") and columns["longitude"]["notnull"])

    if latitude_not_null or longitude_not_null:
        old_columns = set(columns.keys())
        conn.execute("ALTER TABLE lojas RENAME TO lojas_old")
        conn.execute(
            """
            CREATE TABLE lojas (
                id INTEGER PRIMARY KEY,
                nome TEXT NOT NULL,
                endereco TEXT NOT NULL,
                latitude REAL,
                longitude REAL,
                status_coordenada TEXT NOT NULL DEFAULT 'ok',
                erro_coordenada TEXT NOT NULL DEFAULT '',
                nome_normalizado TEXT NOT NULL
            )
            """
        )
        status_expr = (
            "status_coordenada"
            if "status_coordenada" in old_columns
            else "CASE WHEN latitude IS NULL OR longitude IS NULL THEN 'pendente' ELSE 'ok' END"
        )
        error_expr = "erro_coordenada" if "erro_coordenada" in old_columns else "''"
        normalized_expr = "nome_normalizado" if "nome_normalizado" in old_columns else "lower(trim(nome))"
        conn.execute(
            f"""
            INSERT INTO lojas
            (id, nome, endereco, latitude, longitude, status_coordenada, erro_coordenada, nome_normalizado)
            SELECT id, nome, endereco, latitude, longitude, {status_expr}, {error_expr}, {normalized_expr}
            FROM lojas_old
            """
        )
        conn.execute("DROP TABLE lojas_old")
    else:
        if "status_coordenada" not in columns:
            conn.execute("ALTER TABLE lojas ADD COLUMN status_coordenada TEXT NOT NULL DEFAULT 'ok'")
        if "erro_coordenada" not in columns:
            conn.execute("ALTER TABLE lojas ADD COLUMN erro_coordenada TEXT NOT NULL DEFAULT ''")

def ensure_cd_schema(conn: sqlite3.Connection) -> None:
    columns_info = conn.execute("PRAGMA table_info(centro_distribuicao)").fetchall()
    if not columns_info:
        return

    columns = {row["name"]: row for row in columns_info}
    if "updated_at" not in columns:
        conn.execute("ALTER TABLE centro_distribuicao ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "UPDATE centro_distribuicao SET updated_at = ? WHERE updated_at = ''",
            (datetime.now().isoformat(timespec="seconds"),),
        )

def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS configuracoes (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            km_por_litro REAL NOT NULL,
            valor_combustivel REAL NOT NULL,
            ida_e_volta INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS centro_distribuicao (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            nome TEXT NOT NULL,
            endereco TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    ensure_cd_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lojas (
            id INTEGER PRIMARY KEY,
            nome TEXT NOT NULL,
            endereco TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            status_coordenada TEXT NOT NULL DEFAULT 'ok',
            erro_coordenada TEXT NOT NULL DEFAULT '',
            nome_normalizado TEXT NOT NULL
        )
        """
    )
    ensure_lojas_schema(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lojas_nome ON lojas (nome_normalizado)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rotas_calculadas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL,
            cd_latitude REAL NOT NULL,
            cd_longitude REAL NOT NULL,
            store_latitude REAL NOT NULL,
            store_longitude REAL NOT NULL,
            distance_km REAL,
            duration_min REAL,
            geometry_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE (store_id, cd_latitude, cd_longitude, store_latitude, store_longitude)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rotas_store_id ON rotas_calculadas (store_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geocoding_cache (
            endereco_normalizado TEXT PRIMARY KEY,
            endereco_original TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )

def csv_bool(value: Any, default: bool) -> bool:
    if pd.isna(value):
        return default
    return str(value).strip().lower() in {"true", "1", "sim", "yes", "y"}

def migrate_csv_data_if_needed(conn: sqlite3.Connection) -> None:
    if count_rows(conn, "configuracoes") == 0:
        config = DEFAULT_CONFIG.copy()
        if CONFIG_FILE.exists():
            try:
                config_df = pd.read_csv(CONFIG_FILE)
                if not config_df.empty:
                    row = config_df.iloc[0].to_dict()
                    config = {
                        "km_por_litro": float(row.get("km_por_litro", config["km_por_litro"])),
                        "valor_combustivel": float(row.get("valor_combustivel", config["valor_combustivel"])),
                        "ida_e_volta": csv_bool(row.get("ida_e_volta"), config["ida_e_volta"]),
                    }
            except Exception:
                config = DEFAULT_CONFIG.copy()
        conn.execute(
            """
            INSERT INTO configuracoes (id, km_por_litro, valor_combustivel, ida_e_volta)
            VALUES (1, ?, ?, ?)
            """,
            (config["km_por_litro"], config["valor_combustivel"], int(config["ida_e_volta"])),
        )

    if count_rows(conn, "centro_distribuicao") == 0 and CD_FILE.exists():
        try:
            cd_df = pd.read_csv(CD_FILE)
            if not cd_df.empty:
                row = cd_df.iloc[0]
                conn.execute(
                    """
                    INSERT OR REPLACE INTO centro_distribuicao
                    (id, nome, endereco, latitude, longitude, updated_at)
                    VALUES (1, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row.get("nome", "")).strip(),
                        str(row.get("endereco", "")).strip(),
                        float(row.get("latitude")),
                        float(row.get("longitude")),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
        except Exception:
            pass

    if count_rows(conn, "lojas") == 0 and STORES_FILE.exists():
        try:
            stores_df = pd.read_csv(STORES_FILE)
            for _, row in stores_df.iterrows():
                if pd.isna(row.get("id")):
                    continue
                name = str(row.get("nome", "")).strip()
                address = str(row.get("endereco", "")).strip()
                if not name:
                    continue
                latitude = optional_float_value(row.get("latitude"))
                longitude = optional_float_value(row.get("longitude"))
                status = "ok" if latitude is not None and longitude is not None else "pendente"
                error = "" if status == "ok" else "Loja migrada sem latitude/longitude."
                conn.execute(
                    """
                    INSERT OR REPLACE INTO lojas
                    (id, nome, endereco, latitude, longitude, status_coordenada, erro_coordenada, nome_normalizado)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(row.get("id")),
                        name,
                        address,
                        latitude,
                        longitude,
                        status,
                        error,
                        normalize_text(name),
                    ),
                )
        except Exception:
            pass

    if count_rows(conn, "rotas_calculadas") == 0 and ROUTES_FILE.exists():
        try:
            routes_df = pd.read_csv(ROUTES_FILE).reindex(columns=ROUTE_COLUMNS)
            for _, row in routes_df.iterrows():
                if pd.isna(row.get("store_id")):
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO rotas_calculadas
                    (store_id, cd_latitude, cd_longitude, store_latitude, store_longitude,
                     distance_km, duration_min, geometry_json, status, error, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(row.get("store_id")),
                        float(row.get("cd_latitude")),
                        float(row.get("cd_longitude")),
                        float(row.get("store_latitude")),
                        float(row.get("store_longitude")),
                        None if pd.isna(row.get("distance_km")) else float(row.get("distance_km")),
                        None if pd.isna(row.get("duration_min")) else float(row.get("duration_min")),
                        str(row.get("geometry_json") or "[]"),
                        str(row.get("status") or "pendente"),
                        str(row.get("error") or ""),
                        str(row.get("updated_at") or datetime.now().isoformat(timespec="seconds")),
                    ),
                )
        except Exception:
            pass

def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        create_tables(conn)
        migrate_csv_data_if_needed(conn)
        conn.commit()

def load_config() -> dict[str, Any]:
    ensure_storage()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT km_por_litro, valor_combustivel, ida_e_volta FROM configuracoes WHERE id = 1"
        ).fetchone()
    if row is None:
        return DEFAULT_CONFIG.copy()

    return {
        "km_por_litro": float(row["km_por_litro"]),
        "valor_combustivel": float(row["valor_combustivel"]),
        "ida_e_volta": bool(row["ida_e_volta"]),
    }

def save_config(km_por_litro: float, valor_combustivel: float, ida_e_volta: bool) -> None:
    ensure_storage()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO configuracoes (id, km_por_litro, valor_combustivel, ida_e_volta)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                km_por_litro = excluded.km_por_litro,
                valor_combustivel = excluded.valor_combustivel,
                ida_e_volta = excluded.ida_e_volta
            """,
            (float(km_por_litro), float(valor_combustivel), int(ida_e_volta)),
        )
        conn.commit()

def load_cd() -> pd.DataFrame:
    ensure_storage()
    with get_connection() as conn:
        cd = pd.read_sql_query(
            "SELECT nome, endereco, latitude, longitude, updated_at FROM centro_distribuicao WHERE id = 1",
            conn,
        )
    if cd.empty:
        return pd.DataFrame(columns=CD_COLUMNS)

    for column in ["nome", "endereco"]:
        cd[column] = cd[column].fillna("").astype(str)
    return cd.reindex(columns=CD_COLUMNS)

def save_cd(nome: str, endereco: str, latitude: float, longitude: float) -> None:
    ensure_storage()
    updated_at = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO centro_distribuicao (id, nome, endereco, latitude, longitude, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                nome = excluded.nome,
                endereco = excluded.endereco,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                updated_at = excluded.updated_at
            """,
            (nome.strip(), endereco.strip(), float(latitude), float(longitude), updated_at),
        )
        conn.commit()

def load_stores() -> pd.DataFrame:
    ensure_storage()
    with get_connection() as conn:
        stores = pd.read_sql_query(
            """
            SELECT id, nome, endereco, latitude, longitude, status_coordenada, erro_coordenada
            FROM lojas
            ORDER BY id
            """,
            conn,
        )
    if stores.empty:
        return pd.DataFrame(columns=STORE_COLUMNS)

    stores["id"] = stores["id"].astype(int)
    stores["latitude"] = pd.to_numeric(stores["latitude"], errors="coerce")
    stores["longitude"] = pd.to_numeric(stores["longitude"], errors="coerce")
    for column in ["nome", "endereco", "status_coordenada", "erro_coordenada"]:
        stores[column] = stores[column].fillna("").astype(str)
    stores.loc[stores["status_coordenada"].eq(""), "status_coordenada"] = stores.apply(
        lambda row: "ok" if has_valid_coordinates(row) else "pendente",
        axis=1,
    )
    return stores.reindex(columns=STORE_COLUMNS)

def save_stores(stores: pd.DataFrame) -> None:
    ensure_storage()
    stores = stores.reindex(columns=STORE_COLUMNS).copy()
    with get_connection() as conn:
        conn.execute("DELETE FROM lojas")
        for _, row in stores.iterrows():
            if pd.isna(row.get("id")):
                continue
            name = str(row.get("nome", "")).strip()
            if not name:
                continue
            latitude = optional_float_value(row.get("latitude"))
            longitude = optional_float_value(row.get("longitude"))
            status = str(row.get("status_coordenada") or "").strip().lower()
            if not status:
                status = "ok" if latitude is not None and longitude is not None else "pendente"
            error = str(row.get("erro_coordenada") or "").strip()
            conn.execute(
                """
                INSERT INTO lojas
                (id, nome, endereco, latitude, longitude, status_coordenada, erro_coordenada, nome_normalizado)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(row["id"]),
                    name,
                    str(row.get("endereco", "")).strip(),
                    latitude,
                    longitude,
                    status,
                    error,
                    normalize_text(name),
                ),
            )
        conn.commit()

def load_routes() -> pd.DataFrame:
    ensure_storage()
    with get_connection() as conn:
        routes = pd.read_sql_query(
            """
            SELECT store_id, cd_latitude, cd_longitude, store_latitude, store_longitude,
                   distance_km, duration_min, geometry_json, status, error, updated_at
            FROM rotas_calculadas
            ORDER BY updated_at DESC
            """,
            conn,
        )
    if routes.empty:
        return pd.DataFrame(columns=ROUTE_COLUMNS)

    routes = routes.reindex(columns=ROUTE_COLUMNS)
    numeric_columns = [
        "store_id",
        "cd_latitude",
        "cd_longitude",
        "store_latitude",
        "store_longitude",
        "distance_km",
        "duration_min",
    ]
    for column in numeric_columns:
        routes[column] = pd.to_numeric(routes[column], errors="coerce")

    routes["status"] = routes["status"].fillna("").astype(str)
    routes["error"] = routes["error"].fillna("").astype(str)
    routes["geometry_json"] = routes["geometry_json"].fillna("[]").astype(str)
    routes["updated_at"] = routes["updated_at"].fillna("").astype(str)
    return routes

def save_routes(routes: pd.DataFrame) -> None:
    ensure_storage()
    routes = routes.reindex(columns=ROUTE_COLUMNS).copy()
    with get_connection() as conn:
        conn.execute("DELETE FROM rotas_calculadas")
        for _, row in routes.iterrows():
            upsert_route(row.to_dict(), conn=conn, commit=False)
        conn.commit()

def upsert_route(route_data: dict[str, Any], conn: sqlite3.Connection | None = None, commit: bool = True) -> None:
    should_close = conn is None
    active_conn = conn or get_connection()
    try:
        active_conn.execute(
            """
            INSERT INTO rotas_calculadas
            (store_id, cd_latitude, cd_longitude, store_latitude, store_longitude,
             distance_km, duration_min, geometry_json, status, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(store_id, cd_latitude, cd_longitude, store_latitude, store_longitude)
            DO UPDATE SET
                distance_km = excluded.distance_km,
                duration_min = excluded.duration_min,
                geometry_json = excluded.geometry_json,
                status = excluded.status,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                int(route_data["store_id"]),
                float(route_data["cd_latitude"]),
                float(route_data["cd_longitude"]),
                float(route_data["store_latitude"]),
                float(route_data["store_longitude"]),
                None if pd.isna(route_data.get("distance_km")) else float(route_data.get("distance_km")),
                None if pd.isna(route_data.get("duration_min")) else float(route_data.get("duration_min")),
                str(route_data.get("geometry_json") or "[]"),
                str(route_data.get("status") or "pendente"),
                str(route_data.get("error") or ""),
                str(route_data.get("updated_at") or datetime.now().isoformat(timespec="seconds")),
            ),
        )
        if commit:
            active_conn.commit()
    finally:
        if should_close:
            active_conn.close()

def clear_routes() -> None:
    ensure_storage()
    with get_connection() as conn:
        conn.execute("DELETE FROM rotas_calculadas")
        conn.commit()

def delete_routes_for_store(store_id: int) -> None:
    ensure_storage()
    with get_connection() as conn:
        conn.execute("DELETE FROM rotas_calculadas WHERE store_id = ?", (int(store_id),))
        conn.commit()
