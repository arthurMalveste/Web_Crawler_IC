# Achados de inspeção das APIs — verificação direta

**Data da verificação:** 2026-08-04
**Método:** requisições reais aos serviços, a partir de rede residencial brasileira.
**Por que este documento existe:** o plano da Etapa 1 partiu de documentação informal e de inspeção por navegador. Ao implementar os adaptadores, várias suposições não sobreviveram ao contato com a API real. Cada divergência abaixo foi confirmada empiricamente e está travada por teste automatizado.

---

## 1. NTRS — três divergências que quebrariam a coleta

### 1.1 O parâmetro de paginação é `page.from`, não `from` ⚠️ crítico

O plano previa `from` como *offset*. A API **ignora silenciosamente** esse parâmetro e devolve sempre a primeira página.

```
GET /api/citations/search?q="concept of operations"&page.size=5&from=5
  -> ['64968144929743', 20200001712, 20150000827, 20200004826, 20200004918]   (página 1)

GET /api/citations/search?q="concept of operations"&page.size=5&page.from=5
  -> ['23889417853961', 20170007262, 20200004880, 20205008200, 20170012394]   (página 2)
```

**Consequência se não corrigido:** laço infinito colhendo os mesmos 500 registros, sem erro nenhum em log. O `total` reportado continuaria alto, dando a impressão de que a coleta funcionou.

Travado por `tests/test_adapters.py::TestNTRSPaginacao::test_usa_page_from_e_nao_from`.

### 1.2 Parâmetros desconhecidos são ignorados sem erro

```
GET ...?q="concept of operations"&zzzz=1   ->  total = 696   (idêntico ao sem zzzz)
```

**Consequência metodológica:** um HTTP 200 **não prova** que o filtro foi aplicado. Todo filtro usado no adaptador foi validado observando mudança no `stats.total`:

| Parâmetro | Efeito medido | Veredito |
|---|---|---|
| `q="frase"` | 696 (com aspas) vs 3.240 (sem) | ✅ funciona; aspas fazem busca por frase |
| `published.gte` | 696 → 268 | ✅ funciona |
| `distribution=PUBLIC` | reduz | ✅ funciona |
| `disseminated=DOCUMENT_AND_METADATA` | 696 → 636 | ✅ funciona |
| `page.from` | muda a página | ✅ funciona |
| `title="frase"` | **328.986** resultados | ❌ **não filtra por frase no título** |
| `sort.field` / `sort.order` | ordem inalterada | ❌ **ignorados** |

### 1.3 `title=` e `sort.*` não funcionam como o plano supunha

- `title="concept of operations"` devolveu 328.986 resultados — não é filtro de frase. **O sinal de título tem de ser calculado localmente**, no pré-filtro léxico, e é o que fazemos.
- `sort.field=id&sort.order=asc` não altera a ordem: a API ordena sempre por relevância. **Não há ordenação estável para paginar.** A robustez vem do dedupe por `(source, source_id)` no frontier, não de ordenação.

### 1.4 Teto de 10.000 confirmado — e é sobre a soma

O limite não é sobre `page.from` isolado, e sim sobre `page.from + page.size`:

```
page.from=9990, page.size=10  -> HTTP 200   (soma = 10000)
page.from=10000, page.size=10 -> HTTP 400   (soma = 10010)
```

O adaptador reduz a última página para nunca ultrapassar a soma, e particiona o intervalo de anos recursivamente até cada partição caber sob o teto. **Sem isso a cauda de qualquer consulta com >10k resultados se perde em silêncio** — perda de recall que não aparece em log de erro.

### 1.5 `page.size` aceita muito mais que os 100 documentados

`page.size=2000` retornou 2000 registros. O adaptador usa **500**: reduz o número de requisições em 5× em relação a 100, sem depender de comportamento extremo não documentado.

### 1.6 Confirmado — `links.fulltext` existe e é texto real ✅

O achado de maior impacto do plano **se confirma**:

```json
"links": {
  "original": "/api/citations/20200001712/downloads/20200001712.pdf",
  "pdf":      "/api/citations/20200001712/downloads/20200001712.pdf",
  "fulltext": "/api/citations/20200001712/downloads/20200001712.txt"
}
```

O `.txt` baixa 33 KB de texto já extraído e legível. **Na primeira coleta real, 31 de 40 documentos (78%) vieram com texto pronto** — esses dispensam a Etapa 2 inteiramente.

Detalhe de implementação: os links vêm como **caminhos relativos** (`/api/...`), não URLs absolutas.

