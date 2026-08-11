# O motor de rastreamento

Documento de projeto do *web crawler* genérico — a peça que dá ao *pipeline* a flexibilidade de fonte exigida pelo **objetivo específico 1** da IC (*"construir algoritmos automatizados para a navegação e coleta de arquivos documentais em repositórios técnicos e institucionais"*).

---

## Por que um crawler genérico, e não só clientes de API

Duas razões, e a primeira é a que importa cientificamente.

**1. Diversidade de fonte é requisito de validade experimental.**
O objetivo final da IC é aferir se um LLM classifica ConOps de forma confiável (Etapa 5). Se o corpus vier majoritariamente de um repositório, o classificador aprende a reconhecer *o formato daquele repositório*, não *o que é um ConOps*. A métrica final ficaria inflada e a conclusão não se sustentaria fora do acervo de origem. Um corpus que atravessa NASA, ESA, DoD, FAA e projetos europeus é o que torna o resultado generalizável — e a maioria dessas fontes **não** tem API.

**2. O projeto prevê navegação, não apenas consumo de API.**
Dois clientes de API não são "algoritmos de navegação em repositórios institucionais". E quando o orientador ou a Embraer indicarem um repositório novo, adicionar uma fonte precisa custar um arquivo YAML — não um módulo Python.

**Onde há interface estruturada, ela continua sendo preferida.** O NTRS entrega 500 registros com metadado completo por requisição; rastrear o mesmo acervo em HTML custaria milhares. A regra é: *API quando existe, crawler sempre que não*.

---

## Arquitetura

```
config/sources/*.yaml  ──►  SourceSpec
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   API dedicada        Protocolo               CRAWLER GENÉRICO
   (NTRS)              (OAI-PMH, sitemap,      (motor deste doc)
                        bulk CSV)
        └─────────────────────┼─────────────────────┘
                              ▼
                      DocumentRecord  ──►  pré-filtro  ──►  store
```

### Ciclo do motor

```
sementes + sitemap ──► fronteira de URLs (ordenada por prioridade)
                              │
                              ▼
                     visitar página
                        │        │
      extrair links ◄───┘        └──► extrair metadados do <head>
        │  (âncora + contexto)              (citation_*, DC.*, JSON-LD)
        ▼
   pontuar cada link ──► enfileirar
```

### Componentes

| Módulo | Responsabilidade |
|---|---|
| [spec.py](../crawler/engine/spec.py) | contrato YAML da fonte: sementes, escopo, estratégia, orçamento, papel |
| [urlfrontier.py](../crawler/engine/urlfrontier.py) | fila de URLs em SQLite — prioridade, profundidade, retomada |
| [linkscorer.py](../crawler/engine/linkscorer.py) | pontuação semântica de links (rastreamento focado) |
| [extract.py](../crawler/engine/extract.py) | links com âncora/contexto + metadados estruturados |
| [sitemap.py](../crawler/engine/sitemap.py) | descoberta por sitemap, com orçamento e desistência |
| [traps.py](../crawler/engine/traps.py) | canonicalização e detecção de armadilhas |
| [renderer.py](../crawler/engine/renderer.py) | Playwright, opt-in por fonte |
| [crawler.py](../crawler/engine/crawler.py) | o laço que junta tudo |

---

## Rastreamento focado — o *"Web Crawling semântico"*

A introdução do projeto fala em *Web Crawling semântico*. A implementação literal disso é o **focused crawling** (CHAKRABARTI et al., 1999): em vez de varrer o site em largura, estima-se a relevância de cada link **antes de segui-lo** e expande-se primeiro o que promete mais.

Três fontes de evidência, em ordem de confiabilidade:

| Evidência | Peso | Por quê |
|---|---|---|
| **Tokens da URL** | 1.0 | `/uam-conops-2.0.pdf` diz mais que qualquer âncora — quem nomeia arquivo raramente mente |
| **Texto da âncora** | 0.7 | "Concept of Operations v2" é explícito; "clique aqui" não informa nada |
| **Contexto ao redor** | 0.25 | salva quando a âncora é pobre: um "PDF" dentro de um parágrafo sobre concepção operacional vale mais que o mesmo "PDF" numa lista de atas |

Mais três ajustes: bônus para documento (é o alvo, não um passo), penalidade por profundidade (impede afundar num ramo) e penalidade para ruído estrutural (`/login`, `/newsroom`, `/careers`).

O pontuador reusa o **mesmo `lexicon.yaml`** que decide a faixa do documento — consulta e triagem não divergem.

### As duas estratégias compartilham todo o código

A única diferença entre `focused` e `bfs` é o `LinkScorer` estar ligado. Com ele desligado toda prioridade é 0, e a ordenação da fronteira (`priority DESC, depth ASC`) degenera exatamente em busca em largura. Isso é deliberado: **comparar as duas só tem valor se forem idênticas em tudo o mais**.

