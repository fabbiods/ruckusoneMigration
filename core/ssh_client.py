import socket
import logging
from typing import Optional

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException

from core import SSHConnectionError

logger = logging.getLogger(__name__)


def create_connection(
    ip: str,
    username: str,
    password: str,
    enable_password: str = "",
    timeout: int = 30,
) -> ConnectHandler:
    """
    Abre sessão netmiko para dispositivo Ruckus FastIron (ICX).
    Lança SSHConnectionError em qualquer falha de conectividade ou autenticação.
    """
    device_params = {
        "device_type": "ruckus_fastiron",
        "host": ip,
        "username": username,
        "password": password,
        "timeout": timeout,
        "global_delay_factor": 2,
        "session_log": None,
    }
    if enable_password:
        device_params["secret"] = enable_password

    try:
        logger.info(f"Abrindo SSH para {ip} (user={username}, timeout={timeout}s) ...")
        conn = ConnectHandler(**device_params)
        logger.info(f"Conexão SSH estabelecida com {ip}.")
        return conn
    except NetmikoTimeoutException as exc:
        raise SSHConnectionError(f"Timeout ao conectar em {ip}: {exc}") from exc
    except NetmikoAuthenticationException as exc:
        raise SSHConnectionError(f"Falha de autenticação em {ip}: {exc}") from exc
    except (OSError, socket.error) as exc:
        raise SSHConnectionError(f"Erro de rede ao conectar em {ip}: {exc}") from exc
    except Exception as exc:
        raise SSHConnectionError(f"Erro inesperado ao conectar em {ip}: {exc}") from exc


def run_command(
    connection: ConnectHandler,
    command: str,
    expect_string: Optional[str] = None,
    timeout: int = 30,
) -> str:
    """
    Executa um comando e retorna a saída completa.
    Usa expect_string quando há um prompt específico a aguardar.
    """
    try:
        kwargs = {
            "command_string": command,
            "read_timeout": timeout,
            "strip_prompt": False,
            "strip_command": False,
        }
        if expect_string:
            kwargs["expect_string"] = expect_string

        output = connection.send_command(**kwargs)
        logger.debug(f"CMD: {command!r} → {len(output)} bytes")
        return output
    except Exception as exc:
        raise SSHConnectionError(f"Erro ao executar '{command}': {exc}") from exc


def run_command_timing(
    connection: ConnectHandler,
    command: str,
    read_timeout: int = 600,
) -> str:
    """
    Executa comando com timing — ideal para TFTP onde a duração é imprevisível.
    read_timeout: tempo máximo em segundos aguardando a resposta (Netmiko 4.x).
    """
    try:
        output = connection.send_command_timing(
            command_string=command,
            read_timeout=read_timeout,
            strip_prompt=False,
            strip_command=False,
        )
        logger.debug(f"CMD (timing): {command!r} → {len(output)} bytes")
        return output
    except Exception as exc:
        raise SSHConnectionError(f"Erro ao executar '{command}': {exc}") from exc


def send_with_confirmation(
    connection: ConnectHandler,
    command: str,
    confirmation_map: dict,
    timeout: int = 60,
) -> str:
    """
    Envia um comando e responde automaticamente a prompts de confirmação.
    confirmation_map: {substring_do_prompt: resposta_a_enviar}
    Captura EOFError (sessão encerrada pelo device) como situação não-fatal.
    """
    full_output = ""
    try:
        output = connection.send_command_timing(
            command_string=command,
            delay_factor=2,
            strip_prompt=False,
            strip_command=False,
        )
        full_output += output

        for prompt_key, response in confirmation_map.items():
            if prompt_key.lower() in output.lower():
                logger.debug(f"Prompt '{prompt_key}' detectado → respondendo '{response}'")
                output = connection.send_command_timing(
                    command_string=response,
                    delay_factor=2,
                    strip_prompt=False,
                    strip_command=False,
                )
                full_output += output

    except EOFError:
        logger.info("Sessão SSH encerrada pelo device (esperado em reload ou manager disconnect).")
        full_output += "\n[SSH session terminated by device — expected]"
    except Exception as exc:
        logger.warning(f"Exceção em send_with_confirmation para '{command}': {exc}")
        full_output += f"\n[Exception: {exc}]"

    return full_output


def close_connection(connection: ConnectHandler) -> None:
    """Encerra graciosamente a sessão SSH."""
    try:
        connection.disconnect()
        logger.info("Conexão SSH encerrada.")
    except Exception as exc:
        logger.warning(f"Erro ao encerrar conexão SSH: {exc}")
