"""Renderizacao JavaScript via Playwright — opcional e por fonte.

Existe para as fontes cujo HTML servido nao contem os links. O caso concreto e a
documentacao do ESA EOF, que entrega "Loading Document..." e busca a lista por
JavaScript: sem renderizar, o crawler ve uma pagina vazia.

E deliberadamente opt-in. Renderizar custa ordens de grandeza mais que um GET —
sobe um navegador, executa scripts, espera a rede aquietar — e a esmagadora
maioria das fontes da lista serve HTML completo. Ligar por padrao seria trocar
minutos por horas sem ganho.

Playwright em vez de Selenium: mais rapido, API mais estavel e espera automatica
por estado de rede (`networkidle`), que e justamente o que estas paginas exigem.
A dependencia e opcional — se nao estiver instalada, o renderer se desativa e
avisa, em vez de quebrar a coleta das outras fontes.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)


class PlaywrightRenderer:
    """Envolve um navegador headless. Use como context manager."""

    def __init__(
        self,
        *,
        user_agent: str,
        wait_ms: int = 2500,
        wait_selector: str | None = None,
        timeout_ms: int = 45_000,
    ):
        self.user_agent = user_agent
        self.wait_ms = wait_ms
        self.wait_selector = wait_selector
        self.timeout_ms = timeout_ms
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self.disponivel = False

    def __enter__(self) -> "PlaywrightRenderer":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning(
                "renderer.indisponivel",
                motivo="playwright nao instalado",
                acao="pip install playwright && playwright install chromium",
            )
            return self

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1400, "height": 1000},
        )
        # Imagens e fontes nao interessam e dominam o tempo de carga.
        self._context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        self.disponivel = True
        log.info("renderer.pronto")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:  # o navegador pode ja ter morrido
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = None
        self.disponivel = False

    def render(self, url: str) -> str | None:
        """Devolve o HTML apos execucao dos scripts, ou None se nao der."""
        if not self.disponivel:
            return None
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if self.wait_selector:
                page.wait_for_selector(self.wait_selector, timeout=self.timeout_ms)
            else:
                try:
                    page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
                except Exception:
                    # networkidle nunca chega em paginas com polling; o
                    # conteudo ja costuma estar la.
                    pass
            page.wait_for_timeout(self.wait_ms)
            return page.content()
        except Exception as exc:
            log.warning("renderer.falhou", url=url, error=str(exc))
            return None
        finally:
            page.close()


def make_renderer(spec, user_agent: str) -> PlaywrightRenderer | None:
    """Cria o renderer apenas se a fonte pedir."""
    if not getattr(spec, "render", False):
        return None
    return PlaywrightRenderer(
        user_agent=user_agent,
        wait_ms=spec.render_wait_ms,
        wait_selector=spec.render_wait_selector,
    )
