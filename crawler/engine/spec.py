"""SourceSpec — descricao declarativa de uma fonte.

Adicionar uma fonte nova deve custar um arquivo YAML, nao um modulo Python. E
isso que da ao projeto a flexibilidade de fonte exigida pelo objetivo especifico
1 da IC ("navegacao e coleta em repositorios tecnicos e institucionais"): quando
o orientador ou a Embraer apontarem um repositorio novo, escreve-se um spec.

Os adaptadores de API (NTRS, ROSA P) continuam existindo como especializacao:
onde ha interface estruturada ela e preferivel: mais rapida, mais rica em
metadados e mais leve para o servidor. O crawler generico e o caso geral, nao o
ultimo recurso.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

#: Estrategias de expansao da fronteira.
STRATEGY_FOCUSED = "focused"  # ordena por relevancia prevista do link
STRATEGY_BFS = "bfs"  # largura pura — baseline de comparacao

#: Como a fonte e alcancada.
KIND_CRAWL = "crawl"  # navegacao HTML (o caso geral)
KIND_API = "api"  # adaptador dedicado (NTRS)
KIND_OAI = "oai"  # OAI-PMH (ROSA P)
KIND_BULK = "bulk"  # dump CSV/XML (CORDIS)
KIND_SEEDS = "seeds"  # lista fixa de URLs conhecidas

DEFAULT_DOC_EXTENSIONS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx")


@dataclass
class Scope:
    """Limites do rastreamento.

    Sem escopo explicito, "rastrear faa.gov" vira rastrear a internet: um unico
    link para fora leva a um dominio que leva a outro. Os limites sao
    obrigatorios, nao opcionais.
    """

    allow_hosts: list[str] = field(default_factory=list)
    #: Regex de caminho que, se casar, autoriza seguir. Vazio = qualquer um.
    allow_paths: list[str] = field(default_factory=list)
    #: Regex de caminho que, se casar, proibe seguir. Tem precedencia sobre allow.
    deny_paths: list[str] = field(default_factory=list)
    max_depth: int = 3
    #: Orcamento de paginas HTML baixadas. E o freio de emergencia do crawler.
    max_pages: int = 2000
    #: Teto de documentos aceitos. `None` = sem teto.
    max_documents: int | None = None
    #: Seguir apenas links no mesmo host da semente.
    same_host_only: bool = True

    def __post_init__(self) -> None:
        self._allow_re = [re.compile(p, re.I) for p in self.allow_paths]
        self._deny_re = [re.compile(p, re.I) for p in self.deny_paths]
        self.allow_hosts = [h.lower() for h in self.allow_hosts]

    def host_allowed(self, host: str) -> bool:
        host = host.lower()
        if not self.allow_hosts:
            return True
        return any(host == h or host.endswith("." + h) for h in self.allow_hosts)

    def path_allowed(self, path: str) -> bool:
        if any(r.search(path) for r in self._deny_re):
            return False
        if not self._allow_re:
            return True
        return any(r.search(path) for r in self._allow_re)


@dataclass
class SourceSpec:
    name: str
    kind: str = KIND_CRAWL
    seeds: list[str] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    strategy: str = STRATEGY_FOCUSED

    #: Extensoes tratadas como documento (nao como pagina a expandir).
    document_extensions: tuple[str, ...] = DEFAULT_DOC_EXTENSIONS

    #: Regex de URL que tambem identificam documento, para fontes onde o
    #: endereco nao carrega extensao. Casos reais: o Liferay da ESA serve
    #: `/documents/{groupId}/{folderId}/{nome}/{uuid}` e a DTIC serve
    #: `/sti/pdfs/AD1113565` — nos dois a extensao pode faltar, e depender dela
    #: tornaria o documento invisivel para o crawler.
    document_patterns: list[str] = field(default_factory=list)

    #: `auto` procura /sitemap.xml e o declarado em robots.txt; uma URL usa
    #: aquele sitemap; `none` desliga. Sitemap e sempre mais barato que BFS.
    sitemap: str = "auto"

    #: Renderizacao JS via Playwright. So liga onde o HTML servido nao contem
    #: os links (ESA EOF carrega os documentos por JavaScript).
    render: bool = False
    render_wait_ms: int = 2500
    render_wait_selector: str | None = None

    #: Opcoes livres consumidas por adaptadores especializados (api/oai/bulk).
    options: dict[str, Any] = field(default_factory=dict)

    notes: str | None = None

    def __post_init__(self) -> None:
        self._doc_re = [re.compile(p, re.I) for p in self.document_patterns]

    @property
    def is_crawl(self) -> bool:
        return self.kind in (KIND_CRAWL, KIND_SEEDS)

    def is_document_url(self, url: str) -> bool:
        path = url.split("?", 1)[0].split("#", 1)[0].lower()
        if path.endswith(self.document_extensions):
            return True
        return any(r.search(url) for r in self._doc_re)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceSpec":
        d = dict(d)
        scope = Scope(**(d.pop("scope", {}) or {}))
        exts = d.pop("document_extensions", None)
        spec = cls(
            scope=scope,
            document_extensions=tuple(e.lower() for e in exts) if exts else DEFAULT_DOC_EXTENSIONS,
            **{k: v for k, v in d.items() if k in cls.__dataclass_fields__},
        )
        if spec.strategy not in (STRATEGY_FOCUSED, STRATEGY_BFS):
            raise ValueError(f"{spec.name}: estrategia invalida {spec.strategy!r}")
        return spec

    @classmethod
    def load(cls, path: str | Path) -> "SourceSpec":
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        data.setdefault("name", Path(path).stem)
        return cls.from_dict(data)

    @classmethod
    def load_all(cls, directory: str | Path) -> dict[str, "SourceSpec"]:
        out: dict[str, SourceSpec] = {}
        for p in sorted(Path(directory).glob("*.yaml")):
            spec = cls.load(p)
            out[spec.name] = spec
        return out


def iter_specs(directory: str | Path, names: Iterable[str] | None = None) -> list[SourceSpec]:
    specs = SourceSpec.load_all(directory)
    if names is None:
        return list(specs.values())
    faltando = [n for n in names if n not in specs]
    if faltando:
        raise SystemExit(f"fonte(s) sem spec: {', '.join(faltando)}")
    return [specs[n] for n in names]
