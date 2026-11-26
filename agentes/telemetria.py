"""Langfuse opcional. Sem as chaves, sem o pacote ou com credencial recusada, tudo vira no-op."""
from __future__ import annotations

import logging
import os
from contextlib import nullcontext
from typing import Any

log = logging.getLogger("ocean")

_lf: Any = None
_contexto: Any = None
_sessao: str | None = None


class _Nada:
    def update(self, **_: Any) -> None:
        pass


def iniciar(sessao: str, tags: list[str]) -> None:
    global _lf, _contexto, _sessao
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        log.info("Langfuse desligado (LANGFUSE_PUBLIC_KEY e LANGFUSE_SECRET_KEY ausentes)")
        return
    try:
        from langfuse import Langfuse, propagate_attributes
        cliente = Langfuse()                 # le LANGFUSE_BASE_URL (ou LANGFUSE_HOST)
        cliente.auth_check()
        _contexto = propagate_attributes(session_id=sessao, tags=tags)
        _contexto.__enter__()
        _lf, _sessao = cliente, sessao
        log.info("Langfuse ativo | sessao %s", sessao)
    except Exception as erro:                # telemetria nunca derruba a rodada
        status = getattr(erro, "status_code", None)
        motivo = f"HTTP {status}: confira chaves e LANGFUSE_BASE_URL/HOST" if status else str(erro)[:200]
        log.warning("Langfuse indisponivel (%s); seguindo sem rastreio", motivo or type(erro).__name__)


def observar(nome: str, tipo: str = "span", **campos: Any) -> Any:
    """Context manager de um span ou generation; devolve um objeto com `update`."""
    if _lf is None:
        return nullcontext(_Nada())
    return _lf.start_as_current_observation(name=nome, as_type=tipo, **campos)


def evento(nome: str, nivel: str = "DEFAULT", **dados: Any) -> None:
    if _lf is not None:
        _lf.create_event(name=nome, level=nivel, metadata=dados)


def pontuar(nome: str, valor: float) -> None:
    if _lf is not None:
        _lf.create_score(name=nome, value=valor, session_id=_sessao, data_type="NUMERIC")


def fechar() -> None:
    """Envia o que estiver na fila antes de o processo terminar."""
    global _lf
    if _lf is None:
        return
    try:
        _contexto.__exit__(None, None, None)
        _lf.shutdown()
    except Exception as erro:
        log.warning("Langfuse: falha ao encerrar (%s)", erro)
    _lf = None
