import logging
from datetime import datetime

import requests
from requests.exceptions import RequestException, HTTPError

from core import GridAPIError

logger = logging.getLogger(__name__)

_API_REQUEST_TIMEOUT = 30  # segundos


def _headers(api_token: str) -> dict:
    """
    Headers de autenticação para a API Grid.
    Usa token nativo grid_sk_* (Bearer) + x-api-source exigido pela API.
    """
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-api-source": "office",
    }


def get_grid_state(doc_id: str, api_base_url: str, api_token: str) -> tuple[dict, str]:
    """
    Lê o state colaborativo do documento Grid.
    GET /documents/{doc_id}/state → {"doc_id", "state": {...}, "updated_at": "..."}

    Retorna (state, updated_at). Em caso de falha retorna ({}, "") para
    permitir criar o state do zero sem abortar a migração.
    """
    url = f"{api_base_url.rstrip('/')}/documents/{doc_id}/state"
    try:
        resp = requests.get(
            url, headers=_headers(api_token), timeout=_API_REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        body = resp.json() or {}
        return body.get("state") or {}, body.get("updated_at") or ""
    except Exception as exc:
        logger.warning(f"Não foi possível ler state do Grid (iniciando do zero): {exc}")
        return {}, ""


def put_grid_state(
    doc_id: str,
    api_base_url: str,
    api_token: str,
    state: dict,
    if_updated_at: str = "",
) -> dict:
    """
    Sobrescreve o state colaborativo do documento Grid (overwrite total).
    PUT /documents/{doc_id}/state com body {"state": ..., "if_updated_at": ...}

    if_updated_at: token de concorrência otimista obtido no get_grid_state —
    evita sobrescrever alterações concorrentes (passar "" ignora a checagem).
    """
    url = f"{api_base_url.rstrip('/')}/documents/{doc_id}/state"
    try:
        resp = requests.put(
            url,
            json={"state": state, "if_updated_at": if_updated_at},
            headers=_headers(api_token),
            timeout=_API_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "N/A"
        body = exc.response.text[:500] if exc.response is not None else ""
        raise GridAPIError(f"HTTP {code} ao atualizar Grid State: {body}") from exc
    except RequestException as exc:
        raise GridAPIError(f"Erro de conexão com Grid API: {exc}") from exc
    except Exception as exc:
        raise GridAPIError(f"Erro inesperado ao chamar Grid API: {exc}") from exc


def append_migration_record(
    doc_id: str, api_base_url: str, api_token: str, record: dict
) -> dict:
    """
    Lê o state atual, appenda o novo registro de migração e salva.
    Retorna o state atualizado.

    Estrutura do Grid State (consumida pelo dashboard via window.GRID.state.get()):
    {
      "last_update": "YYYY-MM-DD HH:MM:SS",
      "migrations": [ { ...registro... }, ... ]
    }
    """
    current, updated_at = get_grid_state(doc_id, api_base_url, api_token)
    migrations = current.get("migrations", [])
    migrations.append(record)

    new_state = {
        **current,
        "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "migrations": migrations,
    }

    result = put_grid_state(doc_id, api_base_url, api_token, new_state, updated_at)
    logger.info(
        f"Registro adicionado ao Grid State. Total de registros: {len(migrations)}"
    )
    return result
