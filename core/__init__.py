class MigrationError(Exception):
    """Exceção base para todos os erros do pipeline de migração."""


class SSHConnectionError(MigrationError):
    """Falha ao estabelecer ou manter conexão SSH com o switch."""


class FirmwareParseError(MigrationError):
    """Não foi possível extrair a versão do firmware da saída de 'sh flash'."""


class FirmwareUpgradeError(MigrationError):
    """Falha durante o processo de upgrade de firmware via TFTP."""


class PingTimeoutError(MigrationError):
    """Device não respondeu ao ping dentro do timeout configurado."""


class SmartZoneRemovalError(MigrationError):
    """Falha ao executar a sequência de remoção do SmartZone via CLI."""


class SmartZoneAPIError(MigrationError):
    """Falha ao interagir com a API REST do SmartZone (busca ou DELETE)."""


class GridAPIError(MigrationError):
    """Falha ao atualizar o dashboard Grid."""


class RuckusOneAPIError(MigrationError):
    """Falha ao interagir com a API REST do Ruckus One."""
