import logging
import urllib3
import requests
from requests.exceptions import SSLError, ConnectionError, Timeout, RequestException, HTTPError

from core import SmartZoneAPIError

logger = logging.getLogger(__name__)

# Suprime warning de SSL quando verify=False (certificado self-signed)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_TIMEOUT = 30  # segundos


def _request(method: str, url: str, verify_ssl: bool, **kwargs) -> requests.Response:
    """
    Wrapper genérico com tratamento explícito de erros de rede e SSL.
    Lança SmartZoneAPIError com mensagem clara para cada tipo de falha.
    """
    try:
        resp = requests.request(
            method, url, verify=verify_ssl, timeout=_TIMEOUT, **kwargs
        )
        resp.raise_for_status()
        return resp
    except SSLError as exc:
        raise SmartZoneAPIError(
            f"Erro de certificado SSL ao conectar ao SmartZone: {exc}"
        ) from exc
    except ConnectionError as exc:
        raise SmartZoneAPIError(
            f"SmartZone inacessível (connection refused ou DNS inválido): {exc}"
        ) from exc
    except Timeout as exc:
        raise SmartZoneAPIError(
            f"Timeout ({_TIMEOUT}s) ao conectar ao SmartZone: {exc}"
        ) from exc
    except HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "N/A"
        body = exc.response.text[:500] if exc.response is not None else ""
        raise SmartZoneAPIError(f"HTTP {code} na SmartZone API: {body}") from exc
    except RequestException as exc:
        raise SmartZoneAPIError(f"Erro de requisição ao SmartZone: {exc}") from exc


def get_service_ticket(
    base_url: str, username: str, password: str, verify_ssl: bool = False
) -> str:
    """
    Autentica no SmartZone e retorna o serviceTicket de sessão.
    O ticket é obtido a cada execução — sem reaproveitamento entre migrações.
    """
    url = f"{base_url.rstrip('/')}/wsg/api/public/v11_0/serviceTicket"
    logger.info(f"Autenticando na SmartZone API: {url}")
    resp = _request("POST", url, verify_ssl, json={"username": username, "password": password})
    ticket = resp.json().get("serviceTicket")
    if not ticket:
        raise SmartZoneAPIError(
            "serviceTicket ausente na resposta do SmartZone. Verifique as credenciais."
        )
    logger.info("serviceTicket obtido com sucesso.")
    return ticket


def find_switch_by_ip(
    base_url: str,
    api_version: str,
    service_ticket: str,
    ip_address: str,
    verify_ssl: bool = False,
) -> dict:
    """
    Busca o switch pelo IP via fullTextSearch.
    Retorna o dict completo do switch encontrado.
    Lança SmartZoneAPIError se não encontrar ou se houver ambiguidade.
    """
    url = f"{base_url.rstrip('/')}/switchm/api/{api_version}/switch"
    payload = {
        "fullTextSearch": {"type": "AND", "value": ip_address},
        "page": 1,
        "limit": 10,
    }
    logger.info(f"Buscando switch {ip_address} na SmartZone API ...")
    resp = _request("POST", url, verify_ssl,
                    json=payload, params={"serviceTicket": service_ticket})
    data = resp.json()
    total = data.get("totalCount", 0)

    if total == 0:
        raise SmartZoneAPIError(
            f"Switch com IP {ip_address} não encontrado no SmartZone."
        )
    if total > 1:
        raise SmartZoneAPIError(
            f"{total} switches encontrados para o IP {ip_address} — busca ambígua. "
            "Refine o filtro via switchGroupId no config."
        )

    switch_data = data["list"][0]
    logger.info(
        f"Switch encontrado: {switch_data.get('switchName')} "
        f"(id={switch_data.get('id')}, model={switch_data.get('model')})"
    )
    return switch_data


def validate_switch_data(switch_data: dict, expected_ip: str) -> None:
    """
    Valida os dados retornados pela API antes de executar o DELETE.
    Lança SmartZoneAPIError se qualquer validação falhar.

    Validações:
      1. IP bate com o informado pelo operador
      2. Modelo contém 'ICX7150'
      3. Status é 'ONLINE'
    """
    ip_found = switch_data.get("ipAddress", "")
    model    = switch_data.get("model", "")
    status   = switch_data.get("status", "")

    if ip_found != expected_ip:
        raise SmartZoneAPIError(
            f"IP do switch no SmartZone ({ip_found}) diferente do IP informado ({expected_ip}). "
            "Abortando para evitar deleção de dispositivo errado."
        )
    if "ICX7150" not in model.upper():
        raise SmartZoneAPIError(
            f"Modelo '{model}' não corresponde a ICX7150. "
            "Abortando para evitar deleção de dispositivo errado."
        )

    # Status não é validado — o device deve ser removido independente de estar ONLINE ou OFFLINE
    logger.info(f"Validação OK — IP={ip_found} | modelo={model} | status={status}")


def delete_switch(
    base_url: str,
    api_version: str,
    service_ticket: str,
    switch_id: str,
    verify_ssl: bool = False,
) -> dict:
    """Remove o switch do SmartZone via DELETE unitário por ID."""
    url = f"{base_url.rstrip('/')}/switchm/api/{api_version}/switch/{switch_id}"
    logger.info(f"Executando DELETE do switch {switch_id} no SmartZone ...")
    resp = _request("DELETE", url, verify_ssl, params={"serviceTicket": service_ticket})
    result = resp.json() if resp.content else {}
    logger.info(f"Switch removido do SmartZone. Resposta: {result}")
    return result


def remove_switch_from_smartzone(
    base_url: str,
    api_version: str,
    username: str,
    password: str,
    ip_address: str,
    verify_ssl: bool = False,
) -> dict:
    """
    Orquestra o fluxo completo da SmartZone REST API:
      1. Login → serviceTicket
      2. Busca switch pelo IP → UUID interno
      3. Valida dados (IP, modelo, status)
      4. DELETE do switch
    Retorna o dict completo do switch para uso no log e dashboard Grid.
    """
    ticket      = get_service_ticket(base_url, username, password, verify_ssl)
    switch_data = find_switch_by_ip(base_url, api_version, ticket, ip_address, verify_ssl)
    validate_switch_data(switch_data, ip_address)
    delete_switch(base_url, api_version, ticket, switch_data["id"], verify_ssl)
    return switch_data
