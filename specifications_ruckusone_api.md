# Specifications — Ruckus One API
## Integração REST para associação de switches ICX a venues

**Versão:** 3.0 (cadastro direto pelo serial — validado em produção)
**Data:** 2026-06-18
**Fonte:** OpenAPI oficial `switch-0.4.0.yaml` + testes curl + execução real do pipeline

> **Mudança principal da v3.0:** o cadastro do switch passou a ser feito
> **proativamente pelo serial** (equivalente ao "Add Switch" manual do painel),
> via `POST /venues/{venueId}/switches/{switchId}`. O fluxo antigo que
> aguardava o switch aparecer em `/switches/pending` foi **abandonado** —
> ele dependia do switch conectar sozinho ao cloud, o que não acontecia
> de forma confiável.

---

## 1. Visão Geral

API REST com OAuth2 client credentials. Sem versionamento na URI — versão vai no header `Accept`. Rate limit de 2.000 chamadas/minuto por tenant.

### Host (Américas)
```
https://api.ruckus.cloud
```

---

## 2. Autenticação

### Credenciais

| Parâmetro | Valor |
|---|---|
| `tenant_id` | `a7ee34fa73a44eac84ac491de0fbfbad` |
| `client_id` | `e7b12301c9a03d2126ddf13475520c62` |
| `client_secret` | em `config.py` (gitignored) |

### Endpoint

```
POST https://ruckus.cloud/oauth2/token/{tenant_id}
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id={id}&client_secret={secret}
```

### Resposta ✅ (HTTP 200 confirmado)

```json
{
  "access_token": "eyJraWQiOiI...",
  "token_type": "Bearer",
  "expires_in": 7199
}
```

### Uso em todas as requisições

```
Authorization: Bearer {access_token}
```

**Estratégia:** gerar token novo a cada execução do pipeline. `expires_in` = ~2h.

---

## 3. Endpoints Confirmados via Testes

### 3.1 Venues

#### `GET /venues` ✅ HTTP 200
Lista todas as venues do tenant.

```bash
GET https://api.ruckus.cloud/venues
Authorization: Bearer {token}
```

**Resposta:**
```json
[
  {
    "id": "a7558c4ad1754cdda7d8e79fc6a836ec",
    "name": "zona-oeste",
    "description": "SVC Zona Oeste",
    "address": {
      "country": "Brazil",
      "countryCode": "BR",
      "city": "Vila Anastácio, State of São Paulo",
      "addressLine": "...",
      "latitude": -23.51464,
      "longitude": -46.71527,
      "timezone": "America/Sao_Paulo"
    },
    "tags": ["svc-br"],
    "isTemplate": false,
    "isEnforced": false,
    "templateContext": "NONE"
  }
]
```

> ⚠️ **Importante:** `GET /venues?name={name}` NÃO filtra server-side — retorna todas as venues independente do parâmetro. O filtro por `site_slug` deve ser feito **client-side** após buscar todas as venues.

#### `GET /venues/{venueId}/switches` ✅ HTTP 200
Lista switches associados a uma venue específica.

---

### 3.2 Switches

#### `GET /switches` ✅ HTTP 200
Lista todos os switches do tenant.

```bash
GET https://api.ruckus.cloud/switches?pageSize=50
Authorization: Bearer {token}
```

> ⚠️ `?name=` também não filtra server-side — filtrar client-side.

**Estrutura de um switch:**
```json
{
  "id": "10:f0:68:0a:bc:10",
  "venueId": "a7558c4ad1754cdda7d8e79fc6a836ec",
  "name": "BRSCZOACP002-1",
  "description": "Switch de Acesso dados Zona Oeste",
  "enableStack": false,
  "igmpSnooping": "none",
  "jumboMode": false,
  "ipAddressInterfaceType": "VE",
  "ipAddressInterface": "100",
  "ipAddressType": "static",
  "ipAddress": "10.29.208.4",
  "subnetMask": "255.255.255.240",
  "defaultGateway": "10.29.208.1",
  "firmwareVersion": "SPR09010k",
  "dhcpClientEnabled": true,
  "dhcpServerEnabled": false,
  "specifiedType": "ROUTER",
  "rearModule": "none",
  "vlanCustomize": false
}
```

**Observações sobre o campo `id`:**
- Geralmente é o **MAC address** do switch: `"10:f0:68:0a:bc:10"`
- Pode ser o **serial number**: `"FND5026W16R"`

#### `GET /switches/{id}` ✅ HTTP 200
Busca switch específico pelo MAC address ou serial.

```bash
GET https://api.ruckus.cloud/switches/10:f0:68:0a:bc:10
```

#### `GET /switches/pending` ⚠️ HTTP 404 quando vazio
Retorna switches que conectaram ao Ruckus One mas ainda não foram associados a nenhuma venue.

```bash
GET https://api.ruckus.cloud/switches/pending
```

**Quando vazio:**
```json
{
  "requestId": "...",
  "errors": [{"code": "SWITCH-10434", "message": "Switch pending not found."}]
}
```

---

