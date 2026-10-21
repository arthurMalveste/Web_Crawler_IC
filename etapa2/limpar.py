"""Etapa 2 — pre-processamento minimo: um texto limpo por documento.

Junta as duas origens de texto do corpus numa pasta so, <corpus>/texto/,
com o indice <corpus>/texto/indice.jsonl (entrada da Etapa 3):

- PDFs convertidos pelo Docling (<corpus>/markdown/, ver extrair.py);
- NTRS sem PDF: o .txt que a propria NASA entrega (as vezes em XHTML do Tika).

A limpeza e a mesma para as duas e so tira ruido de layout — nao julga
conteudo. Os titulos "##" do Docling ficam: a Etapa 3 usa as secoes.

    python etapa2\\limpar.py
"""

import collections
import html
import json
import os
import re
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORPUS = (REPO / os.environ.get("CONOPS_DATA_ROOT", "../ConOpsCorpus")).resolve()
SAIDA = CORPUS / "texto"

NUM_PAGINA = re.compile(r"\s*(page\s*)?\d{1,4}\s*", re.I)


def limpar(texto: str) -> str:
    linhas = [l.rstrip() for l in texto.splitlines() if l.strip() != "<!-- image -->"]
    linhas = [l for l in linhas if not NUM_PAGINA.fullmatch(l)]
    # Cabecalho/rodape de pagina: a mesma linha longa repetida em muitas paginas.
    # Linhas curtas ficam (no .txt da NASA uma palavra solta por linha e comum),
    # e o piso de 10 repeticoes poupa linhas de tabela que se repetem algumas
    # vezes — custo: em documento com menos de 10 paginas o cabecalho fica.
    contagem = collections.Counter(l.strip() for l in linhas)
    linhas = [
        l for l in linhas
        if len(l.strip()) < 15 or contagem[l.strip()] < 10 or l.lstrip().startswith(("|", "#"))
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip() + "\n"


def palavras(texto: str) -> int:
    return len(re.findall(r"[A-Za-z]{3,}", texto))


def legivel(texto: str) -> bool:
    """Mesmo criterio de extrair.py: palavras por 1000 caracteres de conteudo."""
    conteudo = len(re.sub(r"[\s|\-]", "", texto))
    return palavras(texto) * 1000 / max(conteudo, 1) >= 30


def main() -> None:
    con = sqlite3.connect(CORPUS / "frontier.sqlite")
    docs = con.execute(
        "SELECT source, source_id, title, sha256, stored_path, content_kind FROM documents"
        " WHERE status = 'stored' AND content_kind IN ('pdf', 'text', 'html')"
        # PDFs do NTRS fora por enquanto (ver extrair.py)
        " AND NOT (source = 'ntrs' AND content_kind = 'pdf')"
    ).fetchall()
    con.close()

    ocr = set()
    log = CORPUS / "extracao.jsonl"
    if log.exists():
        for linha in log.read_text(encoding="utf-8").splitlines():
            reg = json.loads(linha)
            if reg.get("ocr"):
                ocr.add(reg["sha256"])

    SAIDA.mkdir(exist_ok=True)
    indice, faltando = [], 0
    for fonte, sid, titulo, sha, caminho, tipo in docs:
        if tipo == "pdf":
            md = CORPUS / "markdown" / f"{sha}.md"
            if not md.exists():  # conversao falhou ou ainda nao rodou
                faltando += 1
                continue
            bruto = md.read_text(encoding="utf-8")
            origem = "docling_ocr" if sha in ocr else "docling"
        else:
            bruto = (CORPUS / caminho).read_text(encoding="utf-8", errors="replace")
            if tipo == "html":  # invólucro XHTML do Apache Tika no fulltext do NTRS
                bruto = html.unescape(re.sub(r"<[^>]+>", "\n", bruto))
            origem = "ntrs_txt"

        texto = limpar(bruto)
        (SAIDA / f"{sha}.txt").write_text(texto, encoding="utf-8")
        n = palavras(texto)
        indice.append({
            "sha256": sha, "source": fonte, "source_id": sid, "title": titulo,
            "origem": origem, "chars_antes": len(bruto), "chars": len(texto), "palavras": n,
            "legivel": legivel(texto),
        })

    with (SAIDA / "indice.jsonl").open("w", encoding="utf-8") as f:
        for reg in indice:
            f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    print(f"{len(indice)} textos em {SAIDA}  ({faltando} PDFs ainda sem Markdown)")
    print(collections.Counter(r["origem"] for r in indice))
    print("ilegiveis:", sum(not r["legivel"] for r in indice))


if __name__ == "__main__":
    main()
