# Hoymiles DTU Add-ons

Repositório de add-ons do Home Assistant com o **Hoymiles DTU API**: lê os dados
do DTU Hoymiles na rede local e os expõe em uma API REST (JSON).

## Instalação

1. No Home Assistant, abra **Configurações → Add-ons → Loja de add-ons**.
2. No menu ⋮, escolha **Repositórios** e adicione:
   `https://github.com/SEU_USUARIO/hoymiles-dtu-addon`
3. Recarregue a loja, instale **Hoymiles DTU API**, ajuste o IP do DTU na aba
   **Configuração** e inicie.

A API fica em `http://<ip-do-home-assistant>:8099`. Rotas e detalhes em
[hoymiles_dtu_api/README.md](hoymiles_dtu_api/README.md).

## Atualizações

O Home Assistant só oferece atualização quando o campo `version` de
`hoymiles_dtu_api/config.yaml` aumenta. Ao mudar o código, suba a versão no
mesmo commit.