### 1.7 Rate limit é publicado em headers — melhor que estimar

```
X-RateLimit-Limit: 500
X-RateLimit-Remaining: 497
X-RateLimit-Reset: 1785848155      (epoch)
```

O fetcher lê esses headers e pausa sozinho quando restam ≤ 5 requisições, em vez de confiar apenas no intervalo fixo do YAML. Os downloads também trazem `ETag` e `Last-Modified`, então as reexecuções usam requisição condicional.

### 1.8 Campos de metadado confirmados

`abstract`, `center`, `subjectCategories`, `keywords`, `stiType`, `distribution`, `exportControl`, `cui`, `sensitiveInformation`, `copyright`, `otherReportNumbers`, `publications`.

`exportControl` tem a forma `{"itar": "NO", "ear": "NO", "isExportControl": "NO", "eccnNumber": ""}` — os três são checados.

**Atenção:** a resposta de busca inclui registros do índice `chorus` (metadados de editoras, sem arquivo, sem `abstract`, sem `center`). O filtro `disseminated=DOCUMENT_AND_METADATA` remove a maior parte, mas o adaptador tolera esses campos ausentes.

---

## 2. ROSA P — duas armadilhas de data

### 2.1 Granularidade exige timestamp completo

`Identify` declara `granularity = YYYY-MM-DDThh:mm:ssZ`. A forma curta, aceita pela maioria dos repositórios OAI-PMH, **aqui é erro**:

```
from=2025-01-01            -> <error code="badArgument">Error parsing date.</error>
from=2025-01-01T00:00:00Z  -> HTTP 200
```

### 2.2 O `datestamp` é data de modificação, não de publicação ⚠️

Todos os registros do acervo trazem datestamps de julho/agosto de 2026 — resultado de reindexação, não de publicação recente. Um documento de 2014 aparece com datestamp de 2026-08-03.

**Consequência:** `from`/`until` servem para **coleta incremental** (o que mudou desde a última execução) e **jamais** para recortar por período do documento. Para o período real existe `dc:date`. Confundir os dois produziria um recorte temporal sem sentido.

### 2.3 Demais confirmações

- `ListSets` vem **vazio** — não há partição por coleção a explorar.
- Padrão de PDF confirmado: `/view/{col}/{num}/{col}_{num}_DS1.pdf` devolve `application/pdf` com magic bytes `%PDF-1.7`.
- `dc:rights.accessRights` = `Public Domain` em todo o acervo inspecionado.
- Sem busca textual no protocolo: **é preciso colher todo o metadado e filtrar localmente**. É barato — 4.000 registros em ~40 s.
- Com `resumptionToken`, nenhum outro parâmetro pode ser enviado junto (o protocolo proíbe; o servidor responde `badArgument`).

---

## 3. DTIC — o diagnóstico do plano precisa ser corrigido

O plano registrou "bloqueio anti-bot ativo". A verificação mostra algo diferente:

```
GET https://apps.dtic.mil/sitemap.xml
  User-Agent: UNICAMP-FT-ConOpsBot/0.1   -> HTTP 403
  User-Agent: Mozilla/5.0 (Chrome)       -> HTTP 307 -> https://apps.dtic.mil/landingpage/maint.html
```

Seguindo o redirecionamento chega-se a uma página **"Under Maintenance"**.

**Há duas coisas distintas acontecendo, e o plano fundiu as duas:**

1. **Filtragem por User-Agent** — real: o UA identificado toma 403 enquanto o de navegador não. Confirma a necessidade de `browser_ua: true` para este domínio.
2. **Indisponibilidade do host** — o acervo inteiro está fora do ar por manutenção, independentemente do cliente.

**Isso muda a avaliação de risco do cronograma.** O plano alocou as semanas 8–9 e folga extra para "resolver o anti-bot" com Playwright. Playwright não resolve uma página de manutenção: nenhuma técnica de cliente contorna um servidor desligado. A ação correta é **reverificar a disponibilidade periodicamente** antes de investir em contorno técnico — e manter NTRS + ROSA P como sustentação do corpus, como o próprio plano já previa.

O que **permanece válido** do plano: o `sitemap.xml` é autorizado explicitamente pela DTIC, os padrões de URL levantados, a restrição a *Distribution Statement A* e a blocklist do R&E Gateway.

---

## 3-bis. Rastreamento HTML — achados de campo

Verificações feitas ao implementar o motor de *crawling* genérico.

### Bloqueio por WAF derruba a premissa do "bot acadêmico identificado" ⚠️

