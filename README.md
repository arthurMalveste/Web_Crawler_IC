# Pipeline de Recuperação de ConOps — Etapa 1 (Coleta)

Coleta automática de documentos de concepção de sistemas (*Concept of Operations*) para uso no processo STPA.

**Projeto:** Busca Automática e Classificação Semântica de Documentos de Concepção de Sistemas Críticos
**Autor:** Arthur Miele Malveste — FT/UNICAMP
**Escopo deste repositório:** Etapa 1 do pipeline. Extração textual, filtragem léxica sobre texto completo, *embeddings* e classificação por LLM são etapas posteriores.

---

## Decisão metodológica: crawler genérico, API onde ela existe

O corpus precisa ser **diverso em fonte**, não apenas grande. Se ele vier majoritariamente de um repositório, o classificador da Etapa 5 aprende a reconhecer *o formato daquele repositório* em vez do que é um ConOps — e a conclusão do trabalho não se sustenta fora do acervo de origem. Diversidade de fonte é, aqui, **requisito de validade experimental**.

Por isso o caso geral é um *web crawler* de verdade, capaz de navegar qualquer repositório institucional a partir de um arquivo de configuração. Onde existe interface estruturada (a API do NTRS, o OAI-PMH do ROSA P), ela é preferida: entrega centenas de registros com metadado completo por requisição, contra ~1 no rastreamento HTML.

A escolha entre Scrapy/Selenium e as ferramentas efetivamente adotadas está documentada em [docs/crawler.md](docs/crawler.md); as divergências verificadas contra as APIs e sites reais, em [docs/achados-api.md](docs/achados-api.md).

> 📄 **Documentação completa do sistema (33 páginas, PDF):** [docs/ConOps-Retrieval-Pipeline-Documentacao.pdf](docs/ConOps-Retrieval-Pipeline-Documentacao.pdf) — arquitetura passo a passo, glossário de toda a nomenclatura, pontos positivos e negativos, e próximos passos priorizados. Gerada a partir de [docs/sistema.html](docs/sistema.html) com `python docs/gerar_pdf.py`.

## Arquitetura

```
config/sources/*.yaml -> SourceSpec
         |
   +-----+---------------+------------------+
   |                     |                  |
API dedicada       Protocolo          CRAWLER GENÉRICO
(NTRS)             (OAI-PMH,          (navegação HTML,
                    sitemap, bulk)     rastreamento focado)
   +-----+---------------+------------------+
         v
1. DESCOBERTA   -> DocumentRecord (contrato único)
2. FILA         SQLite: estado, idempotência, retomada, dedupe
                + PRÉ-FILTRO LÉXICO sobre metadados (decide SE baixa)
3. FETCH        downloader único, políticas por domínio
4. ARMAZENAMENTO endereçado por conteúdo (SHA-256) + manifesto JSONL
```

A separação entre **descobrir** e **baixar** é o que permite ao mesmo núcleo servir uma API REST, um OAI-PMH, um dump CSV e um site HTML — e é o que torna barato reexecutar a descoberta quando o léxico muda, sem baixar nada de novo.

### Rastreamento focado (*"Web Crawling semântico"*)

O crawler pontua cada link **antes de segui-lo** — tokens da URL, texto da âncora e contexto ao redor — e expande primeiro o que promete mais. As duas estratégias (`focused` e `bfs`) compartilham todo o código; a única diferença é o pontuador estar ligado, o que isola essa variável na comparação (ver ressalva sobre o que ela mede, abaixo):

```bash
python -m crawler.cli experiment rosap_crawl --max-pages 60
```

Detalhes em [docs/crawler.md](docs/crawler.md).

Duas identidades distintas, deliberadamente separadas:

- **identidade da descoberta** = `(source, source_id)` — evita reprocessar o mesmo registro;
- **identidade do conteúdo** = **SHA-256 do arquivo** — deduplica entre repositórios. O mesmo ConOps vindo da FAA, do ROSA P e do NTRS vira **um arquivo** com três proveniências registradas.

URL nunca é chave: no Liferay (ESA Cosmos) ela carrega `uuid` + *timestamp* e muda a cada reedição.

