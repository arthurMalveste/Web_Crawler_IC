"""Camada de fetch: um downloader, politicas por dominio.

Regras nao-negociaveis implementadas aqui:
  - robots.txt consultado e respeitado, com cache por dominio;
  - User-Agent identificavel e com contato;
  - blocklist de dominios que nunca devem ser acessados (restricao juridica);
  - tipo validado por magic bytes, nao por Content-Type (servidores
    governamentais erram esse cabecalho com frequencia);
  - teto de tamanho, verificado em streaming (nao so pelo Content-Length);
  - If-None-Match / If-Modified-Since nas reexecucoes;
  - backoff exponencial respeitando Retry-After.
"""

from __future__ import annotations

import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog
import yaml

from .env import carregar_dotenv, expandir_arvore, resolver_caminho

log = structlog.get_logger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# Assinaturas aceitas. Chave = extensao canonica do arquivo salvo.
MAGIC = {
    b"%PDF-": "pdf",
    b"PK\x03\x04": "zip",  # cobre .docx/.pptx/.xlsx
    b"\xd0\xcf\x11\xe0": "ole",  # .doc/.ppt/.xls legados
}


class BlockedByPolicy(Exception):
    """Recusa deliberada: blocklist, robots.txt ou teto de tamanho."""


class NotADocument(Exception):
    """O corpo baixado nao e um documento reconhecivel (tipicamente uma pagina
    de erro HTML devolvida com HTTP 200)."""


@dataclass
class FetchResult:
    url: str
    status: int
    body: bytes | None
    kind: str | None  # "pdf" | "zip" | "ole" | "text" | "json" | "xml" | "html"
    etag: str | None = None
    last_modified: str | None = None
    from_cache: bool = False  # 304 Not Modified
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.body is not None


@dataclass
class DomainPolicy:
    rate: float = 2.0
    concurrency: int = 1
    respect_robots: bool = True
    browser_ua: bool = False
    playwright_fallback: bool = False
    max_depth: int = 3
    timeout_s: float = 60.0
    #: Teto de tempo do corpo inteiro. O timeout do httpx e por operacao de
    #: leitura: um servidor que envia bytes em conta-gotas nunca o dispara e
    #: segura o pipeline indefinidamente. Este e o limite de relogio de parede.
    max_download_s: float = 300.0
    max_file_mb: float = 60.0
    retries: int = 5
    backoff_base_s: float = 2.0
    backoff_max_s: float = 120.0
    user_agent: str = ""


class Config:
    """Le config/domains.yaml e resolve a politica efetiva por dominio.

    O YAML pode referenciar variaveis de ambiente como `${VAR}` ou
    `${VAR:-default}`; elas vem do `.env` da raiz ou do ambiente. E o que
    mantem caminhos de UMA maquina fora de um arquivo versionado (ver
    `crawler/core/env.py`).
    """

    def __init__(self, raw: dict[str, Any]):
        carregar_dotenv()
        self.raw = raw = expandir_arvore(raw)
        self.data_root = resolver_caminho(raw.get("data_root", "data"))
        self.defaults = raw.get("defaults", {}) or {}
        self.domains = raw.get("domains", {}) or {}
        self.blocklist = {d.lower() for d in (raw.get("blocklist") or [])}

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path, encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    def policy_for(self, host: str) -> DomainPolicy:
        merged = dict(self.defaults)
        merged.update(self.domains.get(host.lower(), {}) or {})
        known = set(DomainPolicy.__dataclass_fields__)
        return DomainPolicy(**{k: v for k, v in merged.items() if k in known})

    def is_blocked(self, host: str) -> bool:
        host = host.lower()
        return any(host == b or host.endswith("." + b) for b in self.blocklist)