O plano original supunha que *"administradores de repositórios públicos toleram bots identificados de universidades e bloqueiam bots anônimos"*. Para sites `.gov` atrás de WAF, o comportamento é **exatamente o inverso**:

| Alvo | UA identificado | UA de navegador | UA híbrido (navegador + contato) |
|---|---|---|---|
| `www.faa.gov/*` | **403** | 200 | **403** |
| `www.faa.gov/robots.txt` | **403** | 200 | **403** |
| `apps.dtic.mil/sitemap.xml` | **403** | 307 → manutenção | **403** |

O bloqueio vem do Akamai (a página de erro aponta para `errors.edgesuite.net`) e casa a **string exata** do User-Agent: acrescentar qualquer identificação a um UA de navegador volta a dar 403. Não há meio-termo técnico.

Consequência séria: **o `robots.txt` da FAA também retorna 403.** Não é possível ler a política de rastreamento publicada pelo site — não há diretiva a respeitar nem a violar, porque ela é inacessível ao cliente que a consultaria.

**Decisão do projeto (2026-08-04):** usar User-Agent de navegador **restrito a `faa.gov` e `apps.dtic.mil`**, a 1 requisição a cada 3–4 s.

Fundamentos registrados para o relatório:

1. São documentos **públicos de governo**, publicados com a finalidade de acesso público — não há conteúdo restrito nem contorno de autenticação.
2. A regra do WAF é **mitigação genérica de bots**, aplicada por padrão pelo provedor de CDN, e não política de rastreamento declarada pela instituição. A prova é que a política declarada é inacessível: o `robots.txt` também retorna 403.
3. A DTIC **autoriza explicitamente** a coleta do seu acervo (*"point your crawler to the sitemap at apps.dtic.mil/sitemap.xml"*), o que torna o bloqueio um efeito colateral da CDN, não uma recusa da instituição.
4. A carga imposta é desprezível — 1 requisição a cada 3–4 s, com orçamento fechado por fonte.

**Limites da exceção:** vale apenas para esses dois hosts, está declarada e comentada em `config/domains.yaml`, e não se estende a nenhuma outra fonte. Todo o restante do projeto usa o UA identificado com contato institucional. Continua valendo a restrição de coletar apenas material com *Distribution Statement A* na DTIC, e a *blocklist* do R&E Gateway.

**Consequência verificada, e é o argumento mais forte dos quatro:** com o UA de navegador o `robots.txt` da FAA **passa a ser legível** — e o crawler passa a respeitá-lo, o que antes era tecnicamente impossível. O conteúdo é um `robots.txt` padrão de Drupal:

```
User-agent: *
Disallow: /core/
Disallow: /profiles/
Disallow: /admin/
Disallow: /fast-41-cpp-tasks/
...
```

Nenhuma diretiva proíbe os caminhos que o projeto rastreia (`/sites/faa.gov/files/`, `/uas/`, `/air_traffic/`, `/nextgen/`). Ou seja: **a política de rastreamento declarada pela FAA autoriza exatamente esta coleta**, e o bloqueio do WAF é o que impedia tanto a coleta quanto a leitura da política que a permite. O `urllib.robotparser` do fetcher agora carrega e aplica essa política (log: `robots.carregado host=www.faa.gov presente=True`).

### Sitemap do Liferay (ESA Cosmos) é degenerado

O índice em `cosmos.esa.int/sitemap.xml` aponta para **87 sub-sitemaps de 1 URL cada** (um por *layout* de página). Segui-lo custaria 87 requisições para obter 87 URLs — pior que inútil, porque consome o orçamento antes de o rastreamento começar.

Implementado `SitemapBudget`: teto de 30 requisições e desistência automática quando o rendimento cai abaixo de 3 URLs/requisição. Medido: abandona após 8 requisições e cai para BFS.

### ESA EOF exige renderização JS — mas os arquivos não são públicos

Confirmado por inspeção do HTML servido: **zero links `.pdf`**, presença de `"Loading Document"`, links de documento carregados por JavaScript. Justifica o `render: true` — é a única fonte da lista que precisa.

Com Playwright, o rastreamento **funciona**: 20 páginas renderizadas, 628 links, e a categoria `/document-cat/operations-concept/` lista exatamente os dois alvos:

```
/document/destine-platform-operations-concept-document/
/document/esa-eo-framework-eof-csc-operations-concept/
```

**Porém as landing pages `/document/{slug}/` não contêm link para arquivo algum** — o PDF fica atrás do visualizador e da área de *Sign In*. Resultado: **zero documentos baixáveis**.

