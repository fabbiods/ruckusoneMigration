import csv
import logging
import os
import time
import requests
from requests.exceptions import RequestException, HTTPError

from core import RuckusOneAPIError

logger = logging.getLogger(__name__)

_TIMEOUT = 30  # segundos por requisição


# ── Autenticação ──────────────────────────────────────────────────────────────

def get_access_token(token_url: str, tenant_id: str, client_id: str, client_secret: str) -> str:
    """
    Autentica via OAuth2 client credentials e retorna o Bearer token.
    Token válido por ~2h. Gerar novo a cada execução do pipeline.
    """
    url = f"{token_url.rstrip('/')}/{tenant_id}"
    logger.info(f"Autenticando na Ruckus One API: {url}")
    try:
        resp = requests.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise RuckusOneAPIError("access_token ausente na resposta da autenticação Ruckus One.")
        logger.info("Token Ruckus One obtido com sucesso.")
        return token
    except HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "N/A"
        body = exc.response.text[:300] if exc.response is not None else ""
        raise RuckusOneAPIError(f"HTTP {code} ao autenticar no Ruckus One: {body}") from exc
    except RequestException as exc:
        raise RuckusOneAPIError(f"Erro de conexão com Ruckus One: {exc}") from exc


# ── Venues ────────────────────────────────────────────────────────────────────

def list_venues(api_base: str, token: str) -> list:
    """
    GET /venues — retorna todas as venues do tenant.
    O filtro ?name= não funciona server-side; filtrar client-side.
    """
    url = f"{api_base.rstrip('/')}/venues"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json() or []
    except RequestException as exc:
        raise RuckusOneAPIError(f"Erro ao listar venues: {exc}") from exc


def find_venue_by_slug(api_base: str, token: str, site_slug: str) -> dict | None:
    """
    Busca venue pelo site_slug do Netbox (comparação client-side com venue['name']).
    Retorna None se a venue não existir no Ruckus One.
    """
    venues = list_venues(api_base, token)
    for venue in venues:
        if venue.get("name", "").lower() == site_slug.lower():
            logger.info(f"Venue encontrada: '{venue['name']}' (id={venue['id']})")
            return venue
    logger.warning(f"Venue '{site_slug}' não encontrada no Ruckus One ({len(venues)} venues consultadas).")
    return None


# ── Switches pendentes ────────────────────────────────────────────────────────

def list_pending_switches(api_base: str, token: str) -> list:
    """
    GET /switches/pending — switches que conectaram ao Ruckus One mas
    ainda não foram associados a nenhuma venue.
    Retorna lista vazia quando não há pendentes (HTTP 404 normal).
    """
    url = f"{api_base.rstrip('/')}/switches/pending"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("list", [])
    except RequestException as exc:
        raise RuckusOneAPIError(f"Erro ao buscar switches pendentes: {exc}") from exc


def wait_for_pending_switch(
    api_base: str,
    token: str,
    identifier: str,
    total_timeout: int = 300,
    interval: int = 30,
    progress_callback=None,
) -> dict | None:
    """
    Aguarda o switch aparecer em /switches/pending após a migração.
    Compara por MAC address, serial ou device_name (case-insensitive).
    Retorna o dict do switch ou None se não aparecer dentro do timeout.

    CUSTOMIZE: ajuste total_timeout e interval conforme o tempo de boot do switch.
    """
    elapsed = 0
    attempt = 0
    while elapsed < total_timeout:
        attempt += 1
        msg = f"Aguardando switch em pending assets (tentativa {attempt}, elapsed={elapsed}s/{total_timeout}s) ..."
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

        pending = list_pending_switches(api_base, token)

        if attempt == 1:
            if pending:
                logger.info(f"[pending] {len(pending)} switch(es) aguardando. "
                            f"Campos do primeiro item: {list(pending[0].keys())}")
                logger.info(f"[pending] Primeiro item completo: {pending[0]}")
            else:
                logger.info("[pending] Nenhum switch encontrado ainda.")

        def _normalize(v: str) -> str:
            return v.lower().replace(":", "").replace("-", "").replace(".", "")

        if not identifier:
            logger.warning("Identificador do switch é None — não é possível buscar em pending.")
            return None

        id_norm = _normalize(identifier)

        for sw in pending:
            candidates = [
                str(sw.get("id",           "") or ""),
                str(sw.get("name",         "") or ""),
                str(sw.get("switchName",   "") or ""),
                str(sw.get("macAddress",   "") or ""),
                str(sw.get("serialNumber", "") or ""),
            ]
            if any(_normalize(v) == id_norm for v in candidates if v):
                logger.info(f"Switch encontrado em pending: {sw}")
                return sw

        time.sleep(interval)
        elapsed += interval

    logger.warning(f"Switch '{identifier}' não apareceu em pending em {total_timeout}s.")
    return None


# ── Associação à venue ────────────────────────────────────────────────────────

def assign_switch_to_venue(
    api_base: str,
    token: str,
    venue_id: str,
    switch_id: str,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """
    Adiciona/associa um switch ICX a uma venue.

    Endpoint oficial (Switch Services API v0.4.0, operationId AddSwitch):
        POST /venues/{venueId}/switches/{switchId}

    - venueId e switchId são parâmetros de PATH (ambos obrigatórios).
      switchId é o identificador do switch (serial ou MAC).
    - O body (schema IcxSwitch_V1_1) é obrigatório estar presente, mas
      NENHUMA propriedade individual é obrigatória no schema. Enviamos
      apenas campos úteis (name/description) quando disponíveis.
    - Resposta esperada: 202 Accepted (assíncrono). Confirmar via activity
      API com o requestId, não na resposta imediata.
    """
    url = f"{api_base.rstrip('/')}/venues/{venue_id}/switches/{switch_id}"
    # A API exige o 'id' também no body (SWITCH-10447: "Switch ID is mandatory"),
    # além do switchId no path.
    payload: dict = {"id": switch_id}
    if name:
        payload["name"] = name
    if description:
        payload["description"] = description

    logger.info(
        f"Adicionando switch '{switch_id}' à venue '{venue_id}' "
        f"(POST {url}) payload={payload}"
    )
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json() if resp.content else {}
        logger.info(
            f"Switch '{switch_id}' aceito pela venue (HTTP {resp.status_code}). "
            f"Resposta: {result}"
        )
        return result
    except HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "N/A"
        body = exc.response.text[:500] if exc.response is not None else ""
        raise RuckusOneAPIError(f"HTTP {code} ao adicionar switch à venue: {body}") from exc
    except RequestException as exc:
        raise RuckusOneAPIError(f"Erro de conexão ao adicionar switch: {exc}") from exc


# ── Lookup Netbox ─────────────────────────────────────────────────────────────

def get_site_slug_from_netbox(device_name: str, csv_path: str) -> tuple[str | None, str | None]:
    """
    Busca site_slug e site_name pelo device_name no CSV exportado do Netbox.
    Retorna (site_slug, site_name) ou (None, None) se não encontrado.
    """
    if not os.path.exists(csv_path):
        logger.warning(f"CSV Netbox não encontrado: {csv_path}")
        return None, None
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("device_name", "").strip() == device_name.strip():
                    slug = row.get("site_slug", "").strip()
                    name = row.get("site_name", "").strip()
                    logger.info(
                        f"Netbox lookup: '{device_name}' → site_slug='{slug}', site_name='{name}'"
                    )
                    return (slug or None, name or None)
        logger.warning(f"device_name '{device_name}' não encontrado no CSV do Netbox.")
    except Exception as exc:
        logger.warning(f"Erro ao ler CSV Netbox: {exc}")
    return None, None
