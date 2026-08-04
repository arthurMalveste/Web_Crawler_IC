"""Renderiza docs/sistema.html em PDF usando o Chromium do Playwright.

Playwright ja e dependencia do projeto (renderizacao JS do ESA EOF), entao
gerar o PDF nao acrescenta nenhuma dependencia nova. O Chromium da suporte
completo a CSS de impressao — quebras de pagina, cabecalho e rodape — que
bibliotecas de PDF em Python puro nao oferecem.

    python docs/gerar_pdf.py
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

AQUI = Path(__file__).resolve().parent
ENTRADA = AQUI / "sistema.html"
SAIDA = AQUI / "ConOps-Retrieval-Pipeline-Documentacao.pdf"

RODAPE = """
<div style="width:100%; font-size:7.5pt; color:#888; padding:0 18mm;
            font-family:'Segoe UI',Arial,sans-serif; display:flex;
            justify-content:space-between; border-top:1px solid #ddd; padding-top:3px;">
  <span>ConOps Retrieval Pipeline &mdash; Etapa 1 &mdash; FT/UNICAMP</span>
  <span><span class="pageNumber"></span> / <span class="totalPages"></span></span>
</div>
"""

CABECALHO = '<div style="font-size:1pt;">&nbsp;</div>'


def main() -> None:
    if not ENTRADA.exists():
        raise SystemExit(f"nao encontrei {ENTRADA}")

    with sync_playwright() as pw:
        navegador = pw.chromium.launch()
        pagina = navegador.new_page()
        pagina.goto(ENTRADA.as_uri(), wait_until="networkidle")
        pagina.pdf(
            path=str(SAIDA),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template=CABECALHO,
            footer_template=RODAPE,
            margin={"top": "14mm", "bottom": "16mm", "left": "16mm", "right": "16mm"},
        )
        navegador.close()

    print(f"PDF gerado: {SAIDA}  ({SAIDA.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
