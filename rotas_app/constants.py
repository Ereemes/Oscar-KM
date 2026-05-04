from __future__ import annotations

from pathlib import Path

APP_TITLE = "Distancia CD x Lojas"
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_FILE = DATA_DIR / "sistema_rotas.db"
CONFIG_FILE = DATA_DIR / "configuracoes.csv"
CD_FILE = DATA_DIR / "centro_distribuicao.csv"
STORES_FILE = DATA_DIR / "lojas.csv"
ROUTES_FILE = DATA_DIR / "rotas_calculadas.csv"
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/{coordinates}"
ROUTING_TIMEOUT_SECONDS = 10
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
VIACEP_URL = "https://viacep.com.br/ws/{cep}/json/"
GEOCODING_TIMEOUT_SECONDS = 15
GEOCODING_DELAY_SECONDS = 1.1
GEOCODING_USER_AGENT = "distancia-cd-lojas-mvp/1.0 (streamlit)"

CONFIG_COLUMNS = ["km_por_litro", "valor_combustivel", "ida_e_volta"]
CD_COLUMNS = ["nome", "endereco", "latitude", "longitude", "updated_at"]
STORE_COLUMNS = ["id", "nome", "endereco", "latitude", "longitude", "status_coordenada", "erro_coordenada"]
ROUTE_COLUMNS = [
    "store_id",
    "cd_latitude",
    "cd_longitude",
    "store_latitude",
    "store_longitude",
    "distance_km",
    "duration_min",
    "geometry_json",
    "status",
    "error",
    "updated_at",
]

DEFAULT_CONFIG = {
    "km_por_litro": 8.0,
    "valor_combustivel": 5.5,
    "ida_e_volta": True,
}
