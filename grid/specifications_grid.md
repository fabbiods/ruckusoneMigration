# Specifications — Grid Dashboard
## Projeto Ruckus One - Migração Status

**Documento Grid:** [Projeto Ruckus One - Migração Status](https://grid.adminml.com/d/01KV90903ZKWB3VX6X5C0R8EM1/view)  
**doc_id:** `01KV90903ZKWB3VX6X5C0R8EM1`  
**Arquivo local:** `grid/dashboard_migracao.html`  
**Criado em:** 2026-06-16  

---

## 1. Objetivo

Acompanhar o progresso geral da operação de migração de switches Ruckus ICX 7150 do SmartZone para Ruckus One. Uma linha por switch migrado, atualizada automaticamente pelo pipeline ao final de cada execução bem-sucedida.

---

## 2. Colunas do Dashboard

| Coluna          | Origem                             | Descrição                                        |
|-----------------|------------------------------------|--------------------------------------------------|
| Data/Hora       | Sistema (automático)               | Timestamp da execução                            |
| Site            | SmartZone API (`groupName`)        | Nome do grupo do switch no SmartZone             |
| Switch (Hostname)| **SSH CLI (prompt)** / SmartZone   | Hostname do switch (CLI primário, SZ fallback)   |
| IPv4            | Operador (input da ferramenta)     | IP de gerência informado                         |
| MacAddress      | **SSH CLI (`show version`)** / SZ  | MAC address do switch                            |
| Serial Number   | **SSH CLI (`show version`)** / SZ  | Número de série (usado no cadastro Ruckus One)   |
| Modelo          | **SSH CLI (`show version`)** / SZ  | Ex: ICX7150-48P                                  |
| Firmware Antes  | SSH CLI (`sh flash`)               | Versão do firmware antes da migração             |
| Upgrade         | Pipeline (etapa 3)                 | Sim / Não                                        |
| SZ CLI          | Pipeline (etapa 7)                 | Sucesso / Falha                                  |
| SZ API          | SmartZone REST API (etapa 8)       | Sucesso / Falha / Warning                        |
| Apagado SZ      | SmartZone REST API (DELETE)        | Sim / Não                                        |
| Status Final       | Pipeline                           | Concluído / Concluído com Avisos / Falha         |
| Analista           | Input da ferramenta (obrigatório)  | Usuário MELI responsável pela migração           |
| Controle de Câmbio | Input da ferramenta (obrigatório)  | Número do controle de câmbio                     |
| Observações        | Pipeline (campo livre)             | Notas adicionais                                 |

---

## 3. Como os dados chegam ao dashboard

### Fluxo — envio automático ao final do pipeline

Ao concluir o pipeline (etapa 10), o próprio pipeline envia o registro ao Grid
(`core/grid_api.py::append_migration_record`):

1. Pipeline finaliza as etapas de migração e coleta todos os dados
2. Faz `GET` ao Grid State para ler os registros existentes (+ `updated_at`)
3. Appenda o novo registro ao array `migrations`
4. Faz `PUT` ao Grid State com o array atualizado + `last_update` + `if_updated_at`
5. Emite log de confirmação na interface

### Estrutura do Grid State (JSON)

```json
{
  "last_update": "2026-06-16 14:32:10",
  "migrations": [
    {
      "data_hora":     "2026-06-16 14:30:00",
      "site":          "GRUPO-SMARTZONE-01",  // valor de groupName retornado pela SmartZone API
      "hostname":      "ICX7150-RACK3",
      "ipv4":          "10.50.1.45",
      "mac":           "AA:BB:CC:DD:EE:FF",
      "serial":        "CYS3333XXXX",
      "modelo":        "ICX7150-48P",
      "firmware_antes":"08.0.95g",
      "upgrade":       "Sim",
      "sz_cli":        "Sucesso",
      "sz_api":        "Sucesso",
      "apagado_sz":    "Sim",
      "status_final":     "Concluído",
      "analista":         "facsilva",
      "controle_cambio":  "CC-2026-001",
      "obs":              ""
    }
  ]
}
```

### API Grid State utilizada (CORRIGIDA — validada em produção)

Via SDK no HTML (o que o `dashboard_migracao.html` usa):
```javascript
const { state } = await window.GRID.state.get();   // lê → state.migrations
// ⚠️ get() retorna { state, updated_at } — SEMPRE desestruture "state"
// Passar o retorno direto para acessar .migrations é o bug clássico aqui.
window.GRID.state.set(data, ifUpdatedAt)            // escreve (full overwrite)
```

Via REST (backend Python `core/grid_api.py`):
```
GET  https://grid.melioffice.com/api/v1/documents/{doc_id}/state
     → { "doc_id": "...", "state": {...}, "updated_at": "..." }

PUT  https://grid.melioffice.com/api/v1/documents/{doc_id}/state
     Body: { "state": { ...state completo... }, "if_updated_at": "<updated_at do GET>" }
```

**Autenticação:** token nativo Grid + header de origem:
```
Authorization: Bearer grid_sk_<token>
x-api-source: office
```
> O token é gerado por `POST /api/v1/tokens` (formato `grid_sk_*`) e fica em
> `config.py` (gitignored). Funciona sem VPN, com permissões de owner.

> 🐛 **Bug corrigido (HTTP 405):** o código antigo usava `POST` com body
> `{"data": ...}` num endpoint que só aceita `GET`/`PUT`. O método correto é
> **`PUT`** e o body é `{"state": ..., "if_updated_at": ...}`. Descoberto
> inspecionando o SDK embutido no próprio HTML do dashboard.
>
> ⚠️ `if_updated_at` é o token de **concorrência otimista** — passe o
> `updated_at` retornado pelo GET para não sobrescrever alterações concorrentes
> (passar `""` ignora a checagem).

> 🐛 **Bug corrigido (dashboard em branco):** `window.GRID.state.get()`
> retorna um wrapper `{ state, updated_at }`, **não** o state diretamente.
> O HTML anterior fazia `const state = await window.GRID.state.get()` e
> depois acessava `state.migrations` — que era `undefined` porque estava
> no nível errado. Correção: desestruturar na atribuição:
> ```javascript
> const { state } = await window.GRID.state.get();
> ```
> Confirmado: os dados estavam sendo salvos corretamente no state o tempo todo
> via REST API (`core/grid_api.py`). O problema era exclusivamente na leitura
> pelo SDK no front-end.

---

## 4. Atualização do arquivo HTML

Para atualizar o dashboard com uma nova versão do HTML:

```bash
curl -s -X POST "https://grid.melioffice.com/api/v1/engine/run" \
  -F 'config={"skill_version":"3.6.3","doc_id":"01KV90903ZKWB3VX6X5C0R8EM1","file_new_version":true}' \
  -F "file=@grid/dashboard_migracao.html"
```

> O Grid State **não é afetado** por updates de arquivo — os dados das migrações persistem independente de versões do HTML.

---

## 5. Perguntas em Aberto

- [x] Como identificar o `site` automaticamente? → **`groupName` retornado pela SmartZone API** ao buscar o switch pelo IP
- [ ] Quem mais do time deve ter acesso de leitura ao dashboard?
