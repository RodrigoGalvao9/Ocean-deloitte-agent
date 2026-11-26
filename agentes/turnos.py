"""Cliente do modo por turnos (`/api/v1/turn/*`). Somente REST: sem SSE, WebSocket nem webhook.

Fluxo: `pack()` -> deliberacao -> `submit()`. O servidor abre/fecha a janela, envia os heartbeats e
avanca ate a proxima parada, entao `submit()` devolve o proximo pacote em `next_pack`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .cliente import STATUS_TRANSITORIOS, ClienteOcean, ErroOcean

log = logging.getLogger("ocean")

PAPEIS = ("navigation", "safety", "economy", "coordinator")
AVANCO_PADRAO = 25  # ticks por turno, o padrao do servidor


class ErroTurno(ErroOcean):
    """Submissao recusada pelo servidor. `erros` traz o `validation_errors` quando existir."""

    def __init__(self, mensagem: str, codigo: str | None = None, erros: list[Any] | None = None) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.erros = erros or []


@dataclass
class ResultadoTurno:
    relatorio: dict[str, Any]   # execution_report
    proximo: dict[str, Any]     # next_pack
    recuperado: bool = False    # True se a resposta se perdeu e o avanco foi confirmado pelo pacote


def _marca(pacote: dict[str, Any]) -> tuple[int, int]:
    meta = pacote["meta"]
    return meta["turn_number"], meta["current_tick"]


class ClienteTurnos:
    def __init__(self, cliente: ClienteOcean) -> None:
        self.c = cliente

    # Preparacao
    def configurar_agentes(self, *, reiniciar: bool, modo: str | None = None,
                           equipe: str | None = None) -> dict[str, Any]:
        """Registra os 4 especialistas (navigation, safety, economy, coordinator)."""
        corpo: dict[str, Any] = {"reset": reiniciar}
        if modo:
            corpo["scenario_mode"] = modo
        if equipe:
            corpo["team_id"] = equipe
        dados = self.c._exigir_ok("POST", "/api/v1/turn/setup-agents", corpo, self.c.cabecalhos_admin)
        papeis = {a["role"] for a in dados.get("agents", [])}
        if not set(PAPEIS) <= papeis:
            raise ErroOcean(f"setup-agents nao registrou todos os papeis: {sorted(papeis)}")
        return dados

    # Leitura
    def pacote(self, *, abrir: bool = True, modo: str | None = None) -> dict[str, Any]:
        caminho = f"/api/v1/turn/pack?auto_open={'true' if abrir else 'false'}"
        if modo:
            caminho += f"&scenario_mode={modo}"
        return self.c._exigir_ok("GET", caminho, None, self.c.cabecalhos_admin)

    # Envio
    def enviar(self, pacote_atual: dict[str, Any], decisoes: list[dict[str, Any]], *,
               avancar: int = AVANCO_PADRAO, parar_em_eventos: bool = True) -> ResultadoTurno:
        """Envia as decisoes do turno UMA vez.

        Um submit avanca varios ticks, entao nao e reenviado as cegas: se a resposta se perder,
        o pacote atual diz se o servidor chegou a processar (turno/tick mudou).
        """
        corpo = {"advance_ticks": avancar, "stop_on_events": parar_em_eventos, "decisions": decisoes}
        antes = _marca(pacote_atual)
        try:
            resp = self.c.http.post("/api/v1/turn/submit", json=corpo, headers=self.c.cabecalhos_admin)
        except Exception as erro:  # timeout/queda: o servidor pode ter processado
            return self._confirmar(antes, f"transporte: {erro}")
        try:
            dados = resp.json()
        except ValueError:
            dados = {"detail": resp.text[:300]}
        if resp.status_code == 200:
            return ResultadoTurno(dados["execution_report"], dados["next_pack"])
        if resp.status_code in STATUS_TRANSITORIOS:
            return self._confirmar(antes, f"HTTP {resp.status_code}")
        raise ErroTurno(f"submit -> HTTP {resp.status_code}: {dados.get('detail', dados)}",
                        dados.get("code"), dados.get("validation_errors"))

    def _confirmar(self, antes: tuple[int, int], motivo: str) -> ResultadoTurno:
        log.warning("submit sem resposta (%s); conferindo o pacote", motivo)
        atual = self.pacote(abrir=False)
        if _marca(atual) == antes:
            raise ErroOcean(f"submit nao foi aplicado ({motivo}); pode reenviar")
        origem, destino = antes[1], atual["meta"]["current_tick"]
        relatorio = {"from_tick": origem, "to_tick": destino, "ticks_elapsed": destino - origem,
                     "decisions_committed": None, "decisions_vetoed": None,
                     "stop_reasons": atual["meta"].get("stop_reasons") or []}
        return ResultadoTurno(relatorio, atual, recuperado=True)

    def finalizar(self) -> dict[str, Any]:
        return self.c._exigir_ok("POST", "/api/v1/round/finalize", {}, self.c.cabecalhos_admin)
