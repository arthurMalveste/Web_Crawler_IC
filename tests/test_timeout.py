"""Teto de tempo de download.

O timeout do httpx e por operacao de leitura: um servidor que entrega bytes em
conta-gotas nunca o dispara e trava o pipeline indefinidamente. Observado na
primeira coleta real — 15 minutos para 10 documentos, processo vivo e ocioso em
I/O de rede.
"""

import time
from pathlib import Path

import httpx
import pytest
import respx

from crawler.core.fetcher import BlockedByPolicy, Config, Fetcher

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def config():
    cfg = Config.load(ROOT / "config" / "domains.yaml")
    cfg.defaults.update({"respect_robots": False, "rate": 0.0, "retries": 1})
    for d in cfg.domains.values():
        d.update({"respect_robots": False, "rate": 0.0})
    return cfg


@respx.mock
def test_transferencia_lenta_e_abortada(config):
    config.defaults["max_download_s"] = 0.3
    config.domains["ntrs.nasa.gov"]["max_download_s"] = 0.3

    def conta_gotas():
        yield b"%PDF-1.7\n"
        for _ in range(50):
            time.sleep(0.02)  # servidor vivo, mas lentissimo
            yield b"x" * 64

    respx.get("https://ntrs.nasa.gov/lento.pdf").mock(
        return_value=httpx.Response(200, content=conta_gotas())
    )
    with Fetcher(config) as f:
        with pytest.raises(BlockedByPolicy, match="excedeu"):
            f.fetch("https://ntrs.nasa.gov/lento.pdf")


@respx.mock
def test_download_rapido_nao_e_afetado(config):
    with Fetcher(config) as f:
        respx.get("https://ntrs.nasa.gov/ok.pdf").mock(
            return_value=httpx.Response(200, content=b"%PDF-1.7\n" + b"z" * 1000)
        )
        assert f.fetch("https://ntrs.nasa.gov/ok.pdf").ok