Conclusão: o EOF serve como fonte de **metadado** (identifica os ConOps e seus títulos), não de arquivo. Isso confirma com evidência a avaliação de custo/benefício do plano original — a diferença é que agora se sabe *por quê*: não é o volume (34 documentos), é a indisponibilidade pública dos arquivos.

### ESA Cosmos tem pouco material público

A página do Euclid tem **2 subpáginas** e os links `/documents/` são imagens (`.jpg`). Confirma a avaliação do plano ("Cosmos com pouco material público — escopo fechado por missão"). A documentação de projeto está no `dms.cosmos.esa.int`, que exige autenticação e está em *blocklist*.

### Armadilhas de URL observadas

Padrões que efetivamente aparecem e que o `traps.py` corta: mesma página servida como `http://` e `https://` (ROSA P — dobrava a contagem de documentos), busca facetada, navegação de calendário, identificadores de sessão e caminhos repetidos.

### Busca do ROSA P é 403 para bot

`rosap.ntl.bts.gov/gsearch` retorna 403 ao UA identificado, embora `/view/` e `/browse/` respondam 200. Sem impacto prático: o **OAI-PMH é a via correta para esta fonte** e funciona sem restrição.

---

## 4. CORDIS — bulk bloqueado por `robots.txt`, resolvido com seeds curadas

**Data da verificação:** 2026-08-12.

O plano inicial era um adaptador *bulk* lendo os dumps CSV mensais de `data.europa.eu`
(`projectDeliverables`, filtrados por programa prioritário antes do léxico). Verificado ao
vivo: o desenho funciona — dos ~254 mil entregáveis totais (HORIZON + H2020), o filtro de
programa reduz para ~26 mil, e o léxico deste projeto reduz para 142 candidatos plausíveis.

**Mas os arquivos bulk moram em `cordis.europa.eu/data/`, e o `robots.txt` do CORDIS tem
`Disallow: /data/`** — confirmado com o `robotparser` real do projeto, não por leitura
superficial. Diferente do caso FAA/DTIC (§3-bis — WAF genérico, não política declarada), aqui
é o próprio `robots.txt` do CORDIS dizendo explicitamente para não automatizar aquele
caminho. Um "European Commission reuse notice" está anexado ao dataset em
`data.europa.eu`, mas reuso de dados não é permissão de rastreamento.

**Decisão do projeto:** respeitar o `robots.txt`, abandonar a via bulk. Solução adotada:
`kind: seeds` com os 86 projetos já identificados (pela mesma investigação acima, antes de
descartar a via bulk) como tendo entregáveis relevantes — a página de resultados de cada
projeto (`/project/id/{id}/results`) é permitida pelo `robots.txt`.

### O link do entregável nunca é o arquivo

