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
4. Ative **Mostrar na barra lateral** para ter o painel **DTU Hoymiles**
   sempre à mão.

## Painel na barra lateral

Serve para conferir a coleta antes de ligar a API no seu sistema:

- **Coleta**: potência, energia de hoje e total, painéis online, estado da
  coleta, idade da última leitura, tempo de resposta do DTU e último erro.
  Atualiza a cada 5 s; o botão **Ler agora** força uma leitura na hora.
- **Painéis**: tabela com todos os painéis, agrupados por microinversor.
- **Testar API**: escolha uma rota, veja a resposta JSON, o código HTTP e o
  tempo, e copie a URL (ou o comando `curl`) que o seu sistema deve chamar.
- **IP do DTU**: troca o IP consultado (veja abaixo).

O mesmo painel abre em `http://<ip-do-home-assistant>:8099/` no navegador.

## Configuração

| Opção           | Padrão         | Descrição                                   |
|-----------------|----------------|---------------------------------------------|
| `dtu_host`      | `192.168.0.34` | IP (ou IP:porta) do DTU — dá para trocar sem reiniciar, veja abaixo |
| `poll_interval` | `5`            | Segundos entre leituras do DTU              |
| `timeout`       | `5`            | Tempo máximo de espera pela resposta do DTU |

A porta externa (8099) pode ser trocada na seção **Rede** do add-on.

## Trocar o IP do DTU pelo Home Assistant

Se o DTU mudar de IP, não é preciso editar a configuração nem reiniciar o
add-on. Há três jeitos, todos com o mesmo efeito:

1. **Painel do add-on**: abra **DTU Hoymiles** na barra lateral (ou
   **Abrir interface web** na página do add-on), vá na aba **IP do DTU**,
   digite o novo IP e clique em **Salvar**.
2. **API**: `POST /api/config` com `{"dtu_host": "192.168.0.40"}`.
3. **Dashboard/automação**: com o `rest_command` do exemplo abaixo.

Antes de trocar, o add-on testa o novo endereço. Se o DTU não responder ali,
a troca é recusada (HTTP 502) e a coleta continua no IP antigo — para trocar
assim mesmo, marque **Salvar mesmo se o DTU não responder** no painel ou envie
`"force": true`. Quando a troca é aceita, a coleta passa para o novo IP na hora
e o valor é gravado na aba **Configuração**, valendo também depois de reiniciar.

Exemplo de `configuration.yaml` para trocar o IP por um campo no dashboard:

```yaml
input_text:
  dtu_host:
    name: IP do DTU
    max: 64

rest_command:
  dtu_set_host:
    url: http://localhost:8099/api/config
    method: POST
    content_type: application/json
    payload: '{"dtu_host": "{{ host }}"}'

automation:
  - alias: Aplicar novo IP do DTU
    triggers:
      - trigger: state
        entity_id: input_text.dtu_host
    actions:
      - action: rest_command.dtu_set_host
        data:
          host: "{{ trigger.to_state.state }}"
```

Coloque `input_text.dtu_host` em um cartão de entidades; ao editar o valor, a
automação aplica o novo IP. (Se você mudou a porta na seção **Rede**, ajuste
a `url`.)

## Endpoints

Base: `http://<ip-do-home-assistant>:8099`

| Rota                | Retorno                                              |
|---------------------|------------------------------------------------------|
| `/api/status`       | Leitura completa: resumo, painéis e microinversores  |
| `/api/summary`      | Totais do sistema                                    |
| `/api/panels`       | Lista de painéis                                     |
| `/api/panels/<id>`  | Um painel, ex.: `/api/panels/100000000001-1`         |
| `/api/inverters`    | Painéis agrupados por microinversor                  |
| `/api/config`       | `GET`: configuração atual · `POST`: troca o IP do DTU |
| `/api/refresh`      | `POST`: lê o DTU na hora e devolve a leitura completa |
| `/health`           | Estado do coletor                                    |

Exemplo de `/api/summary`:

```json
{
  "meta": {
    "dtu_host": "192.168.0.34",
    "poll_interval_s": 5,
    "fetched_at": "2026-10-09T23:10:02+00:00",
    "age_s": 1.8,
    "fetch_ms": 140,
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

Resposta de `POST /api/config` quando a troca é aceita:

```json
{
  "ok": true,
  "dtu_host": "192.168.0.40",
  "reachable": true,
  "test_error": null,
  "persisted": true,
  "persist_error": null
}
```

`persisted: false` indica que o IP foi trocado, mas não pôde ser gravado nas
opções (por exemplo, rodando fora do Home Assistant): vale até o add-on
reiniciar.

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
  apenas na rede local. Isso vale também para `POST /api/config` — qualquer
  aparelho da rede que alcance a porta 8099 pode trocar o IP do DTU. O painel
  da barra lateral passa pelo login do Home Assistant.
- Em `/`, navegadores recebem o painel; clientes que não pedem HTML recebem
  a lista de rotas em JSON.
- `meta.fetch_ms` é quanto o DTU levou para responder na última leitura boa.

## Rodar fora do Home Assistant

```bash
DTU_HOST=192.168.0.34 POLL_INTERVAL=5 PORT=8099 python3 server.py
```
