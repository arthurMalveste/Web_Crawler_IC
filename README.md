# Pipeline de Recuperação de ConOps — Etapa 1 (Coleta)

Coleta automática de documentos de concepção de sistemas (*Concept of Operations*) para uso no processo STPA.

**Projeto:** Busca Automática e Classificação Semântica de Documentos de Concepção de Sistemas Críticos
**Autor:** Arthur Miele Malveste — FT/UNICAMP
**Escopo deste repositório:** Etapa 1 do pipeline. Extração textual, filtragem léxica sobre texto completo, *embeddings* e classificação por LLM são etapas posteriores.

---

## Decisão metodológica: *harvester-first, crawler-last*

O projeto original previa Scrapy/BeautifulSoup/Selenium para todas as fontes. A inspeção direta dos repositórios mostrou que **as fontes de maior valor expõem interfaces de máquina estruturadas** (REST, OAI-PMH, *bulk* CSV, *sitemap*). Rastrear HTML nessas fontes seria mais lento, mais frágil, mais pobre em metadados e eticamente pior.

Rastreamento HTML fica reservado a onde não há alternativa. Isso não é um desvio de escopo: é um resultado da revisão de ferramentas prevista na Atividade 1, e está documentado em [docs/achados-api.md](docs/achados-api.md).

## Arquitetura

```
1. DESCOBERTA   adaptadores (1 por fonte, interface única) -> DocumentRecord
2. FILA         SQLite: estado, idempotência, retomada, dedupe
                + PRÉ-FILTRO LÉXICO sobre metadados (decide SE baixa)
3. FETCH        downloader único, políticas por domínio
4. ARMAZENAMENTO endereçado por conteúdo (SHA-256) + manifesto JSONL
```

A separação entre **descobrir** e **baixar** é o que permite ao mesmo núcleo servir uma API REST, um OAI-PMH, um dump CSV e um HTML — e é o que torna barato reexecutar a descoberta quando o léxico muda, sem baixar nada de novo.

Duas identidades distintas, deliberadamente separadas:

- **identidade da descoberta** = `(source, source_id)` — evita reprocessar o mesmo registro;
- **identidade do conteúdo** = **SHA-256 do arquivo** — deduplica entre repositórios. O mesmo ConOps vindo da FAA, do ROSA P e do NTRS vira **um arquivo** com três proveniências registradas.

URL nunca é chave: no Liferay (ESA Cosmos) ela carrega `uuid` + *timestamp* e muda a cada reedição.

## Instalação

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

Antes da primeira coleta em escala, **troque o e-mail de contato** em [config/domains.yaml](config/domains.yaml). Um User-Agent identificável com contato institucional é o que distingue um bot acadêmico tolerado de um bot anônimo bloqueado.

## Uso

```bash
# Descoberta — só metadados, barata, reexecutável à vontade
python -m crawler.cli discover ntrs  --year-start 2015 --year-end 2026
python -m crawler.cli discover rosap --max-pages 40

# Coleta — só aqui gasta banda e disco
python -m crawler.cli harvest --tier strong weak --limit 100

# Reenfileirar falhos (após corrigir o coletor ou depois de erro de rede)
python -m crawler.cli retry-failed

# Métricas
python -m crawler.cli report

# Sincronização semanal — exigida pelos termos de uso do NTRS
python -m crawler.cli sync-ntrs --since 2026-07-28
```

## Resultados da primeira execução real (2026-08-04)

| Métrica | Valor |
|---|---|
| Candidatos descobertos | 712 (NTRS 295, ROSA P 417) |
| Documentos coletados | 207 |
| **Com texto já extraído pela fonte** | **181 / 207 (87%)** |
| Volume — 181 arquivos de texto | 13 MB |
| Volume — 26 PDFs | 118 MB |
| Falhas de download | 1 (timeout de rede, reprocessável) |
| Descartados por *export control* | 0 |

Dois números sustentam as decisões do plano:

- **O pré-filtro evita a maior parte do tráfego.** No ROSA P, 4.000 metadados foram varridos em ~40 s e 3.583 PDFs não precisaram ser baixados.
- **O `links.fulltext` do NTRS barateia a Etapa 2.** 87% do corpus chegou como texto pronto, ocupando 13 MB — os mesmos documentos em PDF ocupariam ordens de grandeza mais e exigiriam extração.

Idempotência verificada em produção: reexecutar a descoberta no mesmo escopo devolveu **377 vistos, 0 novos**.

## Fontes

| Fonte | Camada | Estratégia | Situação |
|---|---|---|---|
| **NTRS / NASA STI** | API REST | cliente REST + particionamento | ✅ implementado |
| **ROSA P (US DOT)** | OAI-PMH | *harvester* + coleta incremental | ✅ implementado |
| CORDIS | *bulk* CSV | download + filtro offline | pendente |
| DTIC | *sitemap* | *sitemap* + fetch lento | ⚠️ host em manutenção |
| MIT PSAS | HTML estático | 1 GET + BeautifulSoup | pendente |
| ESA Cosmos | portal Liferay | crawler limitado por missão | pendente |
| ESA EOF / FAA / EASA / NHTSA | URLs conhecidas | lista-semente | pendente |

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
config/       domains.yaml (políticas), lexicon.yaml (termos), seeds/
crawler/core/ record, prefilter, fetcher, frontier, store, pipeline
crawler/adapters/  ntrs, rosap, ...
docs/         achados-api.md — divergências verificadas contra as APIs reais
reports/      métricas datadas de cada execução
tests/        fixtures gravadas
```

O corpus é gravado em `data_root` ([config/domains.yaml](config/domains.yaml)), **fora do OneDrive** de propósito: dezenas de GB sincronizando travam o cliente e geram conflitos de arquivo.
