# Specifications — SmartZone API (Switch Manager)
## Integração REST para remoção de switches após migração para Ruckus One

**Versão:** 1.0  
**Data:** 2026-06-16  
**Base URL:** `https://ruckusmanager.shippingis.com:8443`  
**API BasePath:** `/switchm/api/v11_0`  
**Fonte:** OpenAPI Swagger v11_0 — Switch Manager  

---

## 1. Objetivo

Documentar como usar a API REST do Ruckus SmartZone para:

1. **Autenticar** e obter o `serviceTicket`
2. **Localizar** o switch pelo IP para obter o `id` interno do SmartZone
3. **Guardar localmente** os dados relevantes do switch antes de removê-lo
4. **Deletar** o switch do SmartZone via API após a migração para Ruckus One via CLI

---

## 2. Autenticação — serviceTicket

### Como funciona

O SmartZone usa um sistema de **Service Ticket** (similar a um token de sessão temporário). Ele **não está no OpenAPI do Switch Manager** porque pertence à API do SmartZone (wsg), mas é obrigatório em **todas** as chamadas do Switch Manager.

### Endpoint de Login (SmartZone Public API)

```
POST https://ruckusmanager.shippingis.com:8443/wsg/api/public/v11_0/serviceTicket
Content-Type: application/json

{
  "username": "admin",
  "password": "sua-senha"
}
```

### Resposta esperada

```json
{
  "serviceTicket": "ST-XXXXXXX-xxxxxxxxxxxxxxxxxxx-cas"
}
```

### Como usar o ticket

O `serviceTicket` é enviado como **query parameter** em **todas** as requisições:

```
GET /switchm/api/v11_0/switch/{id}?serviceTicket=ST-XXXXXXX-...
DELETE /switchm/api/v11_0/switch/{id}?serviceTicket=ST-XXXXXXX-...
```

### Considerações importantes

- O ticket tem **validade desconhecida** — a estratégia adotada é obter um ticket novo a cada execução do pipeline (login no início da etapa 8), eliminando a preocupação com expiração
- Não há `Bearer Token` nem `Authorization` header — é sempre query param
- **Não há endpoint de logout** documentado neste spec — o ticket expira naturalmente por inatividade
- Como o pipeline de migração dura no máximo ~15 minutos (incluindo reboot), obter o ticket apenas no início da etapa 8 é suficiente

---

## 3. Localizar o Switch pelo IP

### Por que é necessário

A API de delete exige o **`id` interno do SmartZone** (UUID), não o IP. É preciso primeiro consultar o switch para obter esse id.

### Endpoint

```
POST /switchm/api/v11_0/switch?serviceTicket={serviceTicket}
Content-Type: application/json
```

### Payload de busca por IP

```json
{
  "filters": [
    {
      "type": "SWITCH_GROUP",
      "value": "{switchGroupId}"
    }
  ],
  "extraFilters": [
    {
      "type": "SWITCH_ID",
      "value": "{ip_ou_mac}"
    }
  ],
  "page": 1,
  "limit": 10
}
```

> **Alternativa mais simples** — busca por texto livre:

```json
{
  "fullTextSearch": {
    "type": "AND",
    "value": "10.x.x.x"
  },
  "page": 1,
  "limit": 10
}
```

### Resposta — campos relevantes

```json
{
  "totalCount": 1,
  "list": [
    {
      "id": "uuid-interno-do-switch",
      "switchName": "ICX7150-NOME",
      "macAddress": "AA:BB:CC:DD:EE:FF",
      "serialNumber": "CYS3333XXXX",
      "ipAddress": "10.x.x.x",
      "model": "ICX7150-48P",
      "firmwareVersion": "09.0.10kT213",
      "status": "ONLINE",
      "groupId": "uuid-do-grupo",
      "groupName": "nome-do-grupo",
      "domainId": "uuid-do-dominio"
    }
  ]
}
```

