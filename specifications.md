# Specifications — Ruckus ICX Migration Tool
## SmartZone → Ruckus One

**Versão:** 3.0 (Web App — pipeline completo SmartZone + Ruckus One + Grid)
**Data:** 2026-06-18
**Autor:** Engenharia de Automação de Redes

---

## 1. Objetivo

Automatizar a migração de switches Ruckus ICX 7150 da plataforma SmartZone para Ruckus One via interface web. O operador acessa a ferramenta pelo browser, informa o IP e senha do switch, e acompanha cada etapa em tempo real.

---

## 2. Arquitetura

```
Browser ──SSE──► Flask (app.py)
       ◄──POST──         │
                     Background Thread
                         │
               core/ (SSH, firmware, ping, API)
                         │
                    Switch ICX 7150
```

### Comunicação em tempo real

- **SSE (Server-Sent Events)** — servidor envia eventos unidirecionais ao browser via `/stream`
- Cada evento é um JSON com campo `type`: `state`, `step`, `log`, `status`, `upgrade_required`, `pipeline_done`, `ping`
- Browser reconecta automaticamente em caso de queda (EventSource nativo)
- Reconexão a cada 3s em caso de erro

### Confirmação de upgrade

- Pipeline pausa e envia evento `upgrade_required` ao browser
- Browser exibe modal de confirmação
- Usuário clica Confirmar/Cancelar → browser envia `POST /confirm-upgrade`
- Pipeline aguarda até 5 minutos pela resposta (`threading.Event`)

---

## 3. Estrutura de Arquivos

```
RuckusOne/
├── app.py                     # Flask app — rotas + orquestração do pipeline
├── config.py                  # Todos os parâmetros configuráveis
├── requirements.txt           # Dependências Python
├── specifications.md          # Este documento
│
├── core/                      # Lógica de negócio (independente de GUI/web)
│   ├── __init__.py            # Hierarquia de exceções
│   ├── ssh_client.py          # Wrapper netmiko
│   ├── firmware.py            # Versão, TFTP, reload, collect_device_info (CLI)
│   ├── ping_monitor.py        # Loop ICMP
│   ├── smartzone.py           # Remoção do SmartZone (CLI)
│   ├── smartzone_api.py       # SmartZone REST API (DELETE do switch)
│   ├── ruckus_one_api.py      # Ruckus One REST (venues + cadastro de switch)
│   └── grid_api.py            # REST para o Grid (state GET/PUT)
│
├── templates/
│   └── index.html             # SPA — Tailwind CSS + JavaScript vanilla
│
└── logs/                      # Arquivos .log gerados em runtime
```

---

## 4. Dependências

| Biblioteca | Versão mínima | Função |
|---|---|---|
| `flask` | 3.0.0 | Servidor web + SSE + rotas REST |
| `netmiko` | 4.3.0 | SSH para Ruckus FastIron (ICX) |
| `requests` | 2.31.0 | Chamadas REST ao GRID |

**Instalação e execução:**
```bash
# Usar Python 3.11 do Homebrew (tem Tk 8.6 e todas as deps)
/opt/homebrew/bin/python3.11 -m pip install -r requirements.txt
/opt/homebrew/bin/python3.11 app.py
```

**Acesso:** http://localhost:5000

---

