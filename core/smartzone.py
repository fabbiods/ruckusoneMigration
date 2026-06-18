import logging
from typing import Callable, Optional

from netmiko import ConnectHandler

from core import SmartZoneRemovalError
from core.ssh_client import send_with_confirmation

logger = logging.getLogger(__name__)


def remove_smartzone(
    connection: ConnectHandler,
    manager_ip: str,
    progress_callback: Optional[Callable] = None,
) -> dict:
    """
    Executa a sequência de remoção do SmartZone no switch:
      conf t → no manager active-list <ip> → manager disconnect →
      manager disable → no manager disable → end

    'manager disable' desconecta o switch do orquestrador atual.
    'no manager disable' reativa o client de manager com a lista limpa,
    permitindo que o switch descubra o Ruckus One.

    Nota: 'manager disconnect' pode encerrar a sessão SSH se o caminho
    de gerência era via SmartZone. Nesse caso 'no manager disable' não
    será executado nesta sessão — o campo 'manager_reenabled' do retorno
    indica isso para que o caller tome providências.

    Retorna dict: {"success": True, "manager_reenabled": bool}
    Lança SmartZoneRemovalError em falhas críticas.
    """
    commands = [
        "conf t",
        f"no manager active-list {manager_ip}",
        "manager disconnect",
        "manager disable",
        "no manager disable",
        "end",
    ]

    full_output = ""
    manager_reenabled = False

    for command in commands:
        logger.info(f"Executando: {command}")
        if progress_callback:
            progress_callback(f"Executando: {command}")

        try:
            output = send_with_confirmation(
                connection,
                command,
                {},
                timeout=15,
            )
            full_output += f"\n[{command}]\n{output}"
            logger.debug(f"Output de '{command}':\n{output[:300]}")

            if command == "no manager disable":
                manager_reenabled = True

            # Se a sessão foi encerrada durante 'manager disconnect', é esperado
            if "[SSH session terminated" in output and command == "manager disconnect":
                logger.warning(
                    "Sessão SSH encerrada após 'manager disconnect'. "
                    "'no manager disable' não foi executado — o switch não iniciará "
                    "a descoberta do Ruckus One automaticamente. "
                    "Execute manualmente: conf t; no manager disable; end"
                )
                full_output += "\n[Sessão encerrada após manager disconnect — no manager disable pendente]"
                break

        except Exception as exc:
            raise SmartZoneRemovalError(
                f"Falha ao executar '{command}': {exc}\n"
                f"Output acumulado:\n{full_output}"
            ) from exc

    logger.info(
        f"Sequência de remoção do SmartZone concluída. manager_reenabled={manager_reenabled}"
    )
    return {"success": True, "manager_reenabled": manager_reenabled}
