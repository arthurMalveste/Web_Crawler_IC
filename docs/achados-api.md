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

## 4. Impacto no cronograma

| Item do plano | Situação |
|---|---|
| NTRS como fonte âncora | ✅ confirmado e implementado |
| `links.fulltext` barateia a Etapa 2 | ✅ confirmado — 78% na primeira coleta |
| ROSA P promovido a prioridade máxima | ✅ confirmado e implementado |
| Particionamento obrigatório no NTRS | ✅ confirmado (teto sobre a soma) |
| DTIC como maior risco | ⚠️ risco real, **causa diferente** — indisponibilidade, não anti-bot |
| Semanas 5–7 para NTRS + ROSA P | Ambos rodando de ponta a ponta bem antes do previsto |