## 5. Rotas da API

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/` | Página principal (SPA) |
| `GET` | `/state` | Estado atual em JSON (para sync inicial) |
| `GET` | `/stream` | SSE — stream de eventos em tempo real |
| `POST` | `/start` | Inicia migração `{"ip": "...", "password": "...", "analista": "...", "controle_cambio": "..."}` |
| `POST` | `/confirm-upgrade` | Confirma upgrade `{"confirmed": true/false}` |
| `POST` | `/set-venue-name` | Analista informa o nome da venue `{"venue_name": "..."}` |
| `POST` | `/venue-created` | Analista confirma criação manual da venue no Ruckus One |
| `POST` | `/reset` | Reseta estado para `pending` |

---

## 6. Eventos SSE

| `type` | Campos | Descrição |
|---|---|---|
| `state` | `running`, `steps`, `status_text`, `status_color` | Estado completo (sync inicial ou após reset) |
| `step` | `step_id`, `status` | Atualização de uma etapa |
| `log` | `level`, `message`, `timestamp` | Linha de log |
| `status` | `text`, `color` | Status geral (barra inferior) |
| `upgrade_required` | `current_version`, `minimum_version` | Abre modal de confirmação |
| `pipeline_done` | `success`, `ip` | Pipeline finalizado |
| `ping` | — | Keepalive a cada 15s |

**Status possíveis por etapa:** `pending` · `running` · `success` · `error` · `skipped` · `warning`

**Cores de status:** `blue` (executando) · `green` (sucesso) · `red` (erro) · `yellow` (cancelado) · `gray` (idle)

---

## 7. Pipeline de Migração

### Fluxo sem upgrade (versão já atende ao mínimo)

```
SSH Connect (+ coleta CLI) → Firmware Check → [skip 3-6] →
Remove SmartZone (CLI) → SmartZone API (DELETE) → Ruckus One (venue+switch) → Grid
```

### Fluxo com upgrade

```
SSH Connect (+ coleta CLI) → Firmware Check → Confirmação Usuário → Upgrade TFTP →
Reload → Ping Wait → Reconnect SSH → Remove SmartZone (CLI) →
SmartZone API (DELETE) → Ruckus One (venue+switch) → Grid
```

---

### Etapa 1 — SSH Connect (+ coleta de dados via CLI)
- Módulo: `core/ssh_client.py :: create_connection()`
- Device type netmiko: `ruckus_fastiron`
- Lança `SSHConnectionError` → pipeline aborta
- **Após conectar**, `core/firmware.py :: collect_device_info()` coleta como **fonte primária**:
  - **hostname** → prompt SSH limpo (`SSH@BRLABSHP001-1#` → `BRLABSHP001-1`)
  - **serial / MAC / modelo** → parsing de `show version`
- Esses dados alimentam o cadastro no Ruckus One (serial) e o registro no Grid.
  A SmartZone API (pré-etapa/etapa 8) passa a ser **complementar** (só preenche o que faltou).

### Etapa 2 — Firmware Check
- Comando: `sh flash`
- Output real do ICX:
  ```
  Compressed Pri Code size = 33554432, Version:08.0.95kT213 (SPR08095k.bin)
  ```
- Regex: `Compressed Pri Code size = \d+, Version:(\S+) \(`
- Versão mínima: `09.0.10kT213` (em `config.py`)
- Comparação: tupla `(int, int, int, str)` — suporta sufixo alfanumérico

### Etapa 3 — Upgrade de Firmware (condicional)
- Modal no browser aguarda confirmação do usuário
- Comando TFTP: `copy tftp flash <TFTP_IP> <FILENAME> primary`
- Timeout: `TFTP_COPY_TIMEOUT = 600s` (customizável)
- Lança `FirmwareUpgradeError` se detectar indicadores de erro na saída

### Etapa 4 — Reboot (condicional)
- Comando: `reload`
- Responde automaticamente aos prompts:
  - `"save the startup configuration"` → `"n"`
  - `"are you sure"` / `"proceed with reload"` → `"y"`
- `EOFError` (sessão encerrada) é esperado e tratado como não-fatal

### Etapa 5 — Ping Wait (condicional)
- Aguarda `RELOAD_WAIT_INITIAL = 60s` antes de iniciar o loop
- Pinga a cada `PING_INTERVAL = 10s`
- Timeout total: `PING_TIMEOUT = 500s` (aumentado de 300s — alguns ICX demoram a voltar)
- Lança `PingTimeoutError` se exceder → pipeline aborta

### Etapa 6 — Reconnect SSH (condicional)
- Retry: `SSH_RECONNECT_RETRIES = 3` tentativas
- Delay: `SSH_RECONNECT_DELAY = 15s` entre tentativas

### Etapa 7 — Remove SmartZone (CLI)
- Módulo: `core/smartzone.py :: remove_smartzone()`
- Sequência de comandos:
  ```
  conf t
  no manager active-list 10.62.66.164
  manager disconnect
  manager disable
  no manager disable
  end
  ```
