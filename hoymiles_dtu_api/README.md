# Hoymiles DTU API

Add-on do Home Assistant que lê os dados do DTU Hoymiles na rede local e os
entrega em JSON para outras aplicações.

O DTU não tem API documentada, mas a página `home.htm` busca tudo em
`http://<ip-do-dtu>/hometable.xml` (texto separado por `;`, sem login). O add-on
consulta esse endereço a cada poucos segundos, guarda a última leitura em
memória e a serve pela porta 8099.

## Instalação

1. Em **Configurações → Add-ons → Loja de add-ons**, abra o menu ⋮ e escolha
   **Repositórios**.
2. Adicione o endereço deste repositório no GitHub e recarregue a loja.
3. Instale **Hoymiles DTU API**, confira a aba **Configuração** e inicie.

## Configuração

| Opção           | Padrão         | Descrição                                   |
|-----------------|----------------|---------------------------------------------|
| `dtu_host`      | `192.168.0.34` | IP (ou IP:porta) do DTU                     |
| `poll_interval` | `5`            | Segundos entre leituras do DTU              |
| `timeout`       | `5`            | Tempo máximo de espera pela resposta do DTU |

A porta externa (8099) pode ser trocada na seção **Rede** do add-on.

## Endpoints

Base: `http://<ip-do-home-assistant>:8099`

| Rota                | Retorno                                              |
|---------------------|------------------------------------------------------|
| `/api/status`       | Leitura completa: resumo, painéis e microinversores  |
| `/api/summary`      | Totais do sistema                                    |
| `/api/panels`       | Lista de painéis                                     |
| `/api/panels/<id>`  | Um painel, ex.: `/api/panels/100000000001-1`         |
| `/api/inverters`    | Painéis agrupados por microinversor                  |
| `/health`           | Estado do coletor                                    |

Exemplo de `/api/summary`:

```json
{
  "meta": {
    "dtu_host": "192.168.0.34",
    "poll_interval_s": 5,
    "fetched_at": "2026-10-09T23:10:02+00:00",
    "age_s": 1.8,
    "stale": false,
    "last_error": null
  },
  "summary": {
    "p_total_kw": 0.0,
    "e_total_kwh": 45976.65,
    "e_today_kwh": 28.14,
    "co2_saved_kg": 22.09,
    "panel_count": 20,
    "panels_online": 0,
    "panels_offline": 20,
    "server_status": "YC",
    "server_address": "123.60.74.212",
    "server_port": 10017
  }
}
```

Cada painel traz `id`, `inverter_serial`, `port`, `v_pv` (V), `v_grid` (V),
`f_grid` (Hz), `p_grid` (W), `e_today_wh` (Wh), `temperature` (°C),
`last_update`, `online` e `raw` (os textos originais do DTU).

## Comportamento

- Se o DTU parar de responder, a API continua servindo a última leitura boa;
  `meta.stale` vira `true` e `meta.last_error` mostra o motivo.
- Antes da primeira leitura, as rotas `/api/*` respondem 503.
- `last_update` é o horário do próprio DTU (sem fuso) e fica `null` quando o
  painel está sem leitura (à noite, por exemplo).
- `online` é derivado: verdadeiro quando a tensão de rede do painel é maior
  que zero.
- CORS liberado (`Access-Control-Allow-Origin: *`) e sem autenticação: use
  apenas na rede local.

## Rodar fora do Home Assistant

```bash
DTU_HOST=192.168.0.34 POLL_INTERVAL=5 PORT=8099 python3 server.py
```
