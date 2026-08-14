"""Extracao de links e de metadados a partir de HTML.

Duas responsabilidades:

  1. LINKS — com o texto da ancora e o contexto ao redor, que sao a evidencia
     de que o LinkScorer precisa para ordenar a fronteira.
  2. METADADOS — a parte que costuma ser subestimada. Repositorios
     institucionais quase sempre emitem metadados estruturados no `<head>`:
     Highwire Press (`citation_*`, o padrao do Google Scholar), Dublin Core
     (`DC.*`), OpenGraph e JSON-LD schema.org. Ler essas tags e o que permite a
     uma fonte HTML entregar titulo, autores, data e resumo com a mesma
     qualidade de uma API — sem isso o crawler traz um PDF sem procedencia.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..core.record import canonical_url

#: Esquemas que nunca sao seguidos.
ESQUEMAS_IGNORADOS = ("mailto:", "javascript:", "tel:", "data:", "ftp:", "#")

#: Placeholders de template que vazam para o HTML servido — `{{ url }}`,
#: `[auth_url]`, `${link}`, `<%= x %>`. Nao sao enderecos: seguir cada um custa
#: uma requisicao e um 404 garantido.
_PLACEHOLDER = re.compile(r"(\{\{.*?\}\}|\{%.*?%\}|\$\{.*?\}|<%.*?%>|^\[[^\]]+\]$)")


@dataclass
class ExtractedLink:
    url: str
    anchor: str
    context: str
    is_document: bool = False


@dataclass
class PageMetadata:
    """Metadados de uma landing page, normalizados a partir de varios padroes."""

    title: str | None = None
    authors: list[str] = field(default_factory=list)
    abstract: str | None = None
    date: str | None = None
    publisher: str | None = None
    doi: str | None = None
    subjects: list[str] = field(default_factory=list)
    #: `citation_pdf_url` — a propria pagina declarando onde esta o PDF. Quando
    #: existe, dispensa adivinhar por padrao de link.
    pdf_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.title or self.authors or self.abstract or self.pdf_url)


def parse_html(body: bytes | str) -> BeautifulSoup:
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    return BeautifulSoup(body, "lxml")


# ------------------------------------------------------------------- links


def extract_links(
    soup: BeautifulSoup,
    base_url: str,
    document_extensions: tuple[str, ...],
) -> list[ExtractedLink]:
    """Coleta os links da pagina com ancora e contexto.

    Resolve URLs relativas contra a base (respeitando `<base href>`) e remove
    fragmentos: `pagina#secao` e a mesma pagina e visita-la duas vezes e
    desperdicio de orcamento.
    """
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = urljoin(base_url, base_tag["href"])

    vistos: set[str] = set()
    out: list[ExtractedLink] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.lower().startswith(ESQUEMAS_IGNORADOS):
            continue
        if _PLACEHOLDER.search(href):
            continue

        # HTML real traz href malformado com frequencia: placeholders de
        # template (`[auth_url]`, `{{link}}`), colchetes que o urlsplit tenta
        # interpretar como IPv6, esquemas invalidos. Um unico deles levantava
        # ValueError e derrubava o rastreamento inteiro — visto no ESA EOF.
        # Link ruim se descarta; nao se aborta a coleta por causa dele.
        try:
            absoluta = urljoin(base_url, href)
            absoluta, _ = urldefrag(absoluta)
            if not absoluta.lower().startswith(("http://", "https://")):
                continue
            absoluta = _quote_path(_alinhar_esquema(absoluta, base_url))
            chave = canonical_url(absoluta)
        except ValueError:
            continue
        if chave in vistos:
            continue
        vistos.add(chave)

        ancora = " ".join(a.get_text(" ", strip=True).split())[:300]
        # `title` costuma trazer o nome completo quando a ancora e so um icone.
        if not ancora and a.get("title"):
            ancora = a["title"].strip()[:300]

        out.append(
            ExtractedLink(
                url=absoluta,
                anchor=ancora,
                context=_contexto(a),
                is_document=_e_documento(absoluta, document_extensions),
            )
        )
    return out


def _alinhar_esquema(url: str, base_url: str) -> str:
    """Alinha http:// com https:// no mesmo host.

    Caso real do ROSA P: a mesma pagina traz o mesmo PDF ora como `http://`,
    ora como `https://`. Sao um unico recurso, mas entrariam na fronteira como
    dois — dobrando a contagem de documentos e gastando o orcamento duas vezes
    no mesmo arquivo. Se a pagina que contem o link foi servida por https, o
    site suporta https e a variante http e apenas um link desatualizado.
    """
    parts = urlsplit(url)
    base = urlsplit(base_url)
    if parts.scheme == "http" and base.scheme == "https" and parts.netloc.lower() == base.netloc.lower():
        return urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))
    return url


def _quote_path(url: str) -> str:
    """Escapa caracteres nao seguros no caminho, preservando o que ja esta escapado.

    Caso real do acervo MIT PSAS: nomes de arquivo com espacos
    (`[STAMP Mar26] STPA Practice.pdf`) e nao-ASCII (`Andrej_Lalis__PUB.pdf`).
    Sem escapar, a requisicao falha; escapando duas vezes, tambem.
    """
    parts = urlsplit(url)
    caminho = quote(parts.path, safe="/%:@&=+$,~()[]!*'")
    return f"{parts.scheme}://{parts.netloc}{caminho}" + (f"?{parts.query}" if parts.query else "")


def _e_documento(url: str, extensoes: tuple[str, ...]) -> bool:
    caminho = urlsplit(url).path.lower()
    return caminho.endswith(extensoes)


def _contexto(tag, janela: int = 200) -> str:
    """Texto ao redor do link — util quando a ancora e pobre ("PDF", "aqui")."""
    pai = tag.find_parent(["p", "li", "td", "div", "section"]) or tag.parent
    if pai is None:
        return ""
    texto = " ".join(pai.get_text(" ", strip=True).split())
    return texto[: janela * 2]


# --------------------------------------------------------------- metadados


def extract_metadata(soup: BeautifulSoup, page_url: str) -> PageMetadata:
    """Le metadados estruturados do `<head>`, em ordem de qualidade.

    Highwire (`citation_*`) primeiro porque e o padrao dos repositorios
    academicos e o mais completo; Dublin Core em seguida; OpenGraph e JSON-LD
    como complemento; `<title>` como ultimo recurso.
    """
    metas = _colher_metas(soup)
    md = PageMetadata(raw=metas)

    # 1. Highwire Press — padrao do Google Scholar.
    md.title = _primeiro(metas, "citation_title")
    md.authors = _todos(metas, "citation_author")
    md.abstract = _primeiro(metas, "citation_abstract")
    md.date = _primeiro(metas, "citation_publication_date", "citation_date", "citation_online_date")
    md.publisher = _primeiro(metas, "citation_publisher", "citation_journal_title")
    md.doi = _primeiro(metas, "citation_doi")
    md.subjects = _todos(metas, "citation_keywords")
    pdf = _primeiro(metas, "citation_pdf_url", "citation_fulltext_html_url")
    if pdf:
        md.pdf_url = urljoin(page_url, pdf)

    # 2. Dublin Core.
    md.title = md.title or _primeiro(metas, "dc.title", "dcterms.title")
    md.authors = md.authors or _todos(metas, "dc.creator", "dc.contributor")
    md.abstract = md.abstract or _primeiro(metas, "dcterms.abstract", "dc.description")
    md.date = md.date or _primeiro(metas, "dc.date", "dcterms.issued", "dcterms.created")
    md.publisher = md.publisher or _primeiro(metas, "dc.publisher")
    md.subjects = md.subjects or _todos(metas, "dc.subject")
    md.doi = md.doi or _primeiro(metas, "dc.identifier.doi")

    # 3. OpenGraph e meta description.
    md.title = md.title or _primeiro(metas, "og:title")
    md.abstract = md.abstract or _primeiro(metas, "og:description", "description")

    # 4. JSON-LD schema.org.
    if md.is_empty or not md.abstract:
        _do_jsonld(soup, md)

    # 4.5 Wrapper de download por redirecionamento JS (achado real do CORDIS,
    # 2026-08-12 — ver docstring de `_wrapper_de_download_js`). So' entra em
    # jogo quando nenhum sinal melhor (citation_pdf_url etc.) ja resolveu o
    # PDF.
    if not md.pdf_url:
        alvo_js = _wrapper_de_download_js(soup, page_url)
        if alvo_js:
            md.pdf_url = alvo_js

    # 5. Ultimo recurso — MAS NAO para o wrapper de download: seu <title> e'
    # sempre o mesmo texto generico ("Documents download module"), igual em
    # milhares de arquivos distintos. Usa-lo aqui daria a todos esses
    # documentos o mesmo titulo errado; melhor deixar `md.title` vazio e
    # deixar `_melhor_titulo` (crawler.py) cair para a ancora que levou ate
    # esta pagina, que e' a informacao real (ex.: o nome do entregavel, tal
    # como aparece na pagina do projeto no CORDIS).
    titulo_bruto = soup.title.string if (soup.title and soup.title.string) else None
    if not md.title and titulo_bruto and titulo_bruto.strip().lower() != _TITULO_WRAPPER_GENERICO:
        md.title = " ".join(titulo_bruto.split())[:400]

    md.authors = [a for a in (x.strip() for x in md.authors) if a][:60]
    md.subjects = _dividir_assuntos(md.subjects)
    return md


#: Padrao de redirecionamento por JAVASCRIPT do "Documents download module"
#: da Comissao Europeia (`ec.europa.eu/research/participants/documents/...` —
#: usado pelo CORDIS para servir entregaveis, entre outros servicos). O link
#: publicado (na pagina do projeto, ou no CSV bulk) NUNCA e' o arquivo em si:
#: e' uma pagina HTML que so' entrega o endereco real via
#: `window.location='...'` dentro de um <script>, sem `<a href>` nem
#: redirecionamento HTTP — nenhum downloader HTTP puro segue isso sozinho.
#: Achado real, verificado ao vivo em 2026-08-12 (as duas requisicoes
#: precisam compartilhar sessao/cookies; o `Fetcher` ja usa um unico
#: `httpx.Client` para tudo, entao isso funciona sem tratamento especial).
_JS_REDIRECT_RE = re.compile(r"window\.location\s*=\s*['\"]([^'\"]+)['\"]")

_TITULO_WRAPPER_GENERICO = "documents download module"


def _wrapper_de_download_js(soup: BeautifulSoup, page_url: str) -> str | None:
    """Reconhece o wrapper de download acima e devolve o endereco real.

    Escopo deliberadamente ESTREITO: so' dispara quando a URL PEDIDA ja' bate
    no caminho conhecido deste modulo especifico — nao em qualquer pagina com
    `window.location` por ai'. Login walls, paywalls e paginas de consentimento
    de cookies tambem usam redirecionamento por JS, e tratar isso como "aqui
    esta' o documento" fora deste contexto seria um falso positivo real (o
    mesmo cuidado de `traps.py::pagina_redirecionada_suspeita`, que tambem
    exige o padrao no CAMINHO, nao so' no conteudo).
    """
    if "/documents/downloadpublic" not in urlsplit(page_url).path.lower():
        return None
    for script in soup.find_all("script"):
        m = _JS_REDIRECT_RE.search(script.string or "")
        if m:
            return urljoin(page_url, m.group(1))
    return None


def _colher_metas(soup: BeautifulSoup) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in soup.find_all("meta"):
        nome = (m.get("name") or m.get("property") or m.get("http-equiv") or "").strip().lower()
        conteudo = (m.get("content") or "").strip()
        if nome and conteudo:
            out.setdefault(nome, []).append(conteudo)
    return out


def _primeiro(metas: dict[str, list[str]], *chaves: str) -> str | None:
    for k in chaves:
        vals = metas.get(k.lower())
        if vals:
            return vals[0]
    return None


def _todos(metas: dict[str, list[str]], *chaves: str) -> list[str]:
    out: list[str] = []
    for k in chaves:
        out.extend(metas.get(k.lower(), []))
    return out


def _dividir_assuntos(valores: list[str]) -> list[str]:
    """`citation_keywords` costuma vir como "a; b; c" num unico campo."""
    out: list[str] = []
    for v in valores:
        out.extend(p.strip() for p in re.split(r"[;,]", v) if p.strip())
    return out[:60]


def _do_jsonld(soup: BeautifulSoup, md: PageMetadata) -> None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            dados = json.loads(tag.string or "{}")
        except (ValueError, TypeError):
            continue
        for no in dados if isinstance(dados, list) else [dados]:
            if not isinstance(no, dict):
                continue
            md.title = md.title or _texto(no.get("name") or no.get("headline"))
            md.abstract = md.abstract or _texto(no.get("description") or no.get("abstract"))
            md.date = md.date or _texto(no.get("datePublished") or no.get("dateCreated"))
            autor = no.get("author")
            if autor and not md.authors:
                if isinstance(autor, list):
                    md.authors = [_texto(a.get("name") if isinstance(a, dict) else a) or "" for a in autor]
                elif isinstance(autor, dict):
                    md.authors = [_texto(autor.get("name")) or ""]
                else:
                    md.authors = [_texto(autor) or ""]


def _texto(v: Any) -> str | None:
    if isinstance(v, str) and v.strip():
        return " ".join(v.split())[:4000]
    return None
