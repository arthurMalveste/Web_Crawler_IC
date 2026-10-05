"""Etapa 2 — extracao de texto dos PDFs do corpus com o Docling.

Separada da Etapa 1 (crawler/) de proposito: so LE o que a coleta deixou no
corpus (frontier.sqlite + raw/) e escreve o Markdown em <corpus>/markdown/.

Versao de teste: converte 5 PDFs do NTRS e 5 de outras fontes.

    .venv\\Scripts\\python etapa2\\extrair.py
"""

import os
import sqlite3
import time
from pathlib import Path

from docling.document_converter import DocumentConverter

REPO = Path(__file__).resolve().parents[1]
CORPUS = (REPO / os.environ.get("CONOPS_DATA_ROOT", "../ConOpsCorpus")).resolve()
SAIDA = CORPUS / "markdown"
N = 5

# Uma fonte por vez nas "outras" (round-robin), para a amostra nao sair toda da mesma.
SQL = """
SELECT source, sha256, stored_path FROM (
    SELECT source, sha256, stored_path,
           ROW_NUMBER() OVER (PARTITION BY source ORDER BY key) AS n
    FROM documents WHERE status = 'stored' AND content_kind = 'pdf'
) WHERE (source = 'ntrs') = ? ORDER BY n, source LIMIT ?
"""


def main() -> None:
    con = sqlite3.connect(CORPUS / "frontier.sqlite")
    docs = con.execute(SQL, (True, N)).fetchall() + con.execute(SQL, (False, N)).fetchall()
    con.close()

    SAIDA.mkdir(exist_ok=True)
    conversor = DocumentConverter()

    for fonte, sha, caminho in docs:
        destino = SAIDA / f"{sha}.md"
        if destino.exists():
            print(f"{fonte:12} {sha[:12]}  ja convertido")
            continue
        inicio = time.perf_counter()
        try:
            doc = conversor.convert(CORPUS / caminho).document
        except Exception as exc:  # um PDF ruim nao derruba o lote
            print(f"{fonte:12} {sha[:12]}  ERRO: {exc}")
            continue
        md = doc.export_to_markdown()
        destino.write_text(md, encoding="utf-8")
        print(
            f"{fonte:12} {sha[:12]}  {doc.num_pages():4} pags  {len(md):8} chars  "
            f"{time.perf_counter() - inicio:6.1f}s"
        )


if __name__ == "__main__":
    main()
