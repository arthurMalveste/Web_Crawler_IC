"""Indice navegavel do corpus.

O armazenamento enderecado por conteudo tem uma vantagem grande (deduplicacao e
idempotencia de graca) e um custo real: os arquivos se chamam
`089b2c4d08dd...pdf`, e ninguem navega um diretorio assim.

Este modulo paga esse custo gerando uma pagina HTML que liga TITULO -> ARQUIVO,
com busca e filtros. E so leitura: nao altera o corpus nem o banco.

    python -m crawler.cli browse
    python -m crawler.cli browse --tier strong --abrir
"""

from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path
from typing import Any

CORES_FAIXA = {
    "strong": ("#1b5e20", "#d8ecd9"),
    "weak": ("#7a5800", "#fdf0d0"),
    "hard_negative": ("#5b2d8e", "#e9ddf7"),
    "negative_sample": ("#5a5a5a", "#e8e8e8"),
}


def _linhas(db: Path, tiers: list[str] | None, limite: int | None) -> list[dict[str, Any]]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    sql = """
        SELECT d.key, d.source, d.source_id, d.title, d.tier, d.lexicon_score,
               d.landing_url, d.stored_path, d.sha256, d.status, d.content_kind,
               d.matched_terms, c.size_bytes, c.n_pages, c.provenance
          FROM documents d
          LEFT JOIN content c ON c.sha256 = d.sha256
         WHERE d.stored_path IS NOT NULL
    """
    params: list[Any] = []
    if tiers:
        sql += f" AND d.tier IN ({','.join('?' * len(tiers))})"
        params += tiers
    sql += " ORDER BY d.lexicon_score DESC, d.title"
    if limite:
        sql += f" LIMIT {int(limite)}"
    linhas = [dict(r) for r in con.execute(sql, params)]
    con.close()
    return linhas


def _cartao(r: dict[str, Any], raiz: Path) -> str:
    cor, fundo = CORES_FAIXA.get(r["tier"] or "", ("#333", "#eee"))
    # O banco guarda caminho relativo a raiz do corpus; o link file:// precisa
    # do absoluto. Caminhos absolutos legados (pre-migracao) passam intactos.
    p = Path(r["stored_path"] or "")
    caminho = (p if p.is_absolute() else raiz / p).as_posix()
    tamanho = f"{(r['size_bytes'] or 0) / 1e6:.1f} MB" if r["size_bytes"] else "—"
    paginas = f"{r['n_pages']} pág." if r["n_pages"] else ""
    termos = ", ".join(json.loads(r["matched_terms"] or "[]")[:5])
    prov = json.loads(r["provenance"] or "[]")
    multi = (
        f'<span class="multi" title="mesmo conteúdo visto em {len(prov)} lugares">'
        f"◆ {len(prov)} proveniências</span>"
        if len(prov) > 1
        else ""
    )
    return f"""
<tr data-busca="{html.escape((r['title'] or '').lower())} {r['source']} {r['tier']} {html.escape(termos.lower())}"
    data-tier="{r['tier']}" data-source="{r['source']}">
  <td><span class="faixa" style="color:{cor};background:{fundo}">{r['tier']}</span></td>
  <td class="score">{r['lexicon_score']:.1f}</td>
  <td>
    <div class="titulo">{html.escape(r['title'] or '(sem título)')}</div>
    <div class="meta">
      <span class="fonte">{r['source']}</span>
      <span>{html.escape(str(r['source_id']))}</span>
      <span>{tamanho}</span><span>{paginas}</span>{multi}
    </div>
    {f'<div class="termos">{html.escape(termos)}</div>' if termos else ''}
  </td>
  <td class="acoes">
    <a href="file:///{caminho}" title="abrir o arquivo local">arquivo</a>
    <a href="{html.escape(r['landing_url'] or '#')}" target="_blank" title="página de origem">origem</a>
    <code title="SHA-256 do conteúdo">{(r['sha256'] or '')[:10]}</code>
  </td>
</tr>"""


def gerar(db: Path, saida: Path, tiers: list[str] | None = None, limite: int | None = None) -> int:
    linhas = _linhas(db, tiers, limite)
    fontes = sorted({r["source"] for r in linhas})
    faixas = sorted({r["tier"] for r in linhas if r["tier"]})

    botoes = "".join(f'<button data-f="tier" data-v="{t}">{t}</button>' for t in faixas)
    botoes += "".join(f'<button data-f="source" data-v="{s}">{s}</button>' for s in fontes)

    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(
        _TEMPLATE.replace("{{TOTAL}}", str(len(linhas)))
        .replace("{{BOTOES}}", botoes)
        .replace("{{LINHAS}}", "".join(_cartao(r, db.parent) for r in linhas)),
        encoding="utf-8",
    )
    return len(linhas)


_TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>Corpus ConOps — índice</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: "Segoe UI", system-ui, sans-serif; margin: 0; padding: 20px 26px;
         background: #fafbfc; color: #1a1a1a; }
  h1 { font-size: 17pt; margin: 0 0 3px; color: #0b3a5d; }
  .sub { color: #666; font-size: 9.5pt; margin-bottom: 14px; }
  .barra { position: sticky; top: 0; background: #fafbfc; padding: 10px 0 12px;
           border-bottom: 1px solid #dde3e9; z-index: 5; }
  input[type=search] { width: 340px; padding: 7px 11px; font-size: 10.5pt;
                       border: 1px solid #c3ccd5; border-radius: 5px; }
  button { padding: 4px 11px; margin: 0 3px 3px 0; font-size: 9pt; cursor: pointer;
           border: 1px solid #c3ccd5; background: #fff; border-radius: 11px; }
  button.on { background: #0b3a5d; color: #fff; border-color: #0b3a5d; }
  #contador { margin-left: 12px; color: #666; font-size: 9.5pt; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px; }
  td { padding: 8px 9px; border-bottom: 1px solid #e6ebf0; vertical-align: top; }
  tr:hover td { background: #f2f6fa; }
  .faixa { font-size: 7.8pt; font-weight: 700; padding: 2px 7px; border-radius: 9px;
           white-space: nowrap; }
  .score { font-variant-numeric: tabular-nums; color: #555; font-size: 9.5pt;
           text-align: right; width: 46px; }
  .titulo { font-weight: 600; font-size: 10.5pt; line-height: 1.3; }
  .meta { font-size: 8.6pt; color: #667; margin-top: 3px; }
  .meta span { margin-right: 11px; }
  .fonte { background: #e8eef4; padding: 1px 6px; border-radius: 3px; font-weight: 600; }
  .multi { color: #7a5800; font-weight: 600; }
  .termos { font-size: 8.4pt; color: #2c6e49; margin-top: 3px; font-style: italic; }
  .acoes { white-space: nowrap; text-align: right; font-size: 9pt; }
  .acoes a { margin-left: 9px; color: #1c5c8c; text-decoration: none; }
  .acoes a:hover { text-decoration: underline; }
  .acoes code { display: block; font-size: 7.6pt; color: #99a; margin-top: 4px; }
  @media (prefers-color-scheme: dark) {
    body, .barra { background: #14181c; color: #e6e6e6; }
    h1 { color: #7fb6dd; } td { border-color: #262c33; }
    tr:hover td { background: #1c2229; }
    input[type=search], button { background: #1c2229; color: #e6e6e6; border-color: #39414a; }
    .fonte { background: #26303a; } .acoes a { color: #7fb6dd; }
  }
</style>

<h1>Corpus ConOps — índice navegável</h1>
<div class="sub">
  Os arquivos em disco têm o SHA-256 como nome. Esta página liga título → arquivo.
  Somente leitura.
</div>

<div class="barra">
  <input type="search" id="q" placeholder="buscar por título, fonte, termo casado…" autofocus>
  <span id="contador"></span>
  <div style="margin-top:8px">{{BOTOES}}</div>
</div>

<table><tbody id="corpo">{{LINHAS}}</tbody></table>

<script>
  const linhas = [...document.querySelectorAll('#corpo tr')];
  const filtros = { tier: new Set(), source: new Set() };
  const q = document.getElementById('q');

  function aplicar() {
    const termo = q.value.toLowerCase().trim();
    let n = 0;
    for (const tr of linhas) {
      const casaTexto = !termo || tr.dataset.busca.includes(termo);
      const casaTier = !filtros.tier.size || filtros.tier.has(tr.dataset.tier);
      const casaFonte = !filtros.source.size || filtros.source.has(tr.dataset.source);
      const ok = casaTexto && casaTier && casaFonte;
      tr.style.display = ok ? '' : 'none';
      if (ok) n++;
    }
    document.getElementById('contador').textContent = n + ' de {{TOTAL}} documentos';
  }

  q.addEventListener('input', aplicar);
  for (const b of document.querySelectorAll('button[data-f]')) {
    b.addEventListener('click', () => {
      const conjunto = filtros[b.dataset.f];
      conjunto.has(b.dataset.v) ? conjunto.delete(b.dataset.v) : conjunto.add(b.dataset.v);
      b.classList.toggle('on');
      aplicar();
    });
  }
  aplicar();
</script>
"""
