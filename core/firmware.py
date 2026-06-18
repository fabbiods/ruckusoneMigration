import re
import time
import logging
from typing import Callable, Optional

from netmiko import ConnectHandler

from core import FirmwareParseError, FirmwareUpgradeError
from core.ssh_client import run_command, run_command_timing, send_with_confirmation

logger = logging.getLogger(__name__)

# Extrai versão da partição primária da saída de "sh flash".
# Formato real do ICX: "Compressed Pri Code size = 33554432, Version:08.0.95kT213 (SPR08095k.bin)"
_FLASH_VERSION_RE = re.compile(
    r"Compressed\s+Pri\s+Code\s+size\s*=\s*\d+,\s*Version:(\S+)\s+\(",
    re.IGNORECASE,
)
# Extrai o nome do arquivo da partição primária: "... (SPR09010kufi.bin)"
_FLASH_FILENAME_RE = re.compile(
    r"Compressed\s+Pri\s+Code\s+size\s*=\s*\d+,\s*Version:\S+\s+\((\S+)\)",
    re.IGNORECASE,
)
# Extrai o arquivo em execução da saída de "show version": "from Primary SPR08095k.bin"
_RUNNING_FILE_RE = re.compile(
    r"from\s+Primary\s+(\S+)",
    re.IGNORECASE,
)
# Separa parte numérica do sufixo alfanumérico no patch (ex: "10kT213" → 10, "kT213")
_PATCH_SPLIT_RE = re.compile(r"^(\d+)([a-zA-Z].*)?$")

# Prompt de confirmação do "reload" no Ruckus ICX
_RELOAD_CONFIRMATIONS = {
    "are you sure": "y",
}


def _normalize_version(version_str: str) -> tuple:
    """
    Converte string de versão Ruckus em tupla comparável.
    Ex: "09.0.10kT213" → (9, 0, 10, "kT213")
    Ex: "08.0.95"      → (8, 0, 95, "")
    """
    parts = version_str.strip().split(".")
    if len(parts) < 3:
        raise FirmwareParseError(f"Formato de versão inválido: '{version_str}'")

    match = _PATCH_SPLIT_RE.match(parts[2])
    if not match:
        raise FirmwareParseError(f"Patch inválido na versão: '{parts[2]}'")

    return (int(parts[0]), int(parts[1]), int(match.group(1)), match.group(2) or "")


def parse_version(raw_output: str) -> str:
    """
    Extrai a string de versão da saída de 'sh flash'.
    Lança FirmwareParseError se o padrão não for encontrado.
    """
    match = _FLASH_VERSION_RE.search(raw_output)
    if not match:
        raise FirmwareParseError(
            "Não foi possível extrair a versão do firmware da saída de 'sh flash'.\n"
            f"Output recebido (primeiros 500 chars):\n{raw_output[:500]}"
        )
    return match.group(1)


def get_firmware_version(connection: ConnectHandler) -> str:
    """Executa 'sh flash' e retorna a versão atual do firmware (partição primária)."""
    logger.info("Executando 'sh flash' para obter versão do firmware ...")
    output = run_command(connection, "sh flash", timeout=30)
    version = parse_version(output)
    logger.info(f"Versão do firmware detectada: {version}")
    return version


def is_upgrade_required(current_version: str, minimum_version: str) -> bool:
    """
    Retorna True se current_version < minimum_version.
    Comparação por tupla normalizada suporta o formato alfanumérico Ruckus.
    """
    current = _normalize_version(current_version)
    minimum = _normalize_version(minimum_version)
    required = current < minimum
    logger.info(
        f"Versão atual={current_version} mínima={minimum_version} → "
        f"upgrade {'NECESSÁRIO' if required else 'NÃO necessário'}"
    )
    return required