```bash
python -m crawler.cli experiment rosap_crawl --max-pages 60
```

Métrica: `harvest_rate` = documentos relevantes encontrados ÷ páginas HTML baixadas. "Relevante" aqui é o tier do léxico (indício de metadado), não verificação de que o documento é de fato um ConOps — não há gold set nesta etapa para confirmar isso. Tratar como sinal de engenharia (vale a pena pontuar links?), não como métrica de precisão.

O sitemap é desligado no experimento, sempre: ele entrega URLs sem que nenhum link seja seguido, e as duas estratégias receberiam a mesma lista. O que está sob teste é a **ordem de expansão da fronteira**, e isso só aparece navegando.

### Resultado medido — FAA, 2026-08-04

Mesmo orçamento (79 páginas), mesma semente, sitemap desligado:

| Métrica | BFS | Focado |
|---|---:|---:|
| Páginas baixadas | 79 | 79 |
| Links vistos | 15.408 | 15.507 |
| Documentos encontrados | 256 | 292 |
| **Documentos relevantes** | **2** | **4** |
| **`harvest_rate`** | **0,0253** | **0,0506** |

**2,0× mais candidatos por página.** A curva de descoberta mostra as duas estratégias empatadas até ~49 páginas e divergindo depois — o crawler focado encontra 255 documentos até a página 73, contra 159 do BFS.

**Duas ressalvas honestas, a registrar em qualquer relatório que cite este número:**
1. Amostra pequena — 2 contra 4 documentos relevantes não sustenta afirmação estatística; repetir com orçamento de várias centenas de páginas e em mais de uma fonte antes de tratar como resultado forte.
2. Não é medida de precisão — "relevante" é o tier do léxico sobre o metadado, não confirmação de que o documento é um ConOps de verdade. O número diz que o rastreamento focado acha mais *candidatos* por página, o que já é útil operacionalmente (completude sob orçamento limitado), mas não é prova de que a estratégia acerta mais.

Vale também o contraexemplo: no teste controlado, quando o ramo relevante é o **primeiro** link da página, o BFS empata com o focado. O ganho aparece quando o conteúdo relevante está enterrado — que é o caso real dos portais institucionais, e por isso a FAA foi a fonte escolhida.

### Evolução prevista para a Etapa 4

Quando a Etapa 4 produzir os *embeddings* BERT, o `LinkScorer` pode trocar a pontuação léxica por **similaridade vetorial** entre o contexto do link e um protótipo de ConOps, sem tocar no motor — a interface `score()` foi desenhada para essa substituição. Isso fecha um ciclo entre as etapas e reaproveita o modelo em duas frentes.

---

## Decisões de projeto que valem registro

**Escopo é obrigatório, não opcional.** Sem `allow_hosts` e limites de profundidade e orçamento, "rastrear faa.gov" vira rastrear a internet: um link para fora leva a um domínio que leva a outro.

**Metadados estruturados são o que equiparam HTML a API.** Repositórios institucionais quase sempre emitem `citation_*` (Highwire, o padrão do Google Scholar), `DC.*` (Dublin Core), OpenGraph ou JSON-LD no `<head>`. Ler essas tags é o que permite a uma fonte HTML entregar título, autores, data e resumo — sem isso o crawler traz um PDF sem procedência.

**Duas identidades distintas.** Descoberta = `(source, source_id)`; conteúdo = **SHA-256**. URL nunca é chave: no Liferay da ESA ela carrega `uuid` + *timestamp* e muda a cada reedição.

**A relevância só pode ser julgada depois do metadado.** Julgá-la na descoberta subestima tudo: no ROSA P o arquivo se chama `dot_78914_DS1.pdf` e não carrega sinal nenhum — o título está na landing page. Foi um bug real, corrigido e coberto por teste.

**Renderização JS é opt-in.** Custa ordens de grandeza mais que um GET. Das fontes da lista, só o ESA EOF precisa — verificado: o HTML servido tem zero links `.pdf`.

---

## Adicionando uma fonte

Um arquivo em `config/sources/`:

```yaml
name: minha_fonte
kind: crawl
strategy: focused
seeds:
  - https://exemplo.org/documentos/
scope:
  allow_hosts: [exemplo.org]
  allow_paths: ['/documentos/', '/relatorios/']
  deny_paths: ['/login', '/busca']
  max_depth: 3
  max_pages: 400
document_extensions: ['.pdf']
document_patterns: ['/download/\d+']   # quando a URL não tem extensão
sitemap: auto
render: false
```

Depois:

```bash
python -m crawler.cli discover minha_fonte --max-pages 50
python -m crawler.cli harvest --tier strong weak
```

Nenhum código Python é necessário.