---

## 4. Dados a Guardar Localmente (antes de deletar)

Antes de acionar o DELETE, o sistema deve capturar e salvar localmente os seguintes campos para fins de auditoria e rastreabilidade:

| Campo SmartZone     | Descrição                           | Por que guardar                             |
|---------------------|--------------------------------------|----------------------------------------------|
| `id`                | UUID interno do SmartZone            | Necessário para o DELETE e auditoria         |
| `switchName`        | Nome configurado no SmartZone        | Identificação humana                         |
| `macAddress`        | MAC address do switch                | Identificador único físico                   |
| `serialNumber`      | Número de série                      | Auditoria / rastreamento físico              |
| `ipAddress`         | IP de gerência                       | Confirmação de que é o switch correto        |
| `model`             | Modelo do switch (ICX7150-48P etc.)  | Validação do dispositivo                     |
| `firmwareVersion`   | Versão do firmware no momento        | Registro do estado pré-migração              |
| `status`            | ONLINE / OFFLINE                     | Confirmar que está acessível antes de deletar|
| `groupId`           | ID do grupo no SmartZone             | Auditoria de onde estava                     |
| `groupName`         | Nome do grupo no SmartZone           | Usado como **Site** no dashboard Grid        |

### Validações obrigatórias antes de deletar

1. `status == "ONLINE"` — confirma que é o switch correto e está respondendo
2. `ipAddress` bate com o IP informado pelo operador na ferramenta
3. `model` contém `ICX7150` — evita deletar o dispositivo errado
4. Switch encontrado é único (`totalCount == 1`) — evita ambiguidade

---

## 5. Deletar o Switch do SmartZone

### Endpoint (delete unitário)

```
DELETE /switchm/api/v11_0/switch/{id}?serviceTicket={serviceTicket}
```

| Parâmetro       | Onde       | Tipo   | Descrição                    |
|-----------------|------------|--------|-------------------------------|
| `id`            | path       | string | UUID do switch no SmartZone   |
| `serviceTicket` | query      | string | Ticket de autenticação        |

### Resposta de sucesso

```
HTTP 200 OK
{
  "id": "uuid-do-switch",
  "name": "ICX7150-NOME"
}
```

### Endpoint alternativo (delete em massa)

```
DELETE /switchm/api/v11_0/switch?serviceTicket={serviceTicket}
Content-Type: application/json

["uuid-switch-1", "uuid-switch-2"]
```

> Para o fluxo de migração unitária, usar sempre o delete por `{id}`.

### Erros possíveis

| HTTP | Descrição                              | Ação sugerida                         |
|------|----------------------------------------|---------------------------------------|
| 400  | Bad Request — parâmetro inválido       | Verificar formato do `id`             |
| 403  | Forbidden — sem privilégio de admin    | Verificar credenciais do serviceTicket|
| 500  | Internal Server Error                  | Retry após intervalo, logar erro       |

### Tratamento de erros SSL

`verify_ssl=False` é a configuração adotada (certificado auto-assinado). Mesmo assim, erros de SSL devem ser capturados e identificados explicitamente para não aparecerem como erros genéricos:

```python
import requests
from requests.exceptions import SSLError, ConnectionError, Timeout

try:
    response = requests.delete(url, params=..., verify=False)
except SSLError as e:
    # Erro de certificado — mesmo com verify=False pode ocorrer em casos de
    # protocolo incompatível (ex: TLS version mismatch) ou hostname inválido
    raise SmartZoneAPIError(f"Erro de certificado SSL ao conectar ao SmartZone: {e}")
except ConnectionError as e:
    raise SmartZoneAPIError(f"SmartZone inacessível (connection refused ou DNS): {e}")
except Timeout as e:
    raise SmartZoneAPIError(f"Timeout ao conectar ao SmartZone: {e}")
```

> Mensagens de log devem incluir o tipo de erro SSL para facilitar diagnóstico sem precisar consultar stack trace completo.