### 3.3 Cadastro de switch — CONFIRMADO via OpenAPI oficial ✅

**Fonte:** `https://docs.ruckus.cloud/_bundle/api/switch-0.4.0.yaml` (Switch Services API v0.4.0).

Existem **dois** endpoints, ambos com `venueId` no path:

| Operação | Método + Path | Body |
|---|---|---|
| **AddSwitch** (1 switch) | `POST /venues/{venueId}/switches/{switchId}` | objeto `IcxSwitch_V1_1` |
| **AddMultipleSwitches** (N) | `POST /venues/{venueId}/switches` | **array** de `IcxSwitch_V1_1` |

> 🐛 **Bug anterior:** o código chamava `POST /venues/{venueId}/switches` (endpoint de múltiplos) enviando **um objeto único** no body em vez de um array → request malformado (SWITCH-10000 / HTTP 400). Corrigido para usar o endpoint singular com `switchId` no path.

#### Campos obrigatórios reais

Os únicos campos `required: true` são **parâmetros de path**:

| Campo | Onde | Obrigatório | Descrição |
|---|---|---|---|
| `venueId` | path | ✅ | ID da venue de destino |
| `switchId` | path | ✅ | Identificador do switch (**serial** ou MAC) |
| (request body) | body | ✅ presente | objeto/array `IcxSwitch_V1_1` |

O schema `IcxSwitch_V1_1` **não possui bloco `required:`** — nenhuma
propriedade individual do body é obrigatória. Não existem campos
`serialNumber`, `model` nem `role`: o identificador é `id` (que no
endpoint singular vai no path como `switchId`), e o tipo é
`specifiedType` (enum: `AUTO` | `SWITCH` | `ROUTER`).

> ⚠️ **Validação em runtime (SWITCH-10447):** apesar de o schema OpenAPI
> não marcar `id` como obrigatório, a API **rejeita** o request se o `id`
> não vier **também no body**:
> ```json
> {"errors":[{"code":"SWITCH-10447","message":"Switch ID is mandatory.","reason":"Provide a valid switch ID."}]}
> ```
> **Portanto: o `id` (serial) deve ir no path E no body.** Payload mínimo
> que funciona:
> ```
> POST /venues/{venueId}/switches/FMH3834S00J
> { "id": "FMH3834S00J", "name": "BRLABSHP001-1" }
> ```

Campos opcionais úteis do body: `name`, `description`, `enableStack`,
`igmpSnooping` (`active`|`passive`|`none`), `jumboMode`,
`ipAddressType` (`static`|`dynamic`|`slaac`), `ipAddress`, `subnetMask`,
`defaultGateway`, `dhcpClientEnabled`, `specifiedType`, `vlanCustomize`.

> ⚠️ O exemplo encontrado em fóruns com `{"switches":[{"serial","role":"STANDALONE"...}]}` **não corresponde** ao schema oficial — provavelmente outra versão/produto. Ignorar.

#### Histórico de erros até chegar ao payload correto

| Erro | Causa | Correção |
|---|---|---|
| `SWITCH-10000` / HTTP 400 | POST no endpoint de múltiplos (`/switches`) com objeto único em vez de array | Usar endpoint singular `/switches/{switchId}` |
| `SWITCH-10447` "Switch ID is mandatory" | `id` ausente no body (só no path) | Incluir `id` no body também |

#### Resposta

`202 Accepted` (assíncrono) com `EmptyResponse`. O sucesso real deve ser
confirmado via **activity API** usando o `requestId` — não vem na resposta imediata.

#### Outros endpoints

| Endpoint | Status | Observação |
|---|---|---|
| `GET /venues/{venueId}/switches/{switchId}` | ✅ | Get switch by id |
| `PUT /venues/{venueId}/switches/{switchId}` | ✅ | UpdateSwitchById (mover/editar) |
| `DELETE /venues/{venueId}/switches/{switchId}` | ✅ | DeleteSwitchById |

---

## 4. Fluxo de Associação de Switch a Venue (v3.0 — cadastro direto)

### Fluxo atual em produção

O cadastro é **proativo pelo serial**, sem depender de o switch aparecer
em `/switches/pending`. Equivale ao "Add Switch" manual do painel.

```
1. Dados do switch (serial, hostname, MAC) já coletados via CLI na Etapa 1
2. Operador informa o nome da venue (sugestão = site_slug do Netbox)
3. GET /venues + filtro client-side → encontra a venue pelo nome
4. POST /venues/{venueId}/switches/{serial}
   body: { "id": "{serial}", "name": "{hostname}" }
   → cadastra o switch direto na venue (202 Accepted, assíncrono)
```

> ❌ **Fluxo abandonado (v2.0):** aguardar o switch em `/switches/pending`
> e só então associar. O switch não conectava sozinho ao cloud de forma
> confiável → timeout constante. As funções `list_pending_switches` /
> `wait_for_pending_switch` foram removidas do código.

### Origem dos dados do switch (mudança importante da v3.0)