def run_firmware_upgrade(
    connection: ConnectHandler,
    tftp_ip: str,
    filename: str,
    partition: str,
    progress_callback: Optional[Callable] = None,
    timeout: int = 600,
) -> bool:
    """
    Executa upgrade de firmware via TFTP.
    Monitora a saída por indicadores de sucesso ou falha.
    Lança FirmwareUpgradeError se a transferência falhar.

    CUSTOMIZE: aumente delay_factor se o link TFTP for lento (padrão: 20).
    CUSTOMIZE: aumente max_loops se o timeout de 600s não for suficiente.
    """
    cmd = f"copy tftp flash {tftp_ip} {filename} {partition}"
    logger.info(f"Iniciando TFTP: {cmd}")

    if progress_callback:
        progress_callback(f"Executando: {cmd}")

    output = run_command_timing(
        connection,
        cmd,
        read_timeout=timeout,
    )

    if progress_callback:
        # Exibir últimos 300 chars para não sobrecarregar o log
        progress_callback(output[-300:] if len(output) > 300 else output)

    output_lower = output.lower()
    error_indicators = ["error", "failed", "timed out", "cannot", "no route", "refused"]
    success_indicators = ["copy done", "flash memory write", "bytes transferred", "done"]

    if any(ind in output_lower for ind in error_indicators):
        raise FirmwareUpgradeError(
            f"Erro detectado na transferência TFTP:\n{output[-800:]}"
        )

    if not any(ind in output_lower for ind in success_indicators):
        logger.warning(
            "Nenhum indicador de sucesso encontrado no output do TFTP. "
            "Verifique o log completo manualmente."
        )

    logger.info("Transferência de firmware via TFTP concluída.")
    return True


def verify_firmware_on_flash(connection: ConnectHandler, old_version: str) -> tuple:
    """
    Roda 'sh flash' e verifica se a versão na partição primária mudou.
    Retorna (changed: bool, new_version: str).
    """
    try:
        new_version = get_firmware_version(connection)
        logger.info(f"Verificação pós-TFTP: partição primária = {new_version} (antes = {old_version})")
        return new_version != old_version, new_version
    except FirmwareParseError as exc:
        logger.warning(f"Não foi possível ler versão da partição após TFTP: {exc}")
        return False, ""


def get_flash_filename(connection: ConnectHandler) -> str:
    """Retorna o nome do arquivo de firmware gravado na partição primária (sh flash)."""
    output = run_command(connection, "sh flash", timeout=30)
    logger.debug(f"[sh flash] output bruto:\n{output}")
    match = _FLASH_FILENAME_RE.search(output)
    if not match:
        raise FirmwareParseError(
            f"Não foi possível extrair filename da partição primária.\nOutput: {output[:500]}"
        )
    return match.group(1)


def get_running_firmware_file(connection: ConnectHandler) -> str:
    """Retorna o nome do arquivo de firmware em execução (show version → 'from Primary XXX.bin')."""
    output = run_command(connection, "show version", timeout=30)
    logger.debug(f"[show version] output bruto:\n{output}")
    match = _RUNNING_FILE_RE.search(output)
    if not match:
        raise FirmwareParseError(
            f"Não foi possível extrair 'from Primary' de 'show version'.\nOutput: {output[:500]}"
        )
    return match.group(1)


def double_check_firmware(connection: ConnectHandler) -> tuple:
    """
    Compara o arquivo na partição primária (sh flash) com o em execução (show version).
    Retorna (flash_file: str, running_file: str, match: bool).
    """
    flash_file   = get_flash_filename(connection)
    running_file = get_running_firmware_file(connection)
    match        = flash_file == running_file
    logger.info(
        f"Double-check: flash={flash_file} | running={running_file} | "
        f"{'OK' if match else 'MISMATCH'}"
    )
    return flash_file, running_file, match


# ── Regex para coleta de info do device via show version / running-config ──
_SERIAL_RE  = re.compile(r"Serial\s+#:\s*(\S+)", re.IGNORECASE)
_MAC_RE     = re.compile(r"MAC\s+Address:\s*([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})", re.IGNORECASE)
_MODEL_RE   = re.compile(r"System-Model:\s*(.+)", re.IGNORECASE)
_HOSTNAME_RE = re.compile(r"^hostname\s+(\S+)", re.IGNORECASE | re.MULTILINE)


def _clean_prompt_hostname(prompt: str) -> str:
    """
    Extrai o hostname puro do prompt do ICX.
    Ex: "SSH@BRLABSHP001-1#"        → "BRLABSHP001-1"
        "SSH@BRLABSHP001-1(config)#" → "BRLABSHP001-1"
        "telnet@SW-LAB-01>"          → "SW-LAB-01"
    Remove o prefixo de transporte/usuário ("SSH@", "telnet@", "user@")
    e os sufixos de modo (#, >, (config), espaços).
    """
    cleaned = prompt.strip()
    # Remove prefixo "algo@" (SSH@, telnet@, usuário@)
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[1]
    # Remove sufixos de modo: #, >, (config)#, espaços, etc.
    cleaned = re.sub(r"[\s>#(].*$", "", cleaned).strip()
    return cleaned


