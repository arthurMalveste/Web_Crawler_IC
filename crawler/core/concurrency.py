"""Lock e semaforo por CHAVE — criados sob demanda, thread-safe.

O harvest paralelo precisa serializar coisas que compartilham uma chave (o
mesmo dominio, o mesmo SHA-256) sem serializar o que nao compartilha. Um lock
global seria simples mas devolveria a serializacao inteira — exatamente o que
o paralelismo por dominio existe para eliminar. Um lock por chave, criado na
primeira vez que a chave aparece, da o isolamento certo: dominios diferentes
(ou conteudos diferentes) nunca esperam um pelo outro.

    locks = KeyedLock()
    with locks.get("ntrs.nasa.gov"):
        ...  # so outra thread pedindo o MESMO host espera aqui

A criacao do lock/semaforo em si (o `dict[key] = ...`) e o unico ponto que
precisa de protecao propria — daqui o `_guard`, sempre segurado por pouquissimo
tempo (um `dict.get`/`dict.setdefault`).
"""

from __future__ import annotations

import threading

class KeyedLock:
    """Um `threading.Lock` por chave, criado na primeira vez que ela aparece."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock


class KeyedSemaphore:
    """Um `threading.Semaphore` por chave, com o valor fixado na primeira vez.

    Chamadas seguintes com `value` diferente NAO redimensionam o semaforo
    existente — nao ha caso de uso aqui para mudar o teto de concorrencia de um
    dominio no meio de uma execucao, e um `Semaphore` do stdlib nao suporta
    redimensionamento de qualquer forma.
    """

    def __init__(self) -> None:
        self._sems: dict[str, threading.Semaphore] = {}
        self._guard = threading.Lock()

    def get(self, key: str, value: int) -> threading.Semaphore:
        with self._guard:
            sem = self._sems.get(key)
            if sem is None:
                sem = self._sems[key] = threading.Semaphore(max(1, value))
            return sem