---

## 6. Fluxo Completo — Integração com o Pipeline

```
[Operador informa IP + senha na interface web]
         │
         ▼
[Etapa 1–7: Pipeline SSH/CLI existente]
         │
         ▼
[Etapa 8 — Nova: SmartZone API]
    │
    ├─ POST /serviceTicket → obtém serviceTicket
    │
    ├─ POST /switch (busca por IP) → obtém id, salva dados localmente
    │
    ├─ Validações:
    │    ├─ totalCount == 1
    │    ├─ ipAddress == IP do operador
    │    ├─ model contém "ICX7150"
    │    └─ status == "ONLINE"
    │
    ├─ DELETE /switch/{id} → remove do SmartZone
    │
    ├─ Log de SUCESSO: "Switch {switchName} ({serialNumber}) removido do SmartZone com sucesso."
    │   ou
    └─ Log de ERRO:    "Falha ao remover switch {ip} do SmartZone: {motivo}" → pipeline continua (WARNING)
```

> **Comportamento em caso de falha no DELETE:**  
> A migração CLI já foi concluída com sucesso.  
> Falha no DELETE do SmartZone deve ser registrada como **WARNING**, não abortar o pipeline.  
> O operador precisa remover manualmente via console do SmartZone se necessário.

---

## 11. Dashboard de Migrações — Grid (Mercado Livre)

### Contexto

O **Grid** (grid.adminml.com / grid.melioffice.com) é a plataforma interna do Mercado Livre para compartilhamento de documentos e dados entre times. Ao final de cada migração, os dados de resultado devem ser registrados em um dashboard no Grid para acompanhamento do progresso geral da operação.

### Objetivo do dashboard

Oferecer visibilidade simples do status de cada switch migrado, sem necessidade de acessar logs da ferramenta.

### Dados a registrar por migração

> ℹ️ **Desde a v3.0**, hostname/serial/MAC/modelo são coletados via **CLI**
> (`show version` + prompt SSH) na etapa 1 como **fonte primária**. A SmartZone
> API (etapa 8) preenche apenas o que o CLI não obteve. Garante o registro
> mesmo quando o switch já não está no SmartZone.

| Campo               | Origem                          | Descrição                                   |
|---------------------|----------------------------------|----------------------------------------------|
| Data/Hora           | Sistema                          | Timestamp da execução                        |
| IP do Switch        | Operador (input da ferramenta)   | IP de gerência                               |
| Nome do Switch      | **SSH CLI** / SmartZone (`switchName`) | Hostname (CLI primário desde a v3.0)   |
| Serial Number       | **SSH CLI** / SmartZone (`serialNumber`) | Identificador físico (CLI primário)  |
| Modelo              | **SSH CLI** / SmartZone (`model`) | Ex: ICX7150-48P (CLI primário)              |
| Firmware antes      | SSH CLI (`get_firmware_version`) | Versão antes da migração                     |
| Upgrade realizado   | Pipeline (etapa 3)               | Sim / Não                                    |
| SmartZone removido  | CLI (etapa 7)                    | Sucesso / Falha                              |
| SmartZone API       | SmartZone REST API (etapa 8)     | Sucesso / Falha / Warning                    |
| Status final        | Pipeline                         | ✅ Concluído / ⚠️ Concluído com avisos / ❌ Falha |
| Operador            | A definir                        | Identificação de quem executou               |

### Formato sugerido

Planilha no Grid (Google Sheets / Excel) com uma linha por migração, atualizada ao final de cada execução da ferramenta. Pode ser atualizada manualmente pelo operador inicialmente, com possibilidade de automação via API do Grid futuramente.

### Perguntas em aberto (Grid)

- [ ] Já existe um documento/planilha no Grid para este projeto ou precisa criar?
- [ ] Qual time/pasta do Grid deve receber o documento?
- [ ] A atualização será manual pelo operador ou deve ser automatizada pela ferramenta?
- [ ] Há campos adicionais de negócio necessários (ex: site, andar, responsável, ticket Jira)?

