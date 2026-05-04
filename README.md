# MVP Distancia CD x Lojas

Aplicacao em Python com Streamlit para cadastrar um Centro de Distribuicao, lojas atendidas e visualizar rotas reais, litros estimados e custo de combustivel.

A interface usa abas para Resumo, Configuracoes, Cadastro do CD, Cadastro de Lojas e Mapa.

## Estrutura do codigo

```text
app.py                    # ponto de entrada do Streamlit
rotas_app/
  constants.py            # caminhos, colunas e configuracoes padrao
  storage.py              # SQLite, migracao dos CSVs e persistencia
  utils.py                # validacoes, normalizacao e formatacoes auxiliares
  addressing.py           # montagem e limpeza de enderecos
  geocoding.py            # busca/cache de latitude e longitude
  store_import.py         # leitura e importacao de planilhas de lojas
  routing.py              # consulta OSRM e cache das rotas
  reporting.py            # calculos de distancia, litros, custo e relatorio
  ui.py                   # telas, formularios, filtros e mapa Streamlit
data/
  sistema_rotas.db        # banco SQLite principal
```

## Melhorias desta versao

- Codigo reorganizado em modulos por responsabilidade.
- Banco de dados SQLite em `data/sistema_rotas.db`.
- Cache das rotas calculadas no SQLite.
- O sistema nao recalcula rotas automaticamente a cada atualizacao da tela.
- O mapa recalcula somente a rota da loja destino selecionada.
- Validacao melhorada de latitude e longitude.
- Bloqueio de coordenadas `0,0`, que geralmente indicam erro de preenchimento.
- Avisos quando a coordenada parece fora da faixa comum do Brasil.
- Mapa simplificado por destino: selecione uma unica loja e visualize somente a rota CD x loja escolhida.
- Filtros no cadastro de lojas por busca, coordenadas com aviso e ordenacao.
- Importacao de lojas por planilha usando endereco, CEP e numero, sem obrigar latitude/longitude.
- Busca automatica de latitude e longitude pelo endereco, com cache no SQLite.
- Lojas sem coordenada ficam salvas como pendentes para correcao manual.

## Cadastro de lojas por planilha

Use uma planilha Excel ou CSV com colunas como:

```text
Filial
CEP
Endereco
Numero
Bairro
Cidade
UF
```

O sistema usa `Filial` como nome da loja, monta o endereco completo e tenta buscar automaticamente latitude e longitude.

Se a coluna Endereco ja vier com o numero, o sistema nao repete o numero. Exemplo: `Rua X, 149` + `Numero 149` continua como `Rua X, 149`.

Depois da importacao, a tela mostra um resumo com linhas lidas, lojas com coordenada, lojas pendentes, lojas que ja existiam e linhas nao importadas.

## Cadastro manual

No cadastro manual de loja, latitude e longitude sao opcionais. Se ficarem em branco, o sistema tenta encontrar as coordenadas automaticamente pelo endereco.

## Mapa por destino

Na aba **Mapa**, o sistema desenha somente a rota da loja selecionada.

Fluxo recomendado:

```text
1. Busque ou selecione a loja destino
2. Clique em Recalcular rota do destino selecionado
3. Visualize a rota entre o CD e essa loja
```

## Calculo de rotas

- O sistema usa somente rota real via OSRM.
- Nao existe fallback por linha reta.
- Se uma rota falhar, a loja aparece como erro e o custo nao e calculado para ela.
- Quando CD e loja continuam com as mesmas coordenadas, a rota salva e reutilizada como cache.

## Geocodificacao

- A busca de latitude e longitude usa endereco completo e salva resultado em cache no SQLite.
- Se o endereco nao for encontrado, a loja sera salva como pendente de coordenada.
- Lojas pendentes aparecem no Cadastro de Lojas, mas nao aparecem no Mapa ate terem latitude/longitude validas.
- Para maior precisao, mantenha CEP, numero, cidade e UF preenchidos.

## Migracao dos CSVs antigos

Ao abrir o app pela primeira vez, se ainda nao existir o banco SQLite, o sistema tenta importar automaticamente os dados antigos dos arquivos CSV da pasta `data/`.

Depois disso, a base principal passa a ser:

```text
data/sistema_rotas.db
```

## Como executar

```powershell
python -m pip install -r requirements.txt
streamlit run app.py
```