def collect_device_info(connection: ConnectHandler) -> dict:
    """
    Coleta hostname, serial, MAC e modelo do switch via CLI.
    Hostname vem do base_prompt do Netmiko (mais confiável no ICX).
    Serial, MAC e modelo vêm do 'show version'.
    """
    info = {"hostname": None, "serial": None, "mac": None, "modelo": None}

    # Hostname: Netmiko já captura o prompt (ex: "SSH@BRLABSHP001-1#") ao conectar
    try:
        prompt = connection.find_prompt()
        hostname = _clean_prompt_hostname(prompt)
        if hostname:
            info["hostname"] = hostname
            logger.info(f"Hostname extraído do prompt SSH: '{prompt}' → '{hostname}'")
    except Exception as exc:
        logger.warning(f"Falha ao extrair hostname do prompt: {exc}")

    try:
        ver_output = run_command(connection, "show version", timeout=30)
        logger.debug(f"[collect_device_info] show version:\n{ver_output}")

        m = _SERIAL_RE.search(ver_output)
        if m:
            info["serial"] = m.group(1)

        m = _MAC_RE.search(ver_output)
        if m:
            info["mac"] = m.group(1)

        m = _MODEL_RE.search(ver_output)
        if m:
            info["modelo"] = m.group(1).strip()

        # Fallback para hostname via running-config se prompt falhou
        if not info["hostname"]:
            cfg_output = run_command(connection, "show running-config", timeout=30)
            m = _HOSTNAME_RE.search(cfg_output)
            if m:
                info["hostname"] = m.group(1)
                logger.info(f"Hostname extraído do running-config: '{info['hostname']}'")
    except Exception as exc:
        logger.warning(f"Falha ao executar 'show version' para coleta de info: {exc}")

    logger.info(
        f"Device info via CLI: hostname={info['hostname']} serial={info['serial']} "
        f"mac={info['mac']} modelo={info['modelo']}"
    )
    return info


def trigger_reload(connection: ConnectHandler, progress_callback: Optional[Callable] = None) -> None:
    """
    Salva a configuração (write memory) e reinicia o switch (reload → y).
    Usa write_channel/read_channel cru para evitar newlines extras que o ICX
    interpreta como entrada inválida no prompt de confirmação.
    A sessão SSH será encerrada pelo device após o reload — isso é esperado.
    """
    def _log(msg: str) -> None:
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    # 1. write memory
    _log(">>> [reload 1/3] Enviando: write memory")
    try:
        wm_output = run_command(connection, "write memory", timeout=30)
        _log(f"<<< write memory respondeu: {wm_output.strip()[:200]!r}")
    except Exception as exc:
        _log(f"!!! write memory retornou exceção (continuando): {exc}")

    # 2. reload — envia o comando e lê o prompt de confirmação
    _log(">>> [reload 2/3] Enviando: reload")
    try:
        connection.write_channel("reload\n")
        time.sleep(3)
        prompt_output = connection.read_channel()
        _log(f"<<< Prompt após reload: {prompt_output.strip()[:300]!r}")

        # 3. confirma com 'y' cru (sem newline extra) — ICX lê char único
        if "are you sure" in prompt_output.lower() or "enter 'y'" in prompt_output.lower():
            _log(">>> [reload 3/3] Prompt de confirmação detectado — enviando: y")
        else:
            _log(">>> [reload 3/3] Prompt não detectado claramente — enviando 'y' mesmo assim")
        connection.write_channel("y")
        time.sleep(2)
        try:
            confirm_output = connection.read_channel()
            _log(f"<<< Resposta à confirmação: {confirm_output.strip()[:300]!r}")
            # Se o ICX ainda pedir confirmação, manda Enter como reforço
            if "are you sure" in confirm_output.lower() or "enter y/n" in confirm_output.lower():
                _log("!!! Switch ainda no prompt — reforçando com Enter")
                connection.write_channel("\n")
                time.sleep(2)
                extra = connection.read_channel()
                _log(f"<<< Resposta ao reforço: {extra.strip()[:300]!r}")
        except EOFError:
            _log("<<< Sessão encerrada após 'y' — reboot iniciado (esperado).")

    except EOFError:
        _log("<<< Sessão SSH encerrada pelo device após reload — reboot iniciado (esperado).")
    except Exception as exc:
        _log(f"!!! Exceção durante reload (pode ser esperado se a sessão caiu): {exc}")