- `manager disconnect` pode encerrar a sessão SSH → `EOFError` é não-fatal
- IP do manager configurável: `SMARTZONE_MANAGER_IP` em `config.py`

### Etapa 8 — SmartZone API (DELETE)
- Módulo: `core/smartzone_api.py :: remove_switch_from_smartzone()`
- Remove o registro do switch no SmartZone via REST API
- Também retorna dados do switch (hostname/mac/serial/model/group) — usados como
  **complemento** dos dados já coletados via CLI (etapa 1)
- Falha é **WARNING** (a migração CLI já foi concluída)

### Etapa 9 — Ruckus One (venue + cadastro do switch)
- Módulo: `core/ruckus_one_api.py`
- Double-check do `site_slug` no CSV Netbox; se não achar, operador digita a venue
- Busca a venue por nome (`GET /venues` + filtro client-side)
- **Cadastra o switch direto pelo serial** (equivale ao "Add Switch" manual):
  ```
  POST /venues/{venueId}/switches/{serial}
  body: { "id": "{serial}", "name": "{hostname}" }
  ```
- Resposta `202 Accepted` (assíncrono). Detalhes completos: `specifications_ruckusone_api.md`
- Falha é **WARNING**

### Etapa 10 — Grid (dashboard)
- Módulo: `core/grid_api.py :: append_migration_record()`
- `GET` no Grid State → append do registro → `PUT` no Grid State
- Auth via token `grid_sk_*` (Bearer) + header `x-api-source: office`
- Falha é **WARNING**. Detalhes completos: `grid/specifications_grid.md`

---

## 8. Tratamento de Erros

| Etapa | Exceção | Comportamento |
|---|---|---|
| SSH Connect | `SSHConnectionError` | Aborta, botões reabilitados |
| Firmware Check | `FirmwareParseError` | Aborta |
| Firmware Upgrade | `FirmwareUpgradeError` | Aborta (NÃO executa reload) |
| Ping Wait | `PingTimeoutError` | Aborta, mensagem de intervenção manual |
| SSH Reconnect | `SSHConnectionError` após N retries | Aborta |
| SmartZone Remove (CLI) | `SmartZoneRemovalError` | Aborta |
| SmartZone API (DELETE) | `SmartZoneAPIError` | Warning — migração CLI já concluída |
| Ruckus One | `RuckusOneAPIError` | Warning — registra status "Falha"/"Pendente" |
| Grid | `GridAPIError` | Warning — pipeline finaliza com sucesso |
| Qualquer outro | `Exception` | Log de erro, pipeline aborta |

---

## 9. Parâmetros Configuráveis (`config.py`)

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `SWITCH_USERNAME` | `"admin"` | Username SSH |
| `TFTP_SERVER_IP` | `"10.191.35.151"` | Servidor TFTP |
| `FIRMWARE_FILENAME` | `"SPR09010k.bin"` | Arquivo de firmware |
| `MINIMUM_FIRMWARE` | `"09.0.10kT213"` | Versão mínima exigida |
| `SMARTZONE_MANAGER_IP` | `"10.62.66.164"` | IP do manager a remover |
| `SSH_TIMEOUT` | `30` | Timeout SSH (s) |
| `TFTP_COPY_TIMEOUT` | `600` | Timeout TFTP (s) |
| `RELOAD_WAIT_INITIAL` | `60` | Espera pré-ping (s) |
| `PING_TIMEOUT` | `500` | Timeout total ping (s) |
| `PING_INTERVAL` | `10` | Intervalo entre pings (s) |
| `SSH_RECONNECT_RETRIES` | `3` | Tentativas de reconexão |
| `SSH_RECONNECT_DELAY` | `15` | Delay entre reconexões (s) |
| `SMARTZONE_API_*` | — | URL/credenciais SmartZone REST API (etapa 8) |
| `RUCKUS_ONE_*` | — | OAuth2 + base URL Ruckus One (etapa 9) |
| `GRID_DOC_ID` | `01KV9090...` | Documento Grid de destino |
| `GRID_API_BASE_URL` | `https://grid.melioffice.com/api/v1` | URL base da API Grid |
| `GRID_API_TOKEN` | `grid_sk_*` | Token Grid (gerado via `POST /api/v1/tokens`) |

