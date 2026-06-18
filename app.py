import threading
import queue
import json
import time
import logging
import os
from datetime import datetime
from typing import Optional

from flask import Flask, render_template, request, jsonify, Response, stream_with_context

import config
from core import (
    SSHConnectionError,
    FirmwareParseError,
    FirmwareUpgradeError,
    PingTimeoutError,
    SmartZoneRemovalError,
    SmartZoneAPIError,
    GridAPIError,
    RuckusOneAPIError,
)
from core.ssh_client import create_connection, close_connection
from core.firmware import (
    get_firmware_version,
    is_upgrade_required,
    run_firmware_upgrade,
    verify_firmware_on_flash,
    double_check_firmware,
    trigger_reload,
    collect_device_info,
)
from core.ping_monitor import wait_for_device
from core.smartzone import remove_smartzone
from core.smartzone_api import remove_switch_from_smartzone
from core.ruckus_one_api import (
    get_access_token,
    find_venue_by_slug,
    assign_switch_to_venue,
    get_site_slug_from_netbox,
)
from core.grid_api import append_migration_record

app = Flask(__name__)

# ── Etapas do pipeline ────────────────────────────────────────────────────────

_STEP_IDS = [
    "ssh_connect",
    "firmware_check",
    "firmware_upgrade",
    "device_reboot",
    "ping_wait",
    "ssh_reconnect",
    "smartzone_cli",     # remoção via CLI
    "smartzone_api",     # deleção via REST API
    "ruckus_one_venue",  # associação à venue no Ruckus One
    "grid_update",       # atualização do dashboard Grid
]

# ── Estado global ─────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_running: bool = False
_steps: dict = {sid: "pending" for sid in _STEP_IDS}
_status_text: str = "Idle"
_status_color: str = "gray"

# Dados coletados durante a migração para o dashboard Grid
_migration_data: dict = {}

# ── SSE subscribers ───────────────────────────────────────────────────────────

_subscribers: list = []
_sub_lock = threading.Lock()

# ── Confirmação interativa de upgrade ─────────────────────────────────────────

_upgrade_event = threading.Event()
_upgrade_confirmed: Optional[bool] = None

# ── Pausa para criação de venue no Ruckus One ─────────────────────────────────

_venue_event = threading.Event()
_venue_created: bool = False

# ── Pausa para coleta do nome da venue ────────────────────────────────────────

_venue_name_event = threading.Event()
_venue_name_value: str = ""

# ── Logging em arquivo ────────────────────────────────────────────────────────

_app_logger = logging.getLogger("MigrationApp")


def _setup_file_logging() -> str:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(config.LOG_DIR, f"migration_{ts}.log")
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    return log_path


_setup_file_logging()

# ── Pub/sub helpers ───────────────────────────────────────────────────────────


def _broadcast(payload: str) -> None:
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def _push(event_type: str, **data) -> None:
    _broadcast(json.dumps({"type": event_type, **data}))


