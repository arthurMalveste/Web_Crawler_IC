"""Etapa 2 — extracao de texto dos PDFs do corpus com o Docling.

Separada da Etapa 1 (crawler/) de proposito: so LE o que a coleta deixou no
corpus (frontier.sqlite + raw/) e escreve o Markdown em <corpus>/markdown/,
registrando cada conversao em <corpus>/extracao.jsonl.

    .venv\\Scripts\\python etapa2\\extrair.py

Pode ser interrompido e rodado de novo: o que ja foi convertido e pulado.
"""

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

REPO = Path(__file__).resolve().parents[1]
CORPUS = (REPO / os.environ.get("CONOPS_DATA_ROOT", "../ConOpsCorpus")).resolve()
SAIDA = CORPUS / "markdown"
LOG = CORPUS / "extracao.jsonl"

# NTRS fora por enquanto: o padrao dele e o .txt da NASA; converter os PDFs
# (~1,7 h para 152) fica pendente de decisao com as orientadoras.
SQL = """
SELECT source, source_id, sha256, stored_path FROM documents
WHERE status = 'stored' AND content_kind = 'pdf' AND source != 'ntrs'
ORDER BY source, key
"""

# Palavras (3+ letras) por 1000 caracteres "de conteudo" — sem espacos e sem
# o enchimento das tabelas Markdown (| e -), senao um relatorio so de tabelas
# numericas parece ilegivel. Texto normal fica bem acima de 100; PDF com fonte
# de codificacao quebrada sai como simbolos e fica perto de 0.
MIN_PALAVRAS_POR_MIL = 30


def legivel(md: str) -> bool:
    palavras = len(re.findall(r"[A-Za-z]{3,}", md))
    conteudo = len(re.sub(r"[\s|\-]", "", md))
    return palavras * 1000 / max(conteudo, 1) >= MIN_PALAVRAS_POR_MIL


def conversor(ocr: bool) -> DocumentConverter:
    opcoes = PdfPipelineOptions(do_ocr=ocr)
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opcoes)})


def main() -> None:
    con = sqlite3.connect(CORPUS / "frontier.sqlite")
    docs = con.execute(SQL).fetchall()
    con.close()

    SAIDA.mkdir(exist_ok=True)
    # Sem OCR por padrao: os PDFs do corpus ja tem camada de texto, e com OCR
    # ligado o Docling passou de 25 min num PDF de 118 pags sem terminar
    # (sem OCR: 157 s). OCR so para quem sai ilegivel.
    sem_ocr, com_ocr = conversor(False), None

    for fonte, sid, sha, caminho in docs:
        destino = SAIDA / f"{sha}.md"
        if destino.exists():
            continue
        inicio = time.perf_counter()
        reg = {"sha256": sha, "source": fonte, "source_id": sid, "ocr": False}
        try:
            doc = sem_ocr.convert(CORPUS / caminho).document
            md = doc.export_to_markdown()
            if not legivel(md):
                com_ocr = com_ocr or conversor(True)
                doc = com_ocr.convert(CORPUS / caminho).document
                md = doc.export_to_markdown()
                reg["ocr"] = True
            destino.write_text(md, encoding="utf-8")
            reg.update(paginas=doc.num_pages(), chars=len(md), legivel=legivel(md))
        except Exception as exc:  # um PDF ruim nao derruba o lote
            reg["erro"] = str(exc)[:300]
        reg["segundos"] = round(time.perf_counter() - inicio, 1)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(reg, ensure_ascii=False) + "\n")
        print(json.dumps(reg, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