---

## 7. Configuração Necessária no config.py

Os seguintes parâmetros precisam ser adicionados ao `config.py` para a integração SmartZone:

```python
# ── SMARTZONE REST API ────────────────────────────────────────────────────────
SMARTZONE_API_BASE_URL: str  = "https://ruckusmanager.shippingis.com:8443"
SMARTZONE_API_VERSION: str   = "v11_0"
SMARTZONE_API_USERNAME: str  = "PREENCHER"     # Usuário de serviço SmartZone
SMARTZONE_API_PASSWORD: str  = "PREENCHER"     # Senha (preferencialmente via env var)
SMARTZONE_API_VERIFY_SSL: bool = False         # False se certificado self-signed
```

> **Segurança:** A senha do SmartZone não deve ficar em texto plano. Avaliar uso de variável de ambiente (`os.getenv("SZ_PASSWORD")`).

---

## 8. Módulo Sugerido — core/smartzone_api.py

O módulo a criar (em tarefa futura) deve expor:

```python
def get_service_ticket(base_url, username, password, verify_ssl=False) -> str:
    """Autentica e retorna o serviceTicket."""

def find_switch_by_ip(base_url, api_version, service_ticket, ip_address, verify_ssl=False) -> dict:
    """Busca o switch pelo IP e retorna seus dados completos."""

def validate_switch_data(switch_data: dict, expected_ip: str) -> None:
    """Valida os dados retornados antes de deletar. Lança exceção se inválido."""

def delete_switch(base_url, api_version, service_ticket, switch_id, verify_ssl=False) -> dict:
    """Remove o switch do SmartZone. Retorna o resultado da API."""

def remove_switch_from_smartzone(base_url, api_version, username, password, ip_address, verify_ssl=False) -> dict:
    """Orquestra todo o fluxo: login → busca → validação → delete → retorna dados auditoria."""
```

---

## 9. Referências da API

| Recurso            | Método | Path                              |
|--------------------|--------|-----------------------------------|
| Login / Ticket     | POST   | `/wsg/api/public/v11_0/serviceTicket` |
| Buscar switches    | POST   | `/switchm/api/v11_0/switch`       |
| Buscar por ID      | GET    | `/switchm/api/v11_0/switch/{id}`  |
| Deletar switch     | DELETE | `/switchm/api/v11_0/switch/{id}`  |
| Deletar múltiplos  | DELETE | `/switchm/api/v11_0/switch`       |
| Histórico firmware | GET    | `/switchm/api/v11_0/switch/{switchId}/firmware` |

**Swagger UI (Switch Manager):** `https://ruckusmanager.shippingis.com:8443/switchm/api/doc`  
**Swagger UI (SmartZone):** `https://ruckusmanager.shippingis.com:8443/wsg/apiDoc/`

---

## 10. Perguntas em Aberto / Respondidas

- [x] Quais são as credenciais do usuário de serviço SmartZone para automação? → `admin` / salva em `config.py` (fora do git)
- [ ] O certificado SSL é auto-assinado? (impacta `verify_ssl`) → assumido `False` (sem verificação) até confirmação
- [x] Há restrição de IP de origem para acesso à API? → **Não há restrição de firewall**
- [ ] Qual é o `switchGroupId` / `domainId` dos switches que serão migrados? → necessário para refinar busca se houver ambiguidade de IPs entre grupos
- [x] O ticket de serviço expira em quanto tempo? → **Tempo desconhecido**. Estratégia: obter ticket novo a cada execução do pipeline (início da etapa SmartZone API), sem reaproveitamento entre migrações
- [x] Após o DELETE, precisa notificar algum sistema adicional além do GRID? → **Não**. Apenas registrar log de sucesso/falha e seguir o pipeline