class Fetcher:
    def __init__(self, config: Config, client: httpx.Client | None = None):
        self.config = config
        self._client = client or httpx.Client(
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(float(config.defaults.get("timeout_s", 60))),
        )
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        # Preenchido a partir dos headers X-RateLimit-*: quando o servidor diz
        # quanto resta, obedecemos ao servidor em vez do palpite do YAML.
        self._rate_pause_until: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- politica

    def _ua(self, policy: DomainPolicy) -> str:
        return BROWSER_UA if policy.browser_ua else (policy.user_agent or BROWSER_UA)

    def _throttle(self, host: str, policy: DomainPolicy) -> None:
        now = time.monotonic()
        pause_until = self._rate_pause_until.get(host, 0.0)
        earliest = max(self._last_hit.get(host, 0.0) + policy.rate, pause_until)
        if earliest > now:
            time.sleep(earliest - now)
        self._last_hit[host] = time.monotonic()

    def _robots_allows(self, url: str, policy: DomainPolicy) -> bool:
        if not policy.respect_robots:
            return True
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if host not in self._robots:
            rp: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            robots_url = f"{parts.scheme}://{host}/robots.txt"
            try:
                r = self._client.get(
                    robots_url,
                    headers={"User-Agent": self._ua(policy)},
                    timeout=20.0,
                )
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    # Sem robots.txt legivel -> sem restricao declarada.
                    rp = None
            except httpx.HTTPError as exc:
                log.warning("robots.indisponivel", host=host, error=str(exc))
                rp = None
            self._robots[host] = rp
            log.info("robots.carregado", host=host, presente=rp is not None)
        rp = self._robots[host]
        if rp is None:
            return True
        return rp.can_fetch(self._ua(policy), url)

    def _note_rate_headers(self, host: str, headers: httpx.Headers) -> None:
        """Obedece ao X-RateLimit-* do servidor (NTRS publica os tres)."""
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        if remaining is None or reset is None:
            return
        try:
            rem, rst = int(remaining), int(reset)
        except ValueError:
            return
        if rem <= 5:
            wait = max(0.0, rst - time.time())
            self._rate_pause_until[host] = time.monotonic() + wait
            log.warning("ratelimit.quase_esgotado", host=host, restante=rem, espera_s=round(wait, 1))

    # ------------------------------------------------------------------ fetch

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str | None = None,
        expect_document: bool = False,
    ) -> FetchResult:
        """Baixa uma URL aplicando toda a politica. Levanta BlockedByPolicy
        quando a recusa e deliberada (blocklist/robots/tamanho)."""
        parts = urlsplit(url)
        host = parts.netloc.lower()

        if self.config.is_blocked(host):
            raise BlockedByPolicy(f"dominio em blocklist: {host}")

        policy = self.policy_for(host)

        if not self._robots_allows(url, policy):
            raise BlockedByPolicy(f"robots.txt proibe: {url}")

        headers = {"User-Agent": self._ua(policy)}
        if accept:
            headers["Accept"] = accept
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        max_bytes = int(policy.max_file_mb * 1024 * 1024)
        delay = policy.backoff_base_s
        last_exc: Exception | None = None

        for attempt in range(1, policy.retries + 1):
            self._throttle(host, policy)
            try:
                with self._client.stream(
                    "GET", url, headers=headers, timeout=policy.timeout_s
                ) as resp:
                    self._note_rate_headers(host, resp.headers)

                    if resp.status_code == 304:
                        return FetchResult(url, 304, None, None, etag, last_modified, True)

                    if resp.status_code == 429 or resp.status_code >= 500:
                        retry_after = resp.headers.get("retry-after")
                        wait = _parse_retry_after(retry_after) or delay
                        log.warning(
                            "fetch.retry",
                            url=url,
                            status=resp.status_code,
                            tentativa=attempt,
                            espera_s=round(wait, 1),
                        )
                        resp.close()
                        if attempt == policy.retries:
                            return FetchResult(url, resp.status_code, None, None)
                        time.sleep(wait)
                        delay = min(delay * 2, policy.backoff_max_s)
                        continue

                    if resp.status_code != 200:
                        return FetchResult(url, resp.status_code, None, None)

                    declared = resp.headers.get("content-length")
                    if declared and int(declared) > max_bytes:
                        resp.close()
                        raise BlockedByPolicy(
                            f"excede teto de {policy.max_file_mb} MB "
                            f"(Content-Length={int(declared) / 1e6:.1f} MB)"
                        )

                    # Le em streaming e aborta se estourar tamanho ou tempo — o
                    # Content-Length pode estar ausente ou mentir, e o timeout
                    # do httpx nao cobre transferencia lenta e continua.
                    chunks: list[bytes] = []
                    total = 0
                    inicio = time.monotonic()
                    for chunk in resp.iter_bytes(1 << 16):
                        total += len(chunk)
                        if total > max_bytes:
                            resp.close()
                            raise BlockedByPolicy(
                                f"excede teto de {policy.max_file_mb} MB durante o download"
                            )
                        decorrido = time.monotonic() - inicio
                        if decorrido > policy.max_download_s:
                            resp.close()
                            raise BlockedByPolicy(
                                f"download excedeu {policy.max_download_s:.0f} s "
                                f"({total / 1e6:.1f} MB recebidos)"
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)

                    kind = sniff_kind(body, resp.headers.get("content-type"))
                    if expect_document and kind not in ("pdf", "zip", "ole", "text"):
                        raise NotADocument(
                            f"conteudo nao e documento (kind={kind}, "
                            f"content-type={resp.headers.get('content-type')})"
                        )

                    return FetchResult(
                        url=str(resp.url),
                        status=200,
                        body=body,
                        kind=kind,
                        etag=resp.headers.get("etag"),
                        last_modified=resp.headers.get("last-modified"),
                        headers=dict(resp.headers),
                    )

            except (BlockedByPolicy, NotADocument):
                raise
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning("fetch.erro_rede", url=url, tentativa=attempt, error=str(exc))
                if attempt == policy.retries:
                    break
                time.sleep(delay)
                delay = min(delay * 2, policy.backoff_max_s)

        log.error("fetch.falhou", url=url, error=str(last_exc))
        return FetchResult(url, 0, None, None)

    def policy_for(self, host: str) -> DomainPolicy:
        return self.config.policy_for(host)

    def get_json(self, url: str, **kw: Any) -> Any:
        import json

        res = self.fetch(url, accept="application/json", **kw)
        if not res.ok:
            raise httpx.HTTPError(f"GET {url} -> HTTP {res.status}")
        return json.loads(res.body.decode("utf-8"))

    def get_text(self, url: str, **kw: Any) -> str:
        res = self.fetch(url, **kw)
        if not res.ok:
            raise httpx.HTTPError(f"GET {url} -> HTTP {res.status}")
        return res.body.decode("utf-8", errors="replace")