> 🔒 `config.py` é **gitignored** — contém secrets (senhas/tokens). Use
> `config.example.py` como template. Nunca versionar valores reais.

---

## 10. Interface Web

**Framework:** Tailwind CSS (CDN) + JavaScript vanilla + SSE nativo
**Porta padrão:** 5000 (customizável em `app.py`)

**Layout:**
```
┌────────────────────────────────────────────────────────────┐
│  Ruckus ICX — Migration Tool  |  SmartZone → Ruckus One  ● │
├─────────────────────────┬──────────────────────────────────┤
│  PAINEL ESQUERDO        │  LOG DE EXECUÇÃO                  │
│                         │                                   │
│  Switch IP:  [_______]  │  [HH:MM:SS] INFO  ...            │
│  Usuário:    [_______]  │  [HH:MM:SS] SUCCESS ...          │
│  Password:   [_______]  │  [HH:MM:SS] ERROR  ...           │
│  Analista:   [_______]  │  (scrollável, auto-scroll)        │
│  Ctrl Câmbio:[_______]  │                                   │
│                         │                                   │
│  ETAPAS DA MIGRAÇÃO     │                                   │
│  ● 1. Conectar SSH      │                                   │
│  ● 2. Verificar FW      │                                   │
│  ● 3. Upgrade FW        │                                   │
│  ● 4. Reboot            │                                   │
│  ● 5. Ping Wait         │                                   │
│  ● 6. Reconectar SSH    │                                   │
│  ● 7. Remover SZ        │                                   │
│  ● 8. SmartZone API     │                                   │
│  ● 9. Ruckus One Venue  │                                   │
│  ● 10. Atualizar Grid   │                                   │
│                         │                                   │
│  [▶ INICIAR MIGRAÇÃO]   │                                   │
│  [↺ LIMPAR / RESET  ]   │                                   │
│  ● Idle                 │                                   │
└─────────────────────────┴───────────────────────────────────┘
```

**Cores dos dots:** ⚫ pending | 🔵 running (pulsando) | 🟢 success | 🔴 error | 🟡 skipped | 🟠 warning

---

## 11. Integração Grid

O dashboard HTML lê os dados via `window.GRID.state.get()` (campo `migrations`).
O pipeline atualiza esse state ao final via REST.

```python
# config.py
GRID_DOC_ID       = "01KV90903ZKWB3VX6X5C0R8EM1"
GRID_API_BASE_URL = "https://grid.melioffice.com/api/v1"
GRID_API_TOKEN    = "grid_sk_..."   # gerar via POST /api/v1/tokens
```

API real (corrigida — antes dava HTTP 405 por usar POST):
```
GET  /documents/{doc_id}/state   → { "state": {...}, "updated_at": "..." }
PUT  /documents/{doc_id}/state   → body { "state": {...}, "if_updated_at": "..." }
Headers: Authorization: Bearer grid_sk_<token>  +  x-api-source: office
```

Detalhes completos e estrutura do state: `grid/specifications_grid.md`.

---

## 12. Histórico de Versões

| Versão | Data | Descrição |
|---|---|---|
| 1.0 | 2026-06-15 | Versão inicial — GUI desktop (customtkinter) |
| 2.0 | 2026-06-15 | Refatoração para aplicação web (Flask + SSE) |
| 2.1 | 2026-06-18 | Adiciona campos obrigatórios Analista e Controle de Câmbio; Grid atualizado automaticamente ao fim do pipeline |
| 3.0 | 2026-06-18 | Coleta de hostname/serial/MAC via CLI (etapa 1) como fonte primária; cadastro do switch no Ruckus One direto pelo serial (resolve SWITCH-10000/10447); Grid corrigido para GET/PUT do state com token grid_sk_ (resolve HTTP 405); PING_TIMEOUT 300→500s; config.py removido do versionamento |
