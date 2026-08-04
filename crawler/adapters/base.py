"""Interface unica de descoberta.

A separacao entre DESCOBRIR e BAIXAR e o que permite ao mesmo nucleo servir uma
API REST (NTRS), um OAI-PMH (ROSA P), um dump CSV (CORDIS) e um HTML (PSAS).
Um adaptador so precisa saber traduzir o formato nativo da sua fonte em
`DocumentRecord`; rate limit, robots, dedupe e armazenamento nao sao problema
dele.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterator

from ..core.fetcher import Fetcher
from ..core.record import DocumentRecord


class BaseAdapter(ABC):
    #: identificador curto usado em `DocumentRecord.source` e nos relatorios
    name: str = "base"

    #: Quando True, a fonte existe para fornecer negativos dificeis e o
    #: amostrador da classe negativa e desligado: coleta-se todo o acervo.
    collect_all_negatives: bool = False

    def __init__(self, fetcher: Fetcher, **options: Any):
        self.fetcher = fetcher
        self.options = options

    @abstractmethod
    def discover(self) -> Iterator[DocumentRecord]:
        """Emite candidatos. NAO baixa arquivos e NAO decide o que guardar."""
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
