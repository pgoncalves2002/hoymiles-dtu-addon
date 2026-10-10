# Changelog

## 1.4.0

- Nova opção `power_zero_threshold_w`: potência total abaixo desse valor (em
  watts) sai como 0 nas respostas da API (`/api/status`, `/api/summary` e
  `POST /api/refresh`). O painel do add-on e os sensores MQTT continuam com o
  valor real; `?raw=1` devolve o valor real também pela API.

## 1.3.0

- Sensores no Home Assistant via MQTT Discovery, usando o Mosquitto do HA
  automaticamente: potência, energia total/hoje, CO₂, painéis online e um
  dispositivo por microinversor (com potência e energia de cada painel).
- **Energia total** pronta para o painel Energia (`total_increasing`), com
  proteção contra quedas do total vindas do DTU.
- Sensores indisponíveis quando o DTU não responde.
- Status do MQTT na aba Coleta. Novas opções `mqtt_enabled` e `mqtt_*`.

## 1.2.0

- Busca do DTU na rede: botão **Procurar** no painel e `POST /api/discover`.
- `auto_discover` (ligado por padrão): se o DTU parar de responder, o add-on
  procura na rede e troca de IP sozinho quando acha um único DTU.
- Erros com motivo provável (`meta.last_error_hint`) e aviso na aba Coleta.
- Leitura mais tolerante ao servidor web simplificado do DTU (resposta sem
  cabeçalhos HTTP ou conexão encerrada cedo).
- Respostas fora do formato do DTU (ex.: página do roteador) passam a ser erro.

## 1.1.0

- Troca do IP do DTU pelo Home Assistant, sem reiniciar o add-on: painel
  **DTU Hoymiles** na barra lateral e rota `POST /api/config`.
- O novo IP é testado antes de ser aplicado e fica salvo na aba
  **Configuração**.
- Painel **DTU Hoymiles** com abas para testar a coleta antes de integrar:
  resumo ao vivo, tabela de painéis por microinversor e testador das rotas
  (resposta JSON, código HTTP, tempo, URL e `curl` para copiar).
- Novas rotas `GET /api/config` e `POST /api/refresh` (leitura imediata).
- `meta.fetch_ms`: tempo de resposta do DTU.

## 1.0.0

- Primeira versão: leitura do `hometable.xml` e API REST em JSON.
