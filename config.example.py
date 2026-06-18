# ══════════════════════════════════════════════════════════════
#  config.example.py — Template de configuração
#
#  1. Copie este arquivo: cp config.example.py config.py
#  2. Preencha os valores marcados como PREENCHER
#  3. NUNCA comite config.py (já está no .gitignore)
# ══════════════════════════════════════════════════════════════
import os

# ── SSH CREDENTIALS ─ CUSTOMIZE ──────────────────────────────────────────────
SWITCH_USERNAME: str = "admin"
SWITCH_PASSWORD: str = ""           # Definido via GUI em runtime

# ── FIRMWARE / TFTP ─ CUSTOMIZE ──────────────────────────────────────────────
TFTP_SERVER_IP: str = "PREENCHER"           # Ex: 10.x.x.x
FIRMWARE_FILENAME: str = "SPR09010k.bin"
FIRMWARE_FLASH_PARTITION: str = "primary"
MINIMUM_FIRMWARE: str = "09.0.10kT213"

# ── SMARTZONE CLI REMOVAL ─ CUSTOMIZE ────────────────────────────────────────
SMARTZONE_MANAGER_IP: str = "PREENCHER"     # Ex: 10.x.x.x

# ── SMARTZONE REST API ─ CUSTOMIZE ───────────────────────────────────────────
SMARTZONE_API_BASE_URL: str = "https://ruckusmanager.shippingis.com:8443"
SMARTZONE_API_VERSION: str = "v11_0"
SMARTZONE_API_USERNAME: str = "PREENCHER"   # Usuário de serviço SmartZone
SMARTZONE_API_PASSWORD: str = "PREENCHER"   # Prefira: os.getenv("SZ_PASSWORD", "")
SMARTZONE_API_VERIFY_SSL: bool = False       # False para certificado self-signed

# ── TIMEOUTS ─ CUSTOMIZE PARA SUA REDE ───────────────────────────────────────
SSH_TIMEOUT: int = 30
TFTP_COPY_TIMEOUT: int = 600
RELOAD_WAIT_INITIAL: int = 60
PING_TIMEOUT: int = 300
PING_INTERVAL: int = 10
SSH_RECONNECT_RETRIES: int = 3
SSH_RECONNECT_DELAY: int = 15

# ── GRID DASHBOARD ─ CUSTOMIZE ───────────────────────────────────────────────
GRID_API_BASE_URL: str = "https://grid.melioffice.com/api/v1"
GRID_DOC_ID: str = "01KV90903ZKWB3VX6X5C0R8EM1"
GRID_API_TOKEN: str = "PREENCHER"

# ── LOGGING ───────────────────────────────────────────────────────────────────
LOG_DIR: str = os.path.join(os.path.dirname(__file__), "logs")
