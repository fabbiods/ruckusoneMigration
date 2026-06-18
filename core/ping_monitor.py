import subprocess
import platform
import time
import logging
from typing import Callable, Optional

from core import PingTimeoutError

logger = logging.getLogger(__name__)


def ping_once(ip: str, timeout: int = 2) -> bool:
    """
    Executa um único ping ICMP via subprocess.
    Compatível com macOS, Linux e Windows.
    Retorna True se o host respondeu (exit code 0).
    """
    system = platform.system().lower()

    if system == "windows":
        # -n 1: um pacote; -w: timeout em milissegundos
        cmd = ["ping", "-n", "1", "-w", str(timeout * 1000), ip]
    else:
        # macOS/Linux: -c 1 pacote; -W timeout em segundos
        # CUSTOMIZE: se seu sistema usar flags diferentes, ajuste aqui
        cmd = ["ping", "-c", "1", "-W", str(timeout), ip]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 3,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception as exc:
        logger.warning(f"Erro ao executar ping para {ip}: {exc}")
        return False


def wait_for_reboot_down(
    ip: str,
    timeout: int = 120,
    interval: int = 3,
    progress_callback: Optional[Callable] = None,
) -> bool:
    """
    Confirma que o device REALMENTE caiu após o comando de reload.
    Pinga até o device parar de responder (confirma que o reboot começou).
    Retorna True se caiu; False se continuou respondendo o tempo todo
    (indício de que o reload NÃO foi aceito).
    """
    msg = f"Confirmando que o device {ip} caiu (aguardando reboot iniciar) ..."
    logger.info(msg)
    if progress_callback:
        progress_callback(msg)

    elapsed = 0
    while elapsed < timeout:
        if not ping_once(ip):
            down_msg = f"Device {ip} parou de responder após ~{elapsed}s — reboot confirmado."
            logger.info(down_msg)
            if progress_callback:
                progress_callback(f"Device {ip} caiu (OFFLINE) — reboot em andamento.")
            return True
        time.sleep(interval)
        elapsed += interval

    warn_msg = (
        f"Device {ip} continuou respondendo por {timeout}s — o reload pode NÃO ter sido aceito."
    )
    logger.warning(warn_msg)
    if progress_callback:
        progress_callback(warn_msg)
    return False


def wait_for_device(
    ip: str,
    total_timeout: int = 300,
    interval: int = 10,
    initial_delay: int = 60,
    progress_callback: Optional[Callable] = None,
    confirm_down: bool = True,
    down_timeout: int = 120,
) -> bool:
    """
    Aguarda o device voltar à rede após reboot.

    Se confirm_down=True (padrão), primeiro confirma que o device CAIU
    (deixou de responder ao ping) — garantindo que o reboot realmente
    aconteceu — antes de aguardar o retorno. Se o device nunca cair,
    lança PingTimeoutError (sinal de que o reload não foi aceito).

    Depois pinga a cada interval segundos até total_timeout ser atingido,
    aguardando o device voltar à rede.

    Lança PingTimeoutError se o device não cair (quando confirm_down) ou
    não voltar dentro do limite.
    """
    # Fase 1: confirmar que o device caiu (reboot de fato iniciou)
    if confirm_down:
        if not wait_for_reboot_down(ip, timeout=down_timeout, interval=3,
                                    progress_callback=progress_callback):
            raise PingTimeoutError(
                f"Device {ip} nunca parou de responder ao ping em {down_timeout}s. "
                "O comando de reload provavelmente NÃO foi aceito pelo switch."
            )

    # Fase 2: aguardar o boot inicial antes de buscar o retorno
    msg = f"Aguardando {initial_delay}s para o device concluir o boot ..."
    logger.info(msg)
    if progress_callback:
        progress_callback(msg)

    time.sleep(initial_delay)

    elapsed = 0
    attempt = 0

    while elapsed < total_timeout:
        attempt += 1
        msg = f"Ping #{attempt} → {ip} (elapsed={elapsed}s / max={total_timeout}s)"
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

        if ping_once(ip):
            success_msg = f"Device {ip} respondeu ao ping após ~{elapsed + initial_delay}s."
            logger.info(success_msg)
            if progress_callback:
                progress_callback(f"Device {ip} está ONLINE novamente.")
            return True

        time.sleep(interval)
        elapsed += interval

    raise PingTimeoutError(
        f"Device {ip} não respondeu ao ping em {total_timeout + initial_delay}s. "
        "Verifique o status do equipamento manualmente."
    )