Achado técnico à parte: o link de cada entregável no CORDIS é uma página HTML ("Documents
download module", em `ec.europa.eu`) que redireciona por **JAVASCRIPT**
(`window.location='...'`), não por `<a href>` nem HTTP 30x — nenhum downloader HTTP puro
segue isso sozinho. As duas requisições (a página-wrapper e o arquivo real) precisam
compartilhar sessão/cookies; o `Fetcher` já usa um único `httpx.Client` para tudo, então
funciona sem tratamento especial. O `<title>` do wrapper é sempre o mesmo texto genérico
("Documents download module") — usá-lo daria a milhares de arquivos distintos o mesmo
título errado, então `extract_metadata` descarta esse título de propósito e o motor cai para
a âncora que levou até a página (o nome real do entregável, como aparece na página do
projeto).

**Validado ao vivo, 100% em conformidade com `robots.txt`:** `discover cordis --max-pages 55`
encontrou 43 documentos, todos `strong` — títulos reais como "Operational Concept Document
(OCD) 2023", "PJ19: CONOPS (2019)", "Final Concept of Operations". `harvest_rate: 0,78` — a
mais alta de qualquer fonte de crawl do projeto.

## 5. CORE (core.ac.uk) — agregador de repositórios acadêmicos

**Data da verificação:** 2026-08-12, contra a API v3 real (`api.core.ac.uk`), com e sem chave.

Diferente das demais fontes: não é um repositório primário nem um portal de projetos — é um
**agregador** que reindexa milhões de repositórios universitários no mundo inteiro. Isso
acrescenta uma camada acadêmica (teses, artigos revisados por pares) que nenhuma fonte atual
cobre. Confirmado ao vivo trazendo ConOps reais de instituições novas: **MIT** (*LAI Concept
of Operations*), **University of North Texas** (*Transportation System Concept of
Operations*, *Mined Geologic Disposal System Concept of Operations* — domínio novo, gestão de
rejeito nuclear/DOE), e o mesmo paper do projeto SESAR/CORUS que já está nas seeds do CORDIS
(*U-space concept of operations*, grant H2020 RIA-763551) — confirma que a deduplicação por
SHA-256 já existente vai lidar com a sobreposição entre fontes, como já faz hoje.

### Divergências reais em relação à documentação publicada

1. **`fullText` sem chave** devolve a string literal `"Not available for public API
   users."` em vez do texto extraído ou de ausência — tratado como ausência
   (`_FULLTEXT_INDISPONIVEL` em `core_api.py`). **Com chave, o texto real vem** — mesmo
   achado de alto impacto do NTRS (`fullText` é gerado com Apache PDFBox sobre o PDF do
   OAI-PMH deles, análogo ao `.txt` do NTRS).
2. **`totalHits` NÃO é confiável** — ao contrário do `stats.total` do NTRS (validado
   observando mudança real no número), uma consulta com escopo de título
   (`title:"concept of operations"`) devolveu `totalHits` na casa das dezenas de milhões,
   claramente contando ocorrências em texto completo, não registros que batem a frase. Os
   RESULTADOS retornados, porém, são relevantes e bem ordenados. Consequência prática: o
   adaptador pagina por `offset`/`limit` até um teto configurável
   (`max_resultados_por_termo`), nunca até esgotar `totalHits`.
3. **`offset`/`limit` paginam corretamente** (confirmado: offset=0 e offset=5 devolvem
   páginas distintas) — ao contrário do bug real do NTRS com o parâmetro `from` sendo
   ignorado, aqui não há essa armadilha.
4. **Nem todo resultado tem arquivo**: `downloadUrl` e `sourceFulltextUrls` podem vir os
   dois vazios (registro só de metadado, artigo fechado sem full text público indexado) —
   descartado antes de emitir.
5. **Latência real: 15 a 60 segundos por chamada**, mesmo em consultas simples, sem relação
   aparente com throttling (o cabeçalho `x-ratelimit-remaining` não caiu após uma única
   chamada — parece ser o tempo de resposta normal do backend deles, não limitação de taxa).
   O orçamento de tempo de uma `discover core` precisa contar com isso.
6. **Cota por tier de registro**, conforme a documentação pública: sem chave, 100
   tokens/dia (10/min); pessoal registrado, 1.000 tokens/dia (25/min); acadêmico, 5.000
   tokens/dia (10/min). O cabeçalho real observado numa chamada autenticada foi
   `x-ratelimit-limit: 150` — não bate exatamente com os números documentados, então vale
   monitorar os cabeçalhos em vez de confiar cegamente no texto da documentação (mesma
   lição do NTRS, item 1.2 acima).
7. **`robots.txt`** de `core.ac.uk`/`api.core.ac.uk` usa o formato novo de "content
   signals" (não `Disallow` tradicional) — sem restrição a `api.core.ac.uk`.

## 6. BASE (base-search.net) — investigado e rejeitado por `robots.txt`

**Data da verificação:** 2026-08-14, ao vivo contra `api.base-search.net` e
`www.base-search.net` (não por leitura de documentação).

Motivação: mesma categoria do CORE (§5) — agregador acadêmico, mas de escala ainda maior
(BASE indexa mais de 400 milhões de documentos via OAI-PMH, contra os milhões do CORE).
Avaliado como fonte candidata a seguir o CORE.

**`api.base-search.net/robots.txt` bloqueia tudo, para todo agente:**

```
User-agent: *
Disallow: /
```

Diferente do CORDIS (§4), onde o `Disallow: /data/` deixava outros caminhos livres e permitiu
a solução de seeds, aqui **não sobra caminho nenhum** dentro da API para respeitar o
`robots.txt` e ainda assim coletar algo. O `Fetcher` do projeto (`crawler/core/fetcher.py`)
consulta e aplica `robots.txt` para **qualquer** adaptador — de API ou de crawl —, então uma
implementação real bateria `BlockedByPolicy` em toda chamada, a menos que o projeto decidisse
deliberadamente configurar `respect_robots: False` para esse domínio.

**Agravante:** `www.base-search.net/robots.txt` (o site principal, não a API) nomeia bots de
IA explicitamente na lista de agentes banidos — `anthropic-ai`, `ClaudeBot`, `Claude-Web`,
junto de `GPTBot`, `PerplexityBot`, `CCBot` etc. — e fecha com um aviso jurídico explícito
contra coleta automatizada sem permissão expressa. Não é a mesma política que rege a API, mas
reforça o sinal de intenção da instituição quanto a automação por agentes de IA.

**Acesso à API também é mais restrito que o CORE:** sem chave e com chave fictícia, ambos
devolveram `{"error": "Access denied for IP address ... and user agent ..."}` — sugere
whitelist de IP além de (ou em vez de) uma chave tipo `Bearer`. A documentação pública
confirma um processo de aprovação manual (formulário + análise de caso de uso + e-mail em
alguns dias), diferente do registro imediato do CORE.

**Decisão (perguntada a você via `AskUserQuestion`, mesmo padrão do CORDIS):** não
implementar. BASE fica documentado como fonte investigada e descartada por política declarada
da instituição — não por limitação técnica. Reavaliar exigiria (a) aprovação formal de acesso
pelo BASE **e** (b) uma decisão explícita e separada sobre configurar `respect_robots: False`
para `api.base-search.net`, já que a aprovação de uso não muda o que o `robots.txt` declara.

## 7. ASC-CSA (Canadian Space Agency) — investigado e rejeitado por baixo sinal/volume

**Data da verificação:** 2026-08-14, ao vivo contra `www.asc-csa.gc.ca` (sitemaps reais e
amostra de páginas), duas rodadas de investigação na mesma sessão.

### 7.1 `/eng/publications/` — conteúdo administrativo, não de engenharia

`robots.txt` é permissivo (só `Disallow: /longdesc/`), e o sitemap dedicado
(`sitemap-site-publications.xml`) lista 598 URLs — nada bloqueado tecnicamente. Mas a
amostragem de URLs e páginas reais mostrou que a seção é majoritariamente **relatórios
corporativos/administrativos**: relatórios ao Parlamento, relatórios financeiros trimestrais,
auditoria e avaliação, "State of the Canadian Space Sector". Zero ocorrências de
"concept"/"operations"/"conops" nas 598 URLs do sitemap. Mesmo padrão de risco do FAA (§3-bis,
item 6) — tecnicamente crawlável, mas sem o tipo de conteúdo que o léxico busca.

### 7.2 `funding-programs/funding-opportunities/ao/` — sinal melhor, ainda insuficiente

Subdiretório de editais (*Announcements of Opportunity*) pareceu mais promissor por conter
especificações técnicas de missão embutidas no HTML. Escaneadas as 180 páginas do subdiretório
(via sitemap real) por links a PDF:

- **53 PDFs únicos** de 150/180 páginas amostradas (~0,35 PDF/página) — densidade baixa.
- Maioria é **boilerplate repetido**: o mesmo
  `Guidelines_for_the_use_of_protected_document_submission_system.pdf` aparece em mais de 40
  páginas — instrução de submissão, não documento técnico.
- Restante: EULAs de licenciamento (RADARSAT-2), relatórios de prioridades científicas,
  roadmaps, manuais do NEOSSAT — nenhum documento de concepção de missão.
- **Nenhum arquivo com "conops"/"concept of operations"/"opscon" no nome** em toda a amostra.
- **Único achado tecnicamente relevante**: `AD-01 CSA-WFS-RD-0002_Rev_IR_-_Mission_Requirements
  _Document.pdf` (projeto WildFireSat) — mas hospedado em `ftp://ftp.asc-csa.gc.ca/...`. O
  `Fetcher` do projeto usa `httpx`, que **não suporta o esquema `ftp://`** — inalcançável sem
  trabalho novo de suporte a FTP, para um retorno de ~1 documento que nem é um ConOps de fato
  (é um *Requirements Document*).
- Vários PDFs citados nas páginas de AO são externos (NASA NTRS, ESA, universidades,
  CASCA) — bibliografia do edital, não documentos autorais da CSA.

**Decisão (perguntada a você via `AskUserQuestion`, duas vezes — uma por subseção):** não
implementar nenhuma das duas. ASC-CSA fica documentado como fonte investigada e descartada por
volume/sinal insuficiente — não por bloqueio de política (diferente do CORDIS/BASE, §4 e §6).
Reavaliar exigiria ou (a) suporte a `ftp://` no `Fetcher`, ou (b) uma varredura mais profunda
do FTP público da CSA (`ftp.asc-csa.gc.ca`, pastas por projeto: `ExP`, `TRP`, `SESS`,
`OpenData_DonneesOuvertes`) para saber se há mais material lá do que o linkado nas páginas
HTML — nenhum dos dois foi feito nesta rodada.

## 8. GOV.UK (Ministry of Defence) — implementado, mesmo padrão do CORE

**Data da verificação:** 2026-08-14, ao vivo contra `www.gov.uk`. Motivação: você trouxe a URL
de busca humana do GOV.UK filtrada por `organisations[]=ministry-of-defence` como candidata.

### `robots.txt` bloqueia a busca humana, não a API

```
Disallow: /*/print$
Disallow: /search/all*
```

A URL que você trouxe bate exatamente `Disallow: /search/all*`. Mas o **Search API**
(`www.gov.uk/api/search.json`) e o **Content API** (`www.gov.uk/api/content/<path>`) são
caminhos separados, sem nenhuma entrada em `robots.txt` — mesmo padrão "UI bloqueada, API
livre" já visto no CORE e no NTRS. Sem chave, sem cadastro, testado ao vivo de imediato.

### Desenho: duas chamadas por documento, não uma

Diferente do CORE (uma chamada devolve tudo), o `search.json` não inclui anexos — só
`title`/`description`/`link`. Os PDFs só aparecem em `details.attachments[].url` do
`content.json`. `GovUKAdapter._to_record` busca o conteúdo por `link` e descarta
(`return None`) se não houver anexo `application/pdf` — confirmado ao vivo que tipos como
`speech`/`news_story`/`oral_statement` não têm `details.attachments` (mesma regra "nem todo
resultado tem arquivo" do CORE, §5 item 4).

Para não pagar essa segunda chamada em duplicidade, o adaptador deduplica pelo `link` **entre
termos**, dentro do mesmo `discover()` — o mesmo documento pode bater buscas por frases
diferentes do léxico (achado ao vivo: "Air Operating Concept" aparece tanto em buscas por
"operational concept" quanto por "operating concept").

### Latência real — ordens de grandeza melhor que o CORE

Medido ao vivo: **~2s por busca** (até 1500 resultados/chamada, bem acima de qualquer volume
visto neste projeto), **<1s por chamada de conteúdo**. Paginação por `start`/`count`
confirmada correta (páginas disjuntas) — sem o bug do `from` do NTRS nem a inflação de
`totalHits` do CORE.

### Volume e qualidade do conteúdo

Termos do léxico, filtrados por `ministry-of-defence`: `"operational concept"`/`"operations
concept"` → 62, `"joint doctrine publication"` → 28, `"JDP"` → 43, `"joint concept note"` → 14.
O GOV.UK mantém coleções curadas próprias — *Joint Doctrine Publications* (17 documentos, ex.
"UK Defence Doctrine (JDP 0-01)", "UK Space Power (JDP 0-40)") e *Strategic and Joint
Capability Concepts/JCN* (inclui "Air Operating Concept", "Maritime Operating Concept",
"Medical Operating Concept", "Integrated Operating Concept") — descobertas naturalmente pela
busca por termo, sem precisar de curadoria manual tipo CORDIS.

### Achado de léxico: "Operating Concept", não "Operational Concept"

O MOD britânico nomeia esse tipo de documento **"Operating Concept"** — frase que não estava
em `config/lexicon.yaml` (só havia "operational concept"/"operations concept"). Sem o termo,
títulos como "Air Operating Concept (AirOpC)" não pontuavam `strong`. Adicionado a
`strong_terms` (e a `TERMOS_LEXICO_FORTE` em `cli.py`, a lista usada como consulta pelas três
fontes de busca textual) — achado válido independente desta fonte, mas encontrado por causa
dela.

### Validado ao vivo

`discover govuk --limit 5`: 19 candidatos vistos, 5 relevantes (4 `strong` + 1 `weak`), 0
descartados por `export_control` (conteúdo público por definição — licença fixa `Crown
copyright — Open Government Licence v3.0`, não varia por documento). `discover-all --only
govuk --limit 3` confirma a fiação sem quebrar `--only`.

## 9. Eficiência do `discover`: threads sub-utilizadas e o limite real de `rate`

**Data da verificação:** 2026-08-16. Motivação: você observou `discover-all` (ntrs, rosap,
core, govuk, cordis) parecendo travado no CORDIS, com as outras fontes "esperando".

### Auditoria: concorrência configurada vs. usada de fato

Cruzando o código de cada `discover()` com a `concurrency` que `config/domains.yaml` já
autorizava para aquele host:

| Fonte | `concurrency` liberada | Threads usadas antes | Motivo |
|---|---|---|---|
| NTRS | 2 | 1 | `discover()` andava 7 termos em sequência — nenhum estado compartilhado entre eles, sub-utilização real. |
| ROSAP | 2 | 1 | **Não é sub-utilização**: harvest OAI-PMH único por `resumptionToken`, protocolo proíbe paralelizar páginas de uma mesma sequência. |
| CORE | 1 | 1 | Já correto — `concurrency: 1` é decisão deliberada (§5, item 6), sem ganho possível. |
| GOV.UK | 2 | 1 | Pior caso: além de não paralelizar termos, é a única fonte com uma chamada de rede *por documento* (`content.json`), sequencial. |
| CORDIS (referência) | 3 | 3 | Já correto — `CrawlAdapter` já abre threads próprias. A demora de 36 min nessa fonte é orçamento (1076 páginas × `rate=2s`/3), não threads ociosas. |

### Fix aplicado 1 — `NTRSAdapter`: termos em paralelo — **funcionou como previsto**

`discover()` agora roda os termos numa `ThreadPoolExecutor` de tamanho
`min(policy_for(host).concurrency, 32)`. **Validado ao vivo**: dois termos independentes (395
e 102 resultados reais) terminaram com 2s de diferença um do outro, total de parede 10s —
evidência direta de execução concorrente (sequencial seria a soma dos dois tempos).

### Fix aplicado 2 — `GovUKAdapter`: chamadas de `content.json` em paralelo — **não teve ganho, e o motivo importa**

Implementado com o mesmo raciocínio (pool de 2, mesmo teto de `www.gov.uk`). **Medido ao vivo,
antes/depois**: `discover govuk --terms "operational concept"` (62 documentos) levou 61s
sequencial e **64s** com a pool — ganho zero.

Causa raiz: `Fetcher._throttle()` roda **dentro** do semáforo de `concurrency`
(`crawler/core/fetcher.py:312`), e serializa o **início** de cada requisição a
`ultimo_hit + rate` — por host, não por slot de concorrência. Como `content.json` responde em
menos de 1s e `rate` era 1.0s, a concorrência nunca chegava a ser o fator limitante de
verdade: uma nova requisição só podia começar 1x/segundo de qualquer forma. **Abrir mais
threads é seguro (o semáforo nunca deixa passar do configurado) mas não é o mesmo que ser
eficaz** — a lição desta seção.

### Fix aplicado 3 — `www.gov.uk`: `rate` reduzido de 1.0s para 0.5s (experimento do operador)

Mesmo padrão do experimento de `concurrency: 3` de 2026-08-05 (`config/domains.yaml`, hosts
sem limite publicado). **Validado ao vivo**: o mesmo teste (`"operational concept"`, 62
documentos) caiu de 61-64s para **38.5s** — ganho real de ~40%, agora que a pool do Fix 2 tem
o que fazer. Sem nenhum 429/403 novo na amostra testada. Risco assumido e registrado no
comentário do YAML: reverter para 1.0s se aparecer erro de taxa.

### Fix aplicado 4 — `Frontier`: `PRAGMA synchronous=NORMAL`

`add()`/`mark()` fazem `commit()` dentro do único `RLock` do processo inteiro (compartilhado
por TODAS as fontes rodando em paralelo, não por fonte/host — `crawler/core/frontier.py:139`).
`journal_mode=WAL` sozinho ainda fazia fsync síncrono a cada commit; `synchronous=NORMAL` é a
combinação documentada pelo próprio SQLite para WAL com escritores frequentes, e reduz o tempo
que cada `add()` segura o lock — beneficia todas as fontes, não só as tocadas nos fixes 1-3.
Risco: perder a última transação numa queda de energia (não um crash de processo comum) —
inofensivo, porque a descoberta é idempotente por `(source, source_id)`.

## 10. Impacto no cronograma

| Item do plano | Situação |
|---|---|
| NTRS como fonte âncora | ✅ confirmado e implementado |
| `links.fulltext` barateia a Etapa 2 | ✅ confirmado — 78% na primeira coleta |
| ROSA P promovido a prioridade máxima | ✅ confirmado e implementado |
| Particionamento obrigatório no NTRS | ✅ confirmado (teto sobre a soma) |
| DTIC como maior risco | ⚠️ risco real, **causa diferente** — indisponibilidade, não anti-bot |
| Semanas 5–7 para NTRS + ROSA P | Ambos rodando de ponta a ponta bem antes do previsto |
