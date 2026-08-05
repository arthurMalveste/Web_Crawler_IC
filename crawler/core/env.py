"""Configuracao por ambiente: `.env` na raiz + expansao de `${VAR}` no YAML.

Ate 2026-08-05 o caminho do corpus estava escrito direto no `domains.yaml`, que
e versionado. Mover a pasta do projeto quebrava a coleta, e a correcao exigia
editar um arquivo rastreado pelo git — o caminho de UMA maquina virava commit e
voltava como conflito na proxima.

Agora o que depende da maquina mora no `.env` (fora do git) e o YAML apenas
referencia:

    data_root: "${CONOPS_DATA_ROOT:-../ConOpsCorpus}"

Precedencia, do mais forte ao mais fraco:

  1. variavel ja exportada no ambiente (CI, `set`/`$env:` na sessao);
  2. `.env` na raiz do repositorio;
  3. o `:-default` escrito no proprio YAML.

O ambiente vence o `.env` de proposito: e o que permite um CI, ou uma execucao
pontual apontada para outro corpus, sobrepor sem tocar em arquivo nenhum.

Sem dependencia externa (`python-dotenv`) de proposito: sao poucas linhas, e a
correcao vale mesmo em uma checkout onde `pip install` ainda nao rodou.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Raiz do repositorio — `crawler/core/env.py` -> sobe tres niveis.
#: Toda referencia relativa do projeto e resolvida a partir daqui, nunca do
#: diretorio em que o comando foi executado.
ROOT = Path(__file__).resolve().parent.parent.parent

DOTENV = ROOT / ".env"

#: `${VAR}` ou `${VAR:-default}`. O default pode ser vazio (`${VAR:-}`).
_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_carregado = False


class ConfigAusente(Exception):
    """Uma variavel referenciada no YAML nao existe e nao tem default."""


def carregar_dotenv(path: Path | None = None, *, forcar: bool = False) -> int:
    """Le o `.env` para `os.environ` e devolve quantas variaveis definiu.

    Nao sobrescreve o que ja esta no ambiente (ver precedencia no topo). E
    barato e idempotente: so le o arquivo na primeira chamada do processo.
    """
    global _carregado
    if _carregado and path is None and not forcar:
        return 0

    alvo = path or DOTENV
    if path is None:
        _carregado = True
    if not alvo.exists():
        return 0

    n = 0
    for linha in alvo.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        if linha.startswith("export "):  # tolera o formato de shell POSIX
            linha = linha[len("export ") :].lstrip()
        chave, sep, valor = linha.partition("=")
        if not sep:
            continue
        chave = chave.strip()
        valor = valor.strip()
        # Aspas sao opcionais e servem para preservar espacos nas pontas.
        # Comentario no fim da linha NAO e removido: um `#` pode ser parte
        # legitima de um caminho, e adivinhar qual e qual causa mais erro do
        # que resolve.
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        if chave and chave not in os.environ:
            os.environ[chave] = valor
            n += 1
    return n


def expandir(valor: str) -> str:
    """Substitui `${VAR}` / `${VAR:-default}` pelo valor do ambiente.

    Falha alto quando a variavel nao existe e nao ha default: um `${...}` que
    sobrevive vira nome de pasta, e o erro so aparece muito depois, na forma de
    um corpus vazio em um diretorio de nome absurdo.
    """

    def troca(m: re.Match[str]) -> str:
        nome, default = m.group(1), m.group(2)
        valor = os.environ.get(nome)
        if valor is not None and valor != "":
            return valor
        if default is not None:
            return default
        raise ConfigAusente(
            f"variavel {nome} nao definida e sem default. "
            f"Defina-a em {DOTENV} (veja .env.example) ou no ambiente."
        )

    return _REF.sub(troca, valor)


def expandir_arvore(obj):
    """Aplica `expandir` a todas as strings de uma estrutura vinda do YAML."""
    if isinstance(obj, str):
        return expandir(obj)
    if isinstance(obj, dict):
        return {k: expandir_arvore(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expandir_arvore(v) for v in obj]
    return obj


def resolver_caminho(valor: str | Path) -> Path:
    """Normaliza um caminho de configuracao para absoluto.

    Expande `~` e resolve o relativo contra a raiz do REPOSITORIO. Resolver
    contra o diretorio corrente faria a mesma configuracao apontar para lugares
    diferentes conforme de onde o comando fosse chamado.
    """
    p = Path(str(valor)).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()
