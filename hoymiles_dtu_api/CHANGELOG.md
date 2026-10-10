# Changelog

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
