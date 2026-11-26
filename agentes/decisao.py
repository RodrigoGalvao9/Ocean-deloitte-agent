"""Do pacote de turno as decisoes: reaproveita `politicas.py` (funcoes puras) sem HTTP.

O pacote tem os mesmos dados do `/state`, so que organizados em outro formato; aqui ficam
os adaptadores e a montagem das decisoes `proposal -> review` no formato do `/turn/submit`.
"""
from __future__ import annotations

from typing import Any

from . import politicas
from .politicas import Memoria, Proposta, Tempestade

# papel do proponente -> papel do revisor (sempre outro agente; o servidor exige propositor != revisor)
REVISOR = {"navigation": "safety", "safety": "navigation", "economy": "safety"}

ESTADOS_DE_PARTIDA = ("AT_ORIGIN", "HOLDING", "AT_BUNKER_STATION", "BUNKERING", "WAITING_BERTH")


def adaptar_navio(navio: dict[str, Any]) -> dict[str, Any]:
    """Pacote -> o dicionario plano que `politicas` espera (mesmas chaves de /api/v1/state)."""
    contrato = navio["route_and_contract"]
    return {
        **navio["operational_state"],
        "logical_id": navio["logical_id"],
        "ship_id": navio["ship_id"],
        "deadline_tick": contrato["deadline_tick"],
        "route_total_nm": contrato["total_nm_milli"] / 1000,
    }


def ler_tempestade(pacote: dict[str, Any]) -> Tempestade | None:
    """Eventos do pacote sao planos; `politicas.ler_tempestade` espera o envelope com `payload`."""
    return politicas.ler_tempestade([{"payload": ev} for ev in pacote.get("active_events", [])])


def tick_atual(pacote: dict[str, Any]) -> int:
    return pacote["meta"]["current_tick"]


def turno_fino(pacote: dict[str, Any]) -> bool:
    """True quando convem avancar poucos ticks: algum navio ainda parte, abastece ou esta avariado."""
    return any(n["operational_state"]["ship_state"] in ESTADOS_DE_PARTIDA
               or n["operational_state"]["e04_active"] for n in pacote["ships"].values())


def propostas_do_turno(pacote: dict[str, Any], memoria: Memoria) -> list[tuple[Proposta, dict[str, Any], list[str]]]:
    """Uma proposta por navio, ja com a revisao independente. Retorna (proposta, navio, vetos).

    O caixa e abatido a cada compra aprovada: varias compras no mesmo turno nao disputam o mesmo saldo.
    """
    tick, tempestade = tick_atual(pacote), ler_tempestade(pacote)
    caixa = pacote["economy"]["available_cent"]
    saida = []
    for navio_pacote in pacote["ships"].values():
        navio = adaptar_navio(navio_pacote)
        proposta = politicas.propor(navio, tick, tempestade, caixa, memoria)
        if proposta is None:
            continue
        vetos = politicas.revisar(proposta, navio, tick, tempestade, caixa)
        if not vetos and proposta.acao["name"] == "BUY_FUEL":
            caixa -= proposta.acao["arguments"]["quantity_t"] * politicas.PRECO_TETO_CENT_T
        saida.append((proposta, navio, vetos))
    return saida


def montar_decisao(proposta: Proposta, vetos: list[str], racional: str | None = None,
                   racional_revisao: str | None = None, prefixo: str | None = None) -> dict[str, Any]:
    """Formato de `decisions[]` do /turn/submit. O texto antes de ':' na justificativa e o codigo do motivo."""
    texto = racional or f"{proposta.acao['name']} {proposta.acao['arguments']}"
    return {
        "ship_id": proposta.logico,
        "proposed_by": proposta.papel,
        "proposal_rationale": f"{prefixo or proposta.motivo}: {texto}",
        "reviewed_by": REVISOR[proposta.papel],
        "review_result": "rejected" if vetos else "approved",
        "review_rationale": ", ".join(vetos) if vetos else (racional_revisao or "INDEPENDENT_CHECK_PASSED"),
        "action": proposta.acao,
    }


def decidir_turno(pacote: dict[str, Any], memoria: Memoria) -> tuple[list[dict[str, Any]], list[Proposta]]:
    """Decisoes prontas para o submit e as propostas na mesma ordem (para atualizar a memoria)."""
    itens = propostas_do_turno(pacote, memoria)
    return [montar_decisao(p, v) for p, _, v in itens], [p for p, _, _ in itens]


def atualizar_memoria(memoria: Memoria, propostas: list[Proposta], relatorio: dict[str, Any], tick: int) -> None:
    """Registra so o que o servidor confirmou (`status == committed`)."""
    for aplicada in relatorio.get("applied_decisions", []):
        if aplicada["status"] != "committed":
            continue
        proposta = propostas[aplicada["index"]]
        if proposta.acao["name"] == "BUY_FUEL":
            memoria.compras[proposta.logico] = memoria.compras.get(proposta.logico, 0) + 1
        elif proposta.acao["name"] == "SET_SPEED":
            memoria.ultima_velocidade[proposta.logico] = tick