def _emit_log(level: str, message: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    _app_logger.log(getattr(logging, level.upper(), logging.INFO), message)
    _push("log", level=level.upper(), message=message, timestamp=ts)


def _emit_step(step_id: str, status: str) -> None:
    global _steps
    with _state_lock:
        _steps[step_id] = status
    _push("step", step_id=step_id, status=status)


def _emit_status(text: str, color: str = "gray") -> None:
    global _status_text, _status_color
    with _state_lock:
        _status_text = text
        _status_color = color
    _push("status", text=text, color=color)


# ── Rotas ─────────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/state")
def get_state():
    with _state_lock:
        return jsonify({
            "running": _running,
            "steps": dict(_steps),
            "status_text": _status_text,
            "status_color": _status_color,
        })


@app.route("/stream")
def stream():
    client_q: queue.Queue = queue.Queue(maxsize=300)

    with _state_lock:
        initial = json.dumps({
            "type": "state",
            "running": _running,
            "steps": dict(_steps),
            "status_text": _status_text,
            "status_color": _status_color,
        })

    with _sub_lock:
        _subscribers.append(client_q)

    def generate():
        try:
            yield f"data: {initial}\n\n"
            while True:
                try:
                    event = client_q.get(timeout=15)
                    yield f"data: {event}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sub_lock:
                try:
                    _subscribers.remove(client_q)
                except ValueError:
                    pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/start", methods=["POST"])
def start():
    global _running
    data = request.json or {}
    ip              = (data.get("ip")              or "").strip()
    username        = (data.get("username")        or config.SWITCH_USERNAME).strip()
    password        = (data.get("password")        or "").strip()
    analista        = (data.get("analista")        or "").strip()
    controle_cambio = (data.get("controle_cambio") or "").strip()

    if not ip:
        return jsonify({"ok": False, "error": "IP do switch não informado."}), 400
    if not username:
        return jsonify({"ok": False, "error": "Usuário não informado."}), 400
    if not password:
        return jsonify({"ok": False, "error": "Senha não informada."}), 400
    if not analista:
        return jsonify({"ok": False, "error": "Analista não informado."}), 400
    if not controle_cambio:
        return jsonify({"ok": False, "error": "Controle de câmbio não informado."}), 400

    with _state_lock:
        if _running:
            return jsonify({"ok": False, "error": "Migração já em andamento."}), 409
        _running = True

    threading.Thread(
        target=_run_pipeline, args=(ip, username, password, analista, controle_cambio), daemon=True
    ).start()
    return jsonify({"ok": True})


@app.route("/confirm-upgrade", methods=["POST"])
def confirm_upgrade():
    global _upgrade_confirmed
    data = request.json or {}
    _upgrade_confirmed = bool(data.get("confirmed", False))
    _upgrade_event.set()
    return jsonify({"ok": True})


@app.route("/set-venue-name", methods=["POST"])
def set_venue_name():
    """Analista informa o nome da venue que irá criar/já criou no Ruckus One."""
    global _venue_name_value
    data = request.json or {}
    _venue_name_value = (data.get("venue_name") or "").strip()
    _venue_name_event.set()
    return jsonify({"ok": True})


@app.route("/venue-created", methods=["POST"])
def venue_created():
    """Analista confirma que a venue foi criada manualmente no Ruckus One."""
    global _venue_created
    _venue_created = True
    _venue_event.set()
    return jsonify({"ok": True})


@app.route("/reset", methods=["POST"])
def reset():
    global _running, _steps, _status_text, _status_color, _migration_data
    with _state_lock:
        if _running:
            return jsonify({"ok": False, "error": "Não é possível resetar durante a migração."}), 409
        _steps = {sid: "pending" for sid in _STEP_IDS}
        _status_text = "Idle"
        _status_color = "gray"
        _migration_data = {}
    _push("state",
          running=False,
          steps=dict(_steps),
          status_text=_status_text,
          status_color=_status_color)
    return jsonify({"ok": True})



# ── Pipeline de migração ───────────────────────────────────────────────────────


def _run_pipeline(ip: str, username: str, password: str, analista: str, controle_cambio: str) -> None:
    global _running, _upgrade_confirmed, _migration_data
    connection = None

    # Inicializa dados de migração para este run
    _migration_data = {
        "data_hora":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ipv4":             ip,
        "analista":         analista,
        "controle_cambio":  controle_cambio,
        "firmware_antes": None,
        "upgrade":        None,
        "sz_cli":         None,
        "sz_api":         None,
        "apagado_sz":     None,
        "hostname":       None,
        "mac":            None,
        "serial":         None,
        "modelo":         None,
        "site":           None,
        "status_final":   None,
        "obs":            "",
    }

    try:
        _emit_log("INFO", "─" * 52)
        _emit_log("INFO", f"  Iniciando migração do switch {ip} (user: {username})")
        _emit_log("INFO", "─" * 52)

        # ── Etapa 1: SSH Connect ──────────────────────────────────────────────
        _emit_step("ssh_connect", "running")
        _emit_status("Conectando via SSH ...", "blue")
        _emit_log("INFO", f"[Etapa 1] Conectando via SSH em {ip} ...")
        try:
            connection = create_connection(
                ip, username, password, timeout=config.SSH_TIMEOUT
            )
            _emit_step("ssh_connect", "success")
            _emit_log("SUCCESS", f"Conexão SSH estabelecida com {ip}")

            # Coleta hostname, serial, MAC e modelo via CLI logo após conexão
            _emit_log("INFO", "Coletando informações do device via CLI (show version / running-config) ...")
            cli_info = collect_device_info(connection)
            _migration_data.update({k: v for k, v in cli_info.items() if v is not None})
            _emit_log("INFO",
                f"CLI info: hostname={cli_info['hostname']} serial={cli_info['serial']} "
                f"mac={cli_info['mac']} modelo={cli_info['modelo']}")
        except SSHConnectionError as exc:
            _emit_step("ssh_connect", "error")
            _emit_log("ERROR", str(exc))
            _emit_status("Falha na conexão SSH", "red")
            _migration_data["status_final"] = "Falha"
            return

        # ── Etapa 2: Firmware Check ───────────────────────────────────────────
        _emit_step("firmware_check", "running")
        _emit_status("Verificando firmware ...", "blue")
        _emit_log("INFO", "[Etapa 2] Verificando versão do firmware ...")
        try:
            current_version = get_firmware_version(connection)
            _migration_data["firmware_antes"] = current_version

            # Double-check: sh flash vs show version
            _emit_log("INFO", "Double-check: comparando sh flash com show version ...")
            try:
                flash_file, running_file, fw_match = double_check_firmware(connection)
                if fw_match:
                    _emit_log("SUCCESS",
                        f"Double-check OK: partição e execução coincidem ({running_file})")
                else:
                    _emit_log("WARNING",
                        f"Mismatch: partição={flash_file} | execução={running_file}. "
                        "Rebootando para alinhar firmware ...")
                    _emit_status("Reboot de alinhamento de firmware ...", "blue")
                    try:
                        trigger_reload(connection, progress_callback=lambda m: _emit_log("INFO", m))
                    except Exception:
                        pass
                    finally:
                        close_connection(connection)
                        connection = None

                    # Aguarda ICMP
                    try:
                        wait_for_device(
                            ip=ip,
                            total_timeout=config.PING_TIMEOUT,
                            interval=config.PING_INTERVAL,
                            initial_delay=config.RELOAD_WAIT_INITIAL,
                            progress_callback=lambda msg: _emit_log("INFO", msg),
                        )
                        _emit_log("SUCCESS", f"Device {ip} voltou após reboot de alinhamento.")
                    except PingTimeoutError as exc:
                        _emit_step("firmware_check", "error")
                        _emit_log("ERROR", f"Timeout aguardando device: {exc}")
                        _migration_data["status_final"] = "Falha"
                        return

                    # Reconecta SSH
                    for attempt in range(1, config.SSH_RECONNECT_RETRIES + 1):
                        try:
                            time.sleep(config.SSH_RECONNECT_DELAY)
                            connection = create_connection(
                                ip, username, password, timeout=config.SSH_TIMEOUT
                            )
                            _emit_log("SUCCESS",
                                f"Reconectado após reboot de alinhamento (tentativa {attempt})")
                            break
                        except SSHConnectionError as exc:
                            _emit_log("WARNING", f"Tentativa {attempt} falhou: {exc}")
                            if attempt == config.SSH_RECONNECT_RETRIES:
                                _emit_step("firmware_check", "error")
                                _emit_log("ERROR", "Não foi possível reconectar após reboot.")
                                _migration_data["status_final"] = "Falha"
                                return

                    # Re-lê versão após reboot
                    current_version = get_firmware_version(connection)
                    _migration_data["firmware_antes"] = current_version
                    _emit_log("SUCCESS", f"Versão pós-reboot: {current_version}")

            except Exception as exc:
                _emit_log("WARNING", f"Double-check falhou: {exc}. Continuando com sh flash.")

            needs_upgrade = is_upgrade_required(current_version, config.MINIMUM_FIRMWARE)
            _emit_step("firmware_check", "success")
            _emit_log(
                "SUCCESS",
                f"Firmware: atual={current_version} | mínimo={config.MINIMUM_FIRMWARE} | "
                f"upgrade={'SIM' if needs_upgrade else 'NÃO'}",
            )
        except (FirmwareParseError, Exception) as exc:
            _emit_step("firmware_check", "error")
            _emit_log("ERROR", f"Falha ao verificar firmware: {exc}")
            _emit_status("Falha na verificação de firmware", "red")
            _migration_data["status_final"] = "Falha"
            return

        warnings = []

        # ── Etapas 3–6: condicionais ao upgrade ──────────────────────────────
        if needs_upgrade:
            # Etapa 3: confirmação + upgrade
            _emit_log("INFO", "[Etapa 3] Aguardando confirmação do usuário para upgrade ...")
            _upgrade_confirmed = None
            _upgrade_event.clear()
            _push("upgrade_required",
                  current_version=current_version,
                  minimum_version=config.MINIMUM_FIRMWARE)
            _upgrade_event.wait(timeout=300)

            if not _upgrade_confirmed:
                for sid in ["firmware_upgrade", "device_reboot", "ping_wait", "ssh_reconnect"]:
                    _emit_step(sid, "skipped")
                _migration_data["upgrade"] = "Não"
                _emit_log("WARNING", "Upgrade cancelado pelo usuário. Pipeline interrompido.")
                _emit_status("Cancelado pelo usuário", "yellow")
                _migration_data["status_final"] = "Falha"
                return

            _emit_step("firmware_upgrade", "running")
            _emit_status("Transferindo firmware via TFTP ...", "blue")
            _emit_log("INFO",
                f"[Etapa 3] TFTP: {config.TFTP_SERVER_IP} → {config.FIRMWARE_FILENAME} ...")
            tftp_completed = False
            try:
                run_firmware_upgrade(
                    connection,
                    tftp_ip=config.TFTP_SERVER_IP,
                    filename=config.FIRMWARE_FILENAME,
                    partition=config.FIRMWARE_FLASH_PARTITION,
                    progress_callback=lambda msg: _emit_log("INFO", msg),
                    timeout=config.TFTP_COPY_TIMEOUT,
                )
                tftp_completed = True
            except FirmwareUpgradeError as exc:
                _emit_step("firmware_upgrade", "error")
                _emit_log("ERROR", str(exc))
                _emit_status("Falha no upgrade de firmware", "red")
                _migration_data["upgrade"]      = "Sim"
                _migration_data["status_final"] = "Falha"
                return
            except SSHConnectionError as exc:
                # Conexão pode cair durante TFTP — reconectar e verificar partição
                _emit_log("WARNING", f"Conexão SSH caiu durante TFTP: {exc}")
                _emit_log("INFO", "Reconectando para verificar se o firmware foi gravado ...")
                connection = None
                for attempt in range(1, config.SSH_RECONNECT_RETRIES + 1):
                    try:
                        time.sleep(config.SSH_RECONNECT_DELAY)
                        connection = create_connection(
                            ip, username, password, timeout=config.SSH_TIMEOUT
                        )
                        _emit_log("SUCCESS",
                            f"Reconectado (tentativa {attempt}/{config.SSH_RECONNECT_RETRIES})")
                        break
                    except SSHConnectionError as reconnect_exc:
                        _emit_log("WARNING", f"Tentativa {attempt} falhou: {reconnect_exc}")
                if not connection:
                    _emit_step("firmware_upgrade", "error")
                    _emit_log("ERROR", "Não foi possível reconectar após queda durante TFTP.")
                    _migration_data["upgrade"]      = "Sim"
                    _migration_data["status_final"] = "Falha"
                    return

            # Verifica se o firmware foi gravado na partição primária
            _emit_log("INFO", "Verificando firmware gravado na partição primária ...")
            try:
                changed, new_version = verify_firmware_on_flash(connection, current_version)
                if changed:
                    _migration_data["upgrade"] = "Sim"
                    _emit_step("firmware_upgrade", "success")
                    _emit_log("SUCCESS",
                        f"Firmware gravado na partição: {new_version} (anterior: {current_version})")
                else:
                    _emit_step("firmware_upgrade", "error")
                    _emit_log("ERROR",
                        f"Partição primária ainda contém {current_version} — TFTP não concluiu.")
                    _migration_data["upgrade"]      = "Sim"
                    _migration_data["status_final"] = "Falha"
                    return
            except Exception as exc:
                if tftp_completed:
                    _emit_step("firmware_upgrade", "success")
                    _emit_log("WARNING", f"TFTP concluído, mas verificação da partição falhou: {exc}")
                    _migration_data["upgrade"] = "Sim"
                else:
                    _emit_step("firmware_upgrade", "error")
                    _emit_log("ERROR", f"Falha ao verificar partição após TFTP: {exc}")
                    _migration_data["status_final"] = "Falha"
                    return

            # Etapa 4: reboot
            _emit_step("device_reboot", "running")
            _emit_status("Reiniciando device ...", "blue")
            _emit_log("INFO", "[Etapa 4] Enviando 'reload' ao switch ...")
            try:
                trigger_reload(connection, progress_callback=lambda m: _emit_log("INFO", m))
                _emit_step("device_reboot", "success")
                _emit_log("SUCCESS", "Reload enviado. Device reiniciando ...")
            except Exception as exc:
                _emit_log("WARNING", f"Aviso durante reload: {exc}")
                _emit_step("device_reboot", "warning")
            finally:
                close_connection(connection)
                connection = None

            # Etapa 5: ping wait
            _emit_step("ping_wait", "running")
            _emit_status("Aguardando retorno do device ...", "blue")
            _emit_log("INFO", f"[Etapa 5] Aguardando retorno de {ip} via ping ...")
            try:
                wait_for_device(
                    ip=ip,
                    total_timeout=config.PING_TIMEOUT,
                    interval=config.PING_INTERVAL,
                    initial_delay=config.RELOAD_WAIT_INITIAL,
                    progress_callback=lambda msg: _emit_log("INFO", msg),
                )
                _emit_step("ping_wait", "success")
                _emit_log("SUCCESS", f"Device {ip} voltou à rede.")
            except PingTimeoutError as exc:
                _emit_step("ping_wait", "error")
                _emit_log("ERROR", str(exc))
                _emit_status("Timeout aguardando device", "red")
                _migration_data["status_final"] = "Falha"
                return

            # Etapa 6: reconexão SSH
            _emit_step("ssh_reconnect", "running")
            _emit_status("Reconectando via SSH ...", "blue")
            _emit_log("INFO", f"[Etapa 6] Reconectando SSH em {ip} ...")
            connected = False
            for attempt in range(1, config.SSH_RECONNECT_RETRIES + 1):
                try:
                    connection = create_connection(
                        ip, username, password, timeout=config.SSH_TIMEOUT
                    )
                    _emit_step("ssh_reconnect", "success")
                    _emit_log("SUCCESS",
                        f"SSH reconectado (tentativa {attempt}/{config.SSH_RECONNECT_RETRIES})")
                    connected = True
                    break
                except SSHConnectionError as exc:
                    _emit_log("WARNING", f"Tentativa {attempt} falhou: {exc}")
                    if attempt < config.SSH_RECONNECT_RETRIES:
                        time.sleep(config.SSH_RECONNECT_DELAY)

            if not connected:
                _emit_step("ssh_reconnect", "error")
                _emit_log("ERROR", "Todas as tentativas de reconexão SSH falharam.")
                _emit_status("Falha na reconexão SSH", "red")
                _migration_data["status_final"] = "Falha"
                return

            # ── Double-check pós-reboot: sh flash == show version ─────────────
            _emit_log("INFO",
                "Double-check: comparando firmware na partição (sh flash) "
                "com firmware em execução (show version) ...")
            try:
                flash_file, running_file, fw_match = double_check_firmware(connection)
                if fw_match:
                    _emit_log("SUCCESS",
                        f"Double-check OK: partição e execução coincidem ({running_file})")
                else:
                    _emit_log("WARNING",
                        f"Mismatch: partição={flash_file} | execução={running_file}. "
                        "Rebootando para ativar o firmware correto ...")
                    _emit_status("Reboot de correção — aguardando retorno ...", "blue")

                    try:
                        trigger_reload(connection, progress_callback=lambda m: _emit_log("INFO", m))
                    except Exception:
                        pass
                    finally:
                        close_connection(connection)
                        connection = None

                    # Aguarda ICMP voltar
                    try:
                        wait_for_device(
                            ip=ip,
                            total_timeout=config.PING_TIMEOUT,
                            interval=config.PING_INTERVAL,
                            initial_delay=config.RELOAD_WAIT_INITIAL,
                            progress_callback=lambda msg: _emit_log("INFO", msg),
                        )
                        _emit_log("SUCCESS", f"Device {ip} voltou após reboot de correção.")
                    except PingTimeoutError as exc:
                        _emit_step("ssh_reconnect", "error")
                        _emit_log("ERROR", f"Timeout aguardando device após reboot de correção: {exc}")
                        _migration_data["status_final"] = "Falha"
                        return

                    # Reconecta SSH
                    for attempt in range(1, config.SSH_RECONNECT_RETRIES + 1):
                        try:
                            time.sleep(config.SSH_RECONNECT_DELAY)
                            connection = create_connection(
                                ip, username, password, timeout=config.SSH_TIMEOUT
                            )
                            _emit_log("SUCCESS",
                                f"Reconectado após reboot de correção (tentativa {attempt})")
                            break
                        except SSHConnectionError as exc:
                            _emit_log("WARNING", f"Tentativa {attempt} falhou: {exc}")
                            if attempt == config.SSH_RECONNECT_RETRIES:
                                _emit_step("ssh_reconnect", "error")
                                _emit_log("ERROR",
                                    "Não foi possível reconectar após reboot de correção.")
                                _migration_data["status_final"] = "Falha"
                                return

                    # Verifica novamente após o reboot de correção
                    try:
                        _, running_final, match_final = double_check_firmware(connection)
                        if match_final:
                            _emit_log("SUCCESS",
                                f"Double-check OK após reboot de correção: {running_final}")
                        else:
                            _emit_log("WARNING",
                                f"Firmware ainda diverge após reboot de correção: "
                                f"running={running_final}. Prosseguindo com aviso.")
                            warnings.append("firmware double-check divergente")
                    except Exception as exc:
                        _emit_log("WARNING",
                            f"Não foi possível verificar double-check após reboot de correção: {exc}")

            except Exception as exc:
                _emit_log("WARNING",
                    f"Double-check (sh flash / show version) falhou: {exc}. "
                    "Continuando com base na verificação de sh flash.")

        else:
            for sid in ["firmware_upgrade", "device_reboot", "ping_wait", "ssh_reconnect"]:
                _emit_step(sid, "skipped")
            _migration_data["upgrade"] = "Não"
            _emit_log("INFO",
                f"Etapas 3–6 puladas — versão {current_version} já atende ao mínimo.")

        # ── Pré-etapa: busca dados complementares na SmartZone API ANTES do CLI ─
        # O CLI desconecta o switch do SZ, então o lookup via API deve ser feito antes.
        # Os dados do CLI coletados na Etapa 1 têm prioridade; SZ preenche apenas o que falta.
        _emit_log("INFO", "[Pré-etapa] Buscando dados complementares na SmartZone API ...")
        try:
            switch_info_pre = remove_switch_from_smartzone(
                base_url=config.SMARTZONE_API_BASE_URL,
                api_version=config.SMARTZONE_API_VERSION,
                username=config.SMARTZONE_API_USERNAME,
                password=config.SMARTZONE_API_PASSWORD,
                ip_address=ip,
                verify_ssl=config.SMARTZONE_API_VERIFY_SSL,
            )
            # Apenas preenche campos que o CLI não coletou
            sz_map = {
                "hostname": switch_info_pre.get("switchName"),
                "mac":      switch_info_pre.get("macAddress"),
                "serial":   switch_info_pre.get("serialNumber"),
                "modelo":   switch_info_pre.get("model"),
                "site":     switch_info_pre.get("groupName"),
            }
            for key, val in sz_map.items():
                if val and not _migration_data.get(key):
                    _migration_data[key] = val
            _migration_data["sz_api"]     = "Sucesso"
            _migration_data["apagado_sz"] = "Sim"
            _emit_log("SUCCESS",
                f"SmartZone API: hostname={switch_info_pre.get('switchName')} "
                f"mac={switch_info_pre.get('macAddress')} "
                f"serial={switch_info_pre.get('serialNumber')}")
            _emit_log("INFO",
                f"Dados finais do device: hostname={_migration_data.get('hostname')} "
                f"mac={_migration_data.get('mac')} serial={_migration_data.get('serial')}")
        except SmartZoneAPIError as exc:
            _emit_log("WARNING", f"Não foi possível obter dados via SmartZone API: {exc}")

        # ── Etapa 7: Remove SmartZone (CLI) ──────────────────────────────────
        _emit_step("smartzone_cli", "running")
        _emit_status("Removendo SmartZone via CLI ...", "blue")
        _emit_log("INFO",
            f"[Etapa 7] Removendo SmartZone via CLI ({config.SMARTZONE_MANAGER_IP}) ...")
        try:
            sz_result = remove_smartzone(
                connection,
                config.SMARTZONE_MANAGER_IP,
                lambda m: _emit_log("INFO", m),
            )
            _migration_data["sz_cli"] = "Sucesso"
            if sz_result.get("manager_reenabled"):
                _emit_step("smartzone_cli", "success")
                _emit_log("SUCCESS", "SmartZone removido via CLI — switch iniciará descoberta do Ruckus One.")
            else:
                _emit_step("smartzone_cli", "warning")
                _emit_log("WARNING",
                    "SmartZone removido, mas 'no manager disable' não foi executado "
                    "(sessão SSH encerrada após manager disconnect). "
                    "Execute manualmente no switch: conf t; no manager disable; end")
                warnings.append("no manager disable pendente")
        except SmartZoneRemovalError as exc:
            _migration_data["sz_cli"] = "Falha"
            _emit_step("smartzone_cli", "error")
            _emit_log("ERROR", str(exc))
            _emit_status("Falha ao remover SmartZone (CLI)", "red")
            _migration_data["status_final"] = "Falha"
            return
        finally:
            if connection:
                close_connection(connection)
                connection = None

        # ── Etapa 8: SmartZone REST API (delete do registro) ─────────────────
        _emit_step("smartzone_api", "running")
        _emit_status("Deletando switch do SmartZone via API ...", "blue")
        _emit_log("INFO", f"[Etapa 8] Deletando switch {ip} do SmartZone via REST API ...")
        try:
            switch_info = remove_switch_from_smartzone(
                base_url=config.SMARTZONE_API_BASE_URL,
                api_version=config.SMARTZONE_API_VERSION,
                username=config.SMARTZONE_API_USERNAME,
                password=config.SMARTZONE_API_PASSWORD,
                ip_address=ip,
                verify_ssl=config.SMARTZONE_API_VERIFY_SSL,
            )
            # Preenche apenas campos ainda ausentes (CLI tem prioridade)
            sz_map = {
                "hostname": switch_info.get("switchName"),
                "mac":      switch_info.get("macAddress"),
                "serial":   switch_info.get("serialNumber"),
                "modelo":   switch_info.get("model"),
                "site":     switch_info.get("groupName"),
            }
            for key, val in sz_map.items():
                if val and not _migration_data.get(key):
                    _migration_data[key] = val
            _migration_data["sz_api"]     = "Sucesso"
            _migration_data["apagado_sz"] = "Sim"
            _emit_step("smartzone_api", "success")
            _emit_log("SUCCESS",
                f"Switch {_migration_data.get('hostname')} ({_migration_data.get('serial')}) "
                "removido do SmartZone via API.")
        except SmartZoneAPIError as exc:
            _migration_data.update({"sz_api": "Falha", "apagado_sz": "Não"})
            _emit_step("smartzone_api", "warning")
            _emit_log("WARNING", f"Falha ao deletar do SmartZone via API (migração CLI concluída): {exc}")
            warnings.append("SmartZone API")

        # ── Etapa 9: Ruckus One — associar switch à venue ────────────────────
        _emit_step("ruckus_one_venue", "running")
        _emit_status("Associando switch ao Ruckus One ...", "blue")
        _emit_log("INFO", "[Etapa 9] Iniciando integração com Ruckus One API ...")

        device_name = _migration_data.get("hostname")
        mac_address = _migration_data.get("mac")
        ruckus_one_status = "Pulado"

        if not device_name and not mac_address:
            _emit_log("WARNING",
                "Hostname e MAC não disponíveis (SmartZone API falhou). "
                "Prosseguindo para Ruckus One sem identificador automático do switch.")
        if True:
            try:
                # 9.1 Autenticar
                r1_token = get_access_token(
                    config.RUCKUS_ONE_TOKEN_URL,
                    config.RUCKUS_ONE_TENANT_ID,
                    config.RUCKUS_ONE_CLIENT_ID,
                    config.RUCKUS_ONE_CLIENT_SECRET,
                )
                _emit_log("SUCCESS", "Token Ruckus One obtido.")

                # 9.2 Lookup site_slug e site_name no CSV Netbox
                site_slug, site_name = get_site_slug_from_netbox(device_name, config.NETBOX_CSV_PATH)
                if site_slug:
                    _emit_log("INFO", f"site_slug encontrado no Netbox: '{device_name}' → '{site_slug}'")
                else:
                    _emit_log("WARNING", f"'{device_name}' não encontrado no CSV Netbox. Usando hostname como slug.")
                    site_slug = device_name

                # 9.3 Pede ao analista o nome da venue que irá criar/usar
                global _venue_name_value
                _venue_name_value = ""
                _venue_name_event.clear()
                _push("venue_name_request",
                      site_slug=site_slug,
                      site_name=site_name or "",
                      device_name=device_name)
                _emit_status("Aguardando nome da venue ...", "yellow")
                _emit_log("INFO",
                    f"[Etapa 9] Aguardando analista informar o nome da venue "
                    f"(sugestão: '{site_slug}') ...")
                _venue_name_event.wait(timeout=config.RUCKUS_ONE_VENUE_NOT_FOUND_TIMEOUT)

                if not _venue_name_value:
                    _emit_log("ERROR", "Timeout aguardando nome da venue. Etapa abortada.")
                    _emit_step("ruckus_one_venue", "error")
                    ruckus_one_status = "Falha"
                else:
                    venue_search_name = _venue_name_value
                    _emit_log("INFO", f"Nome da venue informado: '{venue_search_name}'")

                    # 9.4 Buscar venue pelo nome informado
                    venue = find_venue_by_slug(config.RUCKUS_ONE_API_BASE_URL, r1_token, venue_search_name)

                    if not venue:
                        _emit_log("WARNING",
                            f"Venue '{venue_search_name}' não encontrada. "
                            "Aguardando criação no Ruckus One ...")
                        _push("venue_not_found",
                              site_slug=venue_search_name,
                              site_name=site_name or "",
                              device_name=device_name)
                        _emit_status(f"Aguardando criação da venue '{venue_search_name}' ...", "yellow")

                        global _venue_created
                        _venue_created = False
                        _venue_event.clear()
                        _venue_event.wait(timeout=config.RUCKUS_ONE_VENUE_NOT_FOUND_TIMEOUT)

                        if not _venue_created:
                            _emit_log("ERROR",
                                f"Timeout aguardando venue '{venue_search_name}'. Etapa abortada.")
                            _emit_step("ruckus_one_venue", "error")
                            ruckus_one_status = "Falha"
                        else:
                            # Retry com novo token
                            r1_token = get_access_token(
                                config.RUCKUS_ONE_TOKEN_URL,
                                config.RUCKUS_ONE_TENANT_ID,
                                config.RUCKUS_ONE_CLIENT_ID,
                                config.RUCKUS_ONE_CLIENT_SECRET,
                            )
                            venue = find_venue_by_slug(
                                config.RUCKUS_ONE_API_BASE_URL, r1_token, venue_search_name
                            )
                            if not venue:
                                _emit_log("ERROR",
                                    f"Venue '{venue_search_name}' ainda não encontrada após confirmação.")
                                _emit_step("ruckus_one_venue", "error")
                                ruckus_one_status = "Falha"

                    if venue:
                        _emit_log("INFO", f"Venue encontrada: '{venue['name']}' (id={venue['id']})")

                        # 9.5 Adicionar switch à venue diretamente pelo serial
                        # (equivale ao "Add Switch" manual — POST /venues/{id}/switches/{switchId}).
                        # Prioriza o serial; cai para MAC se o serial não foi coletado.
                        switch_id = _migration_data.get("serial") or mac_address
                        if not switch_id:
                            _emit_log("ERROR",
                                "Sem serial nem MAC do switch — impossível adicionar via API. "
                                "Verifique a coleta via CLI (Etapa 1).")
                            _emit_step("ruckus_one_venue", "error")
                            ruckus_one_status = "Falha"
                        else:
                            _emit_log("INFO",
                                f"Adicionando switch '{switch_id}' (hostname '{device_name}') "
                                f"à venue '{venue['name']}' via API ...")
                            assign_switch_to_venue(
                                config.RUCKUS_ONE_API_BASE_URL,
                                r1_token,
                                venue["id"],
                                switch_id,
                                name=_migration_data.get("hostname"),
                            )
                            _emit_step("ruckus_one_venue", "success")
                            _emit_log("SUCCESS",
                                f"Switch '{switch_id}' adicionado à venue '{venue['name']}' no Ruckus One. "
                                "(202 Accepted — propagação assíncrona, confirme no painel.)")
                            ruckus_one_status = "Sucesso"
                            _migration_data["ruckus_one_venue"] = venue["name"]

            except RuckusOneAPIError as exc:
                _emit_log("WARNING", f"Falha na integração Ruckus One: {exc}")
                _emit_step("ruckus_one_venue", "warning")
                ruckus_one_status = "Falha"
                warnings.append("Ruckus One")

        _migration_data["ruckus_one_status"] = ruckus_one_status

        # ── Etapa 10: Atualizar dashboard Grid ────────────────────────────────
        _emit_step("grid_update", "running")
        _emit_status("Atualizando dashboard Grid ...", "blue")
        _emit_log("INFO", "[Etapa 10] Enviando registro de migração ao Grid ...")

        if warnings:
            _migration_data["status_final"] = "Concluído com Avisos"
        else:
            _migration_data["status_final"] = "Concluído"

        try:
            append_migration_record(
                config.GRID_DOC_ID,
                config.GRID_API_BASE_URL,
                config.GRID_API_TOKEN,
                _migration_data,
            )
            _emit_step("grid_update", "success")
            _emit_log("SUCCESS", "Registro enviado ao Grid com sucesso.")
        except GridAPIError as exc:
            _emit_step("grid_update", "warning")
            _emit_log("WARNING", f"Falha ao enviar ao Grid: {exc}")
            warnings.append("Grid update")

        if warnings:
            _emit_status("Concluído com avisos", "yellow")
        else:
            _emit_status("Migração concluída com sucesso!", "green")

        _emit_log("SUCCESS", "═" * 52)
        _emit_log("SUCCESS", f"  MIGRAÇÃO CONCLUÍDA — Switch {ip}")
        _emit_log("SUCCESS", "═" * 52)

        _push("pipeline_done", success=True, ip=ip, has_warnings=bool(warnings))

    except Exception as exc:
        _emit_log("ERROR", f"Erro inesperado no pipeline: {exc}")
        _emit_status("Erro inesperado", "red")
        _migration_data["status_final"] = "Falha"
        _push("pipeline_done", success=False)
    finally:
        if connection:
            close_connection(connection)
        with _state_lock:
            _running = False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n  Ruckus ICX — Migration Tool")
    print("  Acesse: http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