O `serial`, `hostname` e `MAC` agora vêm da **coleta via CLI na Etapa 1**
(`core/firmware.py::collect_device_info`), não mais da SmartZone API:

- **hostname** → prompt SSH limpo (`SSH@BRLABSHP001-1#` → `BRLABSHP001-1`)
- **serial / MAC / modelo** → parsing de `show version`
- A SmartZone API passou a ser **fonte complementar** (preenche só o que o CLI não obteve)

Isso garante o cadastro mesmo quando o switch já não está registrado no SmartZone.

### Mapeamento Netbox → Ruckus One

| Campo | Origem | Uso no Ruckus One |
|---|---|---|
| `site_slug` | CSV Netbox (double-check) | `venue.name` — busca via `GET /venues` + filtro client-side. Se não achar no CSV, operador digita a venue |
| `serial` | **CLI (`show version`)** | `switchId` no path + `id` no body do POST |
| `hostname` | **CLI (prompt SSH)** | `name` no body |
| `mac_address` | CLI / SmartZone | fallback de identificador se o serial faltar |

### Algoritmo de busca de venue por site_slug

```python
def find_venue_by_slug(token: str, site_slug: str) -> dict | None:
    """
    GET /venues retorna TODAS as venues — filtramos client-side.
    Compara site_slug com venue['name'] (case-insensitive).
    """
    venues = requests.get(
        f"{RUCKUS_ONE_API_BASE_URL}/venues",
        headers={"Authorization": f"Bearer {token}"}
    ).json()

    for venue in venues:
        if venue["name"].lower() == site_slug.lower():
            return venue
    return None
```

---

## 5. Comportamento quando a venue não existe

1. `find_venue_by_slug()` retorna `None`
2. Pipeline emite evento `venue_not_found` via SSE
3. Interface web exibe painel de pausa com `site_slug` e `site_name` do Netbox
4. Operador cria a venue manualmente no painel Ruckus One
5. Clica em "Venue criada — continuar"
6. Pipeline faz nova busca (retry)
7. Se ainda `None` → registra ERROR, encerra etapa como falha

**Timeout de espera:** 30 minutos (`RUCKUS_ONE_VENUE_NOT_FOUND_TIMEOUT = 1800`)

---

## 6. Configuração (`config.py`)

```python
RUCKUS_ONE_API_BASE_URL     = "https://api.ruckus.cloud"
RUCKUS_ONE_TOKEN_URL        = "https://ruckus.cloud/oauth2/token"
RUCKUS_ONE_TENANT_ID        = "a7ee34fa73a44eac84ac491de0fbfbad"
RUCKUS_ONE_CLIENT_ID        = "e7b12301c9a03d2126ddf13475520c62"
RUCKUS_ONE_CLIENT_SECRET    = em config.py (gitignored)
RUCKUS_ONE_VENUE_NOT_FOUND_TIMEOUT = 1800  # segundos
```

---

## 7. Módulo `core/ruckus_one_api.py` (implementado)

```python
def get_access_token(token_url, tenant_id, client_id, client_secret) -> str:
    """POST /oauth2/token → Bearer token. ✅ Testado."""

def list_venues(api_base, token) -> list:
    """GET /venues → lista completa. ✅ Testado."""

def find_venue_by_slug(api_base, token, site_slug) -> dict | None:
    """Busca client-side por name == site_slug. ✅ Validado."""

def assign_switch_to_venue(api_base, token, venue_id, switch_id, name=None, description=None) -> dict:
    """POST /venues/{venueId}/switches/{switchId} — serial no path E no body.
    ✅ Validado em produção (resolve SWITCH-10000 e SWITCH-10447)."""

def get_site_slug_from_netbox(device_name, csv_path) -> tuple[str|None, str|None]:
    """Double-check do site_slug no CSV Netbox. Se não achar, operador digita a venue."""
```

> 🗑️ **Removidas na v3.0:** `list_pending_switches` e `wait_for_pending_switch`
> — o fluxo de pending foi abandonado (ver seção 4).

---

## 8. Perguntas em aberto

- [x] ~~Payload correto para o POST de switch~~ → **resolvido:** endpoint
  singular `/switches/{switchId}` com `id` no path E no body (SWITCH-10447)
- [x] ~~Depender de `/switches/pending`~~ → **abandonado:** cadastro direto pelo serial
- [ ] Confirmar sucesso real via **activity API** com o `requestId` (hoje só
  tratamos o `202 Accepted` — a propagação é assíncrona)
- [ ] Há paginação no `GET /venues` e `GET /switches`? (`pageSize`/`limit`/`page`?)
- [ ] Após o cadastro via API, o switch precisa de algum comando CLI extra para
  efetivamente conectar ao cloud Ruckus One?

---

## 9. Arquivos relacionados

| Arquivo | Descrição |
|---|---|
| `netbox/specifications_netbox.md` | Inventário: device_name → site_slug |
| `specifications_smartzone_api.md` | SmartZone REST API (delete do switch) |
| `grid/specifications_grid.md` | Dashboard Grid |
| `specifications.md` | Spec geral da ferramenta |