## Instalação

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
copy .env.example .env          # cp no bash
```

O `.env` guarda **tudo que depende da máquina** — e só isso. Ele não vai para o git; [.env.example](.env.example) é o modelo versionado, com as variáveis comentadas:

| Variável | Para que serve |
|---|---|
| `CONOPS_DATA_ROOT` | Raiz do corpus. Se ele for pasta irmã do repositório (`../ConOpsCorpus`), o padrão já serve e o `.env` é dispensável. |
| `CONOPS_CONTACT_EMAIL` | E-mail anunciado no User-Agent. **Trocar pelo institucional** antes da primeira coleta em escala: um bot acadêmico identificável é tolerado, um bot anônimo é bloqueado. |

O [config/domains.yaml](config/domains.yaml) referencia essas variáveis como `${VAR:-default}` e nunca contém caminho absoluto — é arquivo versionado, e caminho absoluto vale em uma máquina só. Uma variável já exportada no ambiente tem prioridade sobre o `.env`, o que permite apontar uma execução para outro corpus sem editar arquivo nenhum:

```bash
CONOPS_DATA_ROOT=/mnt/scratch/corpus python -m crawler.cli report   # bash
python -m crawler.cli report --data-root D:/outro/Corpus            # ou por flag
```

**Se você mover o corpus ou o repositório**, basta atualizar `CONOPS_DATA_ROOT`: os caminhos gravados no banco são relativos a essa raiz, então continuam válidos.

## Uso

```bash
# Descoberta — só metadados, barata, reexecutável à vontade. Paralela por host.
python -m crawler.cli discover ntrs  --year-start 2015 --year-end 2026   # API
python -m crawler.cli discover rosap --max-pages 40                      # OAI-PMH
python -m crawler.cli discover faa   --max-pages 200                     # crawler
python -m crawler.cli discover psas  --max-pages 120                     # hard negatives

# Todas as fontes configuradas numa invocação só — uma que falhar não trava as outras
python -m crawler.cli discover-all

# Rastreamento focado vs BFS na mesma fonte — leitura diagnóstica, não prova de precisão
python -m crawler.cli experiment faa --max-pages 200

# Coleta — só aqui gasta banda e disco. Paralela por domínio.
python -m crawler.cli harvest --tier strong weak --limit 100

# Reenfileirar falhos: harvest (documentos) ou descoberta (páginas), após corrigir o coletor
python -m crawler.cli retry-failed
python -m crawler.cli discover faa --retry-failed

# Métricas
python -m crawler.cli report

