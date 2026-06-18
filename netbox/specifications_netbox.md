# Specifications — Netbox: Inventário de Switches Ruckus
## Associação device_name → site_slug para migração ao Ruckus One

**Arquivo fonte:** `netbox-ruckus-roles-models - Devices.csv`  
**Data de extração:** 2026-06-18  
**Origem:** Netbox interno (http://netbox.adminml.com)

---

## 1. Objetivo

Fornecer o mapeamento entre cada switch Ruckus (`device_name`) e sua localidade (`site_slug`) para que, durante ou após a migração SmartZone → Ruckus One, o device possa ser associado à **venue** (localidade) correta na plataforma Ruckus One.

---

## 2. Estrutura do CSV

| Coluna | Exemplo | Descrição |
|---|---|---|
| `site_id` | `178` | ID numérico do site no Netbox |
| `site_slug` | `aguascalientes` | Slug único do site — **chave de associação ao Ruckus One** |
| `site_name` | `Aguascalientes` | Nome legível do site |
| `device_id` | `34567` | ID interno do device no Netbox |
| `device_name` | `MXSCAGACAM001-1` | Hostname configurado no switch — **chave de associação via SSH** |
| `role_name` | `Access Cameras` | Função do device na rede |
| `role_slug` | `access-cameras` | Slug da função |
| `model` | `ICX7150-48PF` | Modelo do hardware |
| `model_slug` | `icx7150-48pf` | Slug do modelo |
| `manufacturer` | `Ruckus` | Fabricante (sempre Ruckus neste dataset) |
| `status` | `Ativo` | Status no Netbox |
| `url` | `http://netbox.adminml.com/...` | Link direto ao device no Netbox |

---

## 3. Resumo do Dataset

| Métrica | Valor |
|---|---|
| **Total de devices** | 2.785 |
| **Sites únicos** | 292 |
| **ICX7150 (alvo da migração)** | **1.896** |
| **Outros modelos (ICX7450, ICX8200 etc.)** | 889 |

---

## 4. Devices Alvo da Migração — ICX7150

Total: **1.896 devices** distribuídos em 18 variantes de modelo.

### Modelos ICX7150 presentes

| Modelo | Quantidade |
|---|---|
| ICX7150-48PF | 557 |
| ICX7150-48-POEF | 487 |
| ICX7150-24-POE | 265 |
| ICX7150-C08-POE | 146 |
| ICX7150-C12P | 98 |
| ICX7150-24P | 52 |
| ICX7150-C08P | 49 |
| ICX7150-48P | 40 |
| ICX7150-48-POE | 39 |
| ICX7150-C12-POE | 37 |
| ICX7150-C12 | 30 |
| ICX7150-48Z-HPOE | 30 |
| ICX7150-24P-4X10G | 21 |
| ICX7150-24F | 20 |
| ICX7150-C12-2X1G | 14 |
| ICX7150-C08 | 1 |
| ICX7150-5ZP | 5 |
| ICX7150-24PF-4X10G | 5 |

---

## 5. Distribuição por País

Identificado pelo prefixo de 2 letras do `device_name`:

| Prefixo | País | ICX7150 |
|---|---|---|
| `BR` | Brasil | 1.130 |
| `MX` | México | 333 |
| `FE` | — | 203 |
| `CL` | Chile | 117 |
| `FM` | — | 41 |
| `CO` | Colômbia | 34 |
| `AR` | Argentina | 25 |
| `FJ` | — | 7 |
| `UY` | Uruguai | 2 |
| `AE` | — | 2 |

---

## 6. Distribuição por Role

| Role | Quantidade (total dataset) |
|---|---|
| Access Cameras | 1.917 |
| Core Cameras | 328 |
| Access Switch | 304 |
| Distribution Cameras | 88 |
| Core Switch | 78 |
| Inventory | 36 |
| Switch | 29 |
| Management Switch | 4 |
| Distribution Switch | 2 |

---

## 7. Distribuição por Status

| Status | Quantidade |
|---|---|
| **Ativo** | 2.251 |
| **Offline** | 472 |
| Planejado | 41 |
| Em Descomissionamento | 16 |
| Inventário | 6 |

> **Atenção:** 472 devices estão com status `Offline` no Netbox. Esses devices podem ou não estar acessíveis via SSH — devem ser verificados individualmente durante a migração.

---

## 8. Como Usar no Pipeline de Migração

> ℹ️ **Papel do CSV na v3.0:** o CSV é usado como **double-check** do `site_slug`.
> O pipeline busca o `device_name` (hostname coletado via CLI na etapa 1) no CSV
> para sugerir a venue. Se **não encontrar**, o operador **digita o nome da venue**
> manualmente na interface (etapa 9). A confirmação/seleção final da venue é sempre
> do operador. Ver `specifications_ruckusone_api.md` § 4.

### Lookup por device_name (pós-SSH)

Após conectar via SSH e obter o hostname do switch, buscar no CSV pelo `device_name` para obter o `site_slug` correspondente:

```python
import csv

def get_site_slug(device_name: str, csv_path: str) -> str | None:
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["device_name"] == device_name:
                return row["site_slug"]
    return None
```

### Lookup por IP (via Netbox API)

O CSV não contém IPs de gerência. Para obter o IP → device_name → site_slug:
- Consultar a API do Netbox: `GET http://netbox.adminml.com/api/dcim/devices/?name=<hostname>`
- Ou `GET http://netbox.adminml.com/api/dcim/ip-addresses/?device=<hostname>`

### Uso no dashboard Grid

O `site_slug` obtido via este CSV é o valor a ser registrado na coluna **Site** do dashboard Grid (`grid/dashboard_migracao.html`), complementando o `groupName` retornado pela SmartZone API.

---

## 9. Convenção de Nomenclatura dos Devices

Padrão observado: `{PAÍS}{TIPO}{CIDADE}{FUNÇÃO}{SEQ}-{STACK}`

Exemplo: `BRMACUCRP001-1`
- `BR` → Brasil
- `MA` → tipo de local (Mercado)
- `CU` → cidade (Cuiabá)
- `CRP` → função (Core Router/Switch)
- `001` → sequência
- `1` → número do stack

---

## 10. Arquivos Relacionados

| Arquivo | Descrição |
|---|---|
| `netbox/netbox-ruckus-roles-models - Devices.csv` | Dados completos — não commitar (dados internos) |
| `specifications.md` | Spec geral da ferramenta de migração |
| `specifications_smartzone_api.md` | Spec da API REST do SmartZone |
| `grid/specifications_grid.md` | Spec do dashboard Grid |