def sniff_kind(body: bytes, content_type: str | None) -> str:
    """Identifica o formato real do corpo.

    Formatos binarios sao decididos pelos magic bytes, nunca pelo Content-Type:
    o caso que importa e o servidor anunciar `application/pdf` e devolver uma
    pagina de erro HTML, e isso so os bytes revelam.

    Para formatos textuais nao ha magic bytes, entao o criterio e estrutural.
    Duas armadilhas reais, ambas observadas no NTRS:
      - texto puro que comeca com `[` (ex.: "[Paper Number]") nao e JSON;
      - o `fulltext` as vezes vem como XHTML do Apache Tika, que e HTML de
        verdade mas tambem e texto extraido legitimo (quem decide o que fazer
        com isso e o chamador, via `expect_document`).
    """
    head = body[:8]
    for magic, kind in MAGIC.items():
        if head.startswith(magic):
            return kind

    ct = (content_type or "").lower()
    sample = body[:4096].lstrip()
    low = sample[:512].lower()

    if low.startswith(b"<!doctype html") or low.startswith(b"<html") or b"<html" in low:
        return "html"
    if sample.startswith(b"<?xml") or (sample.startswith(b"<") and b">" in sample[:256]):
        return "xml"

    # JSON so quando de fato faz parse — `[Paper Number]` comeca com `[` e nao e.
    if sample[:1] in (b"{", b"["):
        import json as _json

        try:
            _json.loads(body.decode("utf-8"))
            return "json"
        except (ValueError, UnicodeDecodeError):
            pass

    if "pdf" in ct:
        # Content-Type diz PDF mas os bytes nao confirmam: nao e PDF.
        return "unknown"
    if ct.startswith("text/"):
        return "text"
    try:
        body[:4096].decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "unknown"


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None
