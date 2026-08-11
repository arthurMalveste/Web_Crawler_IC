# Painel web (opcional)

Interface local para rodar `discover`/`harvest` com checkboxes de fonte e
acompanhar métricas ao vivo, sem digitar a CLI a cada execução. Camada fina:
só chama `python -m crawler.cli ...` como subprocesso e lê o mesmo SQLite que
o coletor já usa (`frontier.sqlite`, `urlfrontier.sqlite`). Nenhuma lógica de
descoberta/coleta muda por causa dela — ver `webui/metrics.py`/`jobs.py` para
o que exatamente é reaproveitado de `crawler/`.

## Rodar

```bash
pip install -r requirements.txt   # inclui o Flask
python -m webui.app
```

Abra http://127.0.0.1:5000 no navegador.

**Só localhost.** O servidor não escuta na rede (`127.0.0.1` fixo em
`webui/app.py`) — é ferramenta de uso pessoal, e cada clique dispara tráfego
real contra os servidores das fontes configuradas.

## O que cada botão faz

- **Descobrir selecionadas** — roda `discover-all --only <fontes marcadas>`
  (mesmo comando da CLI, mesmo isolamento de falha por fonte). Só registra
  candidatos, não baixa nada.
- **Coletar pendentes** — roda `harvest` — baixa tudo que já foi aprovado
  pela descoberta, de qualquer fonte. O harvest não filtra por fonte hoje;
  os checkboxes valem só para descobrir.

Só uma execução por vez: iniciar uma segunda enquanto a primeira roda
devolve um erro em vez de disparar dois subprocessos.

## Métricas mostradas

- **Taxa configurada** (`s/req`) e concorrência por fonte — vêm direto de
  `config/domains.yaml` via `Config.policy_for`, o mesmo valor que o
  `Fetcher` respeita. É a taxa *declarada*, não a espera real medida em
  tempo real (isso exigiria instrumentação nova no `Fetcher`, fora de
  escopo aqui).
- **Pendentes / Armazenados por fonte**, na tabela de Fontes — cruzamento
  fonte×status lido direto do `frontier.sqlite` (`metrics.counts_by_source_and_status`).
- **Prévia do harvest** ("N arquivos serão baixados agora"), por fonte,
  recalculada a cada mudança nas caixas de faixa (`strong`/`weak`)
  — usa a MESMA consulta que `Pipeline.harvest()` de fato consome
  (`Frontier.pending`, via `metrics.pending_by_source`), então não é uma
  estimativa. Se nenhuma faixa estiver marcada, um aviso explícito lembra
  que isso baixa TUDO sem filtro (mesmo comportamento de `harvest` sem
  `--tier` na CLI).
- **Documentos novos descobertos nesta execução**, por fonte — diferença de
  `frontier.sqlite` entre o início do job e agora.
- **URLs vasculhadas nesta execução**, por fonte — só para fontes de
  navegação HTML (as que têm um YAML em `config/sources/`); fontes via API
  (NTRS, ROSA P) não têm essa noção e aparecem como "—".
- **Resultado do harvest discriminado por fonte** (armazenados/duplicados/
  inalterados/falhos) — a lista de fontes aqui não é fixa: aparece quem quer
  que tenha mudado de status desde o início do job, mesmo sem checkbox de
  fonte no harvest.
- Cauda do log (`reports/webui-runs/<job_id>.log`) da execução em
  andamento/mais recente.

## Fora de escopo (de propósito)

Sem WebSocket, sem botão de cancelar, sem autenticação, sem histórico de
execuções na UI (a tabela `runs` do `Frontier` já persiste isso) — ver a
seção "Escopo explicitamente fora do v1" do plano original se for retomar.
