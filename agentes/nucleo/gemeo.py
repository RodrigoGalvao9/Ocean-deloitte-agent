"""Gemeo digital do navio (DEC-08): simula TODO o horizonte do turno, nao so o proximo tick.

Funcao pura, sem HTTP e sem LLM. Reaproveita o modelo de consumo de `politicas.py`.
No modo por turnos o servidor avanca ate 25 ticks por submit; validar so o primeiro tick
deixaria passar uma violacao da reserva no meio do turno.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import politicas
from ..politicas import Tempestade


@dataclass(frozen=True)
class ResultadoSimulacao:
    """Resultado da simulacao de um horizonte."""

    violou_reserva: bool
    tick_violacao: int | None
    combustivel_util_final_t: float   # acima da reserva; negativo = abaixo dela
    menor_folga_t: float              # pior folga em qualquer tick do horizonte
    progresso_final_nm: float
    chegou: bool
    tick_chegada: int | None


def simular_horizonte(navio: dict[str, Any], velocidade: float, tick: int, avanco: int,
                      tempestade: Tempestade | None, compra_t: float = 0.0) -> ResultadoSimulacao:
    """Simula `avanco` ticks a partir de `tick` mantendo `velocidade` (nos).

    Args:
        navio: Estado plano (`decisao.adaptar_navio`).
        velocidade: Velocidade alvo mantida durante o horizonte.
        tick: Tick inicial.
        avanco: Quantidade de ticks que o submit vai avancar.
        tempestade: Tempestade ativa/futura ou `None`.
        compra_t: Combustivel adicional ja comprado (toneladas) a considerar no tanque.

    Returns:
        Resultado com a pior folga de reserva em qualquer tick do horizonte.
    """
    logico = navio["logical_id"]
    hotel = politicas.CONSUMO[logico][0] / 6
    util = politicas.combustivel_util(navio) + compra_t
    falta = navio["route_total_nm"] - navio["progress_nautical_mile_milli"] / 1000
    total = navio["route_total_nm"]
    menor, violacao, chegada = util, None, None
    for t in range(tick, min(tick + avanco, politicas.TOTAL_TICKS)):
        if falta > 0 and velocidade > 0:
            v, mult = velocidade, 1.0
            if tempestade and tempestade.ativa(t):
                v, mult = min(v, tempestade.velocidade_segura), tempestade.multiplicador
            util -= politicas.queima_por_tick(logico, v, mult)
            falta -= v / 6
            if falta <= 0 and chegada is None:
                chegada = t + 1
        else:
            util -= hotel
        menor = min(menor, util)
        if util < 0 and violacao is None:
            violacao = t
    return ResultadoSimulacao(violacao is not None, violacao, round(util, 3), round(menor, 3),
                              round(total - max(falta, 0), 3), falta <= 0, chegada)