# Sincronização semanal — exigida pelos termos de uso do NTRS
python -m crawler.cli sync-ntrs --since 2026-07-28
```

## Resultados medidos (2026-08-04)

**Corpus:** 1.799 candidatos descobertos em 5 fontes, 213 documentos coletados.

| Faixa | Quantidade |
|---|---:|
| `strong` (sinal forte no título) | 92 |
| `weak` (sinal no abstract/subject) | 224 |
| `hard_negative` (acervo PSAS) | 627 |
| `negative_sample` (negativos fáceis) | 856 |

Por fonte: PSAS 703 · ROSA P 417 · FAA 383 · NTRS 295.

Quatro números sustentam as decisões de arquitetura:

- **O pré-filtro evita a maior parte do tráfego.** No ROSA P, 4.000 metadados foram varridos em ~40 s e 3.583 PDFs não precisaram ser baixados.
- **O `links.fulltext` do NTRS barateia a Etapa 2.** 87% dos documentos do NTRS chegaram como texto já extraído, ocupando 13 MB — os mesmos em PDF ocupariam ordens de grandeza mais e exigiriam extração.
- **O rastreamento focado achou mais candidatos sob o mesmo orçamento.** Na FAA, com 79 páginas: `harvest_rate` 0,0506 (focado) contra 0,0253 (BFS) — 2,0× mais candidatos por página. Leitura diagnóstica, não prova de precisão: "relevante" aqui é o tier do léxico, não verificação de que o documento é de fato um ConOps, e a amostra é pequena (4 contra 2). Ver [docs/crawler.md](docs/crawler.md).
- **Sitemap, quando existe, dispensa navegar.** A FAA entregou 4.175 URLs em poucas requisições.

Idempotência verificada em produção: reexecutar a descoberta no mesmo escopo devolveu **377 vistos, 0 novos**.

## Fontes

| Fonte | Via | Situação |
|---|---|---|
| **NTRS / NASA STI** | API REST dedicada | ✅ coletando |
| **ROSA P (US DOT)** | OAI-PMH + coleta incremental | ✅ coletando |
| **MIT PSAS** (*hard negatives*) | crawler | ✅ 703 documentos |
| **FAA** | crawler | ✅ 4.175 URLs via sitemap |
| **ESA Cosmos** | crawler | ✅ roda — pouco material público |
| **ESA EOF** | crawler + Playwright | ⚠️ rastreia, mas arquivos não são públicos |
| **DTIC** | *sitemap* autorizado + crawler | ⛔ host em manutenção |
| **CORDIS** | *bulk* CSV + crawler | spec pronto, adaptador *bulk* pendente |

Adicionar uma fonte custa um YAML em [config/sources/](config/sources/) — nenhum código Python.

### Exceção de User-Agent: FAA e DTIC

Ambos retornam **403 ao User-Agent identificado** e 200 apenas ao User-Agent de navegador puro; um UA híbrido (navegador + contato acadêmico) também é bloqueado. Na FAA, o próprio `robots.txt` retorna 403 — a política de rastreamento do site é inacessível ao cliente que a consultaria.

Isso derruba a premissa de que *"administradores toleram bots identificados de universidades"*: para sites `.gov` atrás de Akamai, o efeito é o inverso.

**Decisão do projeto:** usar UA de navegador **restrito a `faa.gov` e `apps.dtic.mil`**, com taxa de 1 requisição a cada 3–4 s. Justificativa: são documentos públicos de governo, publicados para acesso público; a regra do WAF é mitigação genérica de bots, não política declarada da instituição; e a carga imposta é desprezível. Todo o restante do projeto continua com o UA identificado e contato institucional. A exceção está delimitada e comentada em [config/domains.yaml](config/domains.yaml) e detalhada em [docs/achados-api.md](docs/achados-api.md).

## Pré-filtro léxico

Decide, **a partir do metadado**, se vale baixar o arquivo. É aqui que se ganha ordem de grandeza: na primeira coleta real, 4.000 registros do ROSA P foram varridos e **3.583 PDFs não precisaram ser baixados**.

| Faixa | Condição | Ação |
|---|---|---|
| `strong` | sinal forte no título, sem marcador adversarial | baixar sempre |
| `weak` | sinal em abstract/subject, ou `strong` rebaixado | baixar, marcar para revisão |
| `negative_sample` | sem sinal | amostrar N por fonte e baixar |

A terceira faixa não é opcional. O objetivo final é um **classificador binário**; sem classe negativa não há precisão, recall nem F1, e a Etapa 5 vira demonstração em vez de avaliação. A amostragem é determinística (semente fixa + hash do id): reexecutar a coleta seleciona exatamente os mesmos negativos.

O léxico ([config/lexicon.yaml](config/lexicon.yaml)) inclui a **assinatura estrutural** da ISO/IEC/IEEE 29148:2011 e da ANSI/AIAA G-043A-2012 — títulos de seção canônicos são sinal muito mais específico que o termo isolado. Ele é reaproveitado integralmente na Etapa 3 e vira base do *prompt* da Etapa 5.

## Conformidade

- **Export control:** registros com ITAR/EAR marcado são descartados **antes** de entrar na fila. A contagem vai para `rejects.jsonl` e para o relatório.
- **User-Agent:** identificável e com contato institucional em todas as fontes, **exceto** `faa.gov` e `apps.dtic.mil`, onde o WAF só responde a UA de navegador (ver acima). A exceção é explícita, delimitada por host e documentada.
- **NTRS:** atribuição obrigatória — *"Data provided by NASA Scientific and Technical Information Program"*. Os termos exigem consulta semanal a `/redistributions` e remoção de documentos retirados (comando `sync-ntrs`).
- **DTIC:** apenas *Distribution Statement A*. R&E Gateway (exige CAC/PIV) em blocklist.
- **ESA:** `dms.cosmos.esa.int` exige autenticação e está em **blocklist explícita** — não se tenta acesso.
- **Postura geral:** o corpus é **interno ao projeto**. O que se publica são metadados, métricas e identificadores persistentes (DOI, id NTRS, número AD) — nunca os PDFs.

## Testes

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

Os testes usam *fixtures* gravadas dos serviços reais (`tests/fixtures/`) e **não tocam a rede** — reproduzir a coleta não pode depender de os servidores estarem no ar.

## Estrutura

```
config/       domains.yaml (políticas), lexicon.yaml (termos), sources/ (uma fonte por YAML)
crawler/core/ record, prefilter, fetcher, frontier, store, pipeline
crawler/adapters/  ntrs, rosap, ...
docs/         achados-api.md — divergências verificadas contra as APIs reais
reports/      métricas datadas de cada execução
tests/        fixtures gravadas
```

O corpus é gravado em `data_root` — configurado por `CONOPS_DATA_ROOT` no [.env](.env.example) —, **fora do repositório e fora do OneDrive** de propósito: dezenas de GB sincronizando travam o cliente e geram conflitos de arquivo.
