"""Tabela de opcoes do Economista (DEC-02): numeros deterministicos, o LLM so escolhe o id.

O LLM nunca inventa quantidade, custo ou caixa: ele devolve o `id` de uma linha desta tabela
e a justificativa. O gateway recalcula tudo a partir da linha escolhida.
"""
from __future__ import annotations

import math
from typing import Any

from .. import politicas
from ..politicas import Tempestade

PASSO_T = 5   # o servidor so aceita multiplos de 5 t


def tabela_economista(navio: dict[str, Any], tick: int, tempestade: Tempestade | None,
                      caixa_livre_cent: int, preco_cent_t: int | None = None,
                      compras_feitas: int = 0) -> list[dict[str, Any]]:
    """Gera as opcoes de abastecimento para um navio parado na origem.

    A opcao `nada` sempre existe. Cada opcao de compra traz custo, folga projetada e se cobre a
    necessidade da viagem com a contingencia de partida. Retorna so `nada` fora da janela de compra.

    Args:
        navio: Estado plano do navio.
        tick: Tick atual.
        tempestade: Tempestade conhecida ou `None`.
        caixa_livre_cent: Caixa livre em centavos.
        preco_cent_t: Preco por tonelada (centavos); usa o teto historico se ausente.
        compras_feitas: Compras ja confirmadas para este navio.

    Returns:
        Lista de opcoes `{id, quantidade_t, custo_cent, ...}`, da menor para a maior quantidade.
    """
    preco = preco_cent_t or politicas.PRECO_TETO_CENT_T
    v = politicas.velocidade_por_prazo(navio, tick, tempestade)
    necessario = politicas.projetar(navio, v, tick, tempestade)[1]
    util = politicas.combustivel_util(navio)
    alvo = necessario * (1 + politicas.CONTINGENCIA_PARTIDA)
    opcoes: list[dict[str, Any]] = [{
        "id": "nada", "quantidade_t": 0, "custo_cent": 0,
        "folga_pos_compra_t": round(util - alvo, 2), "cobre_necessidade": util >= alvo}]
    pode = (navio["ship_state"] == "AT_ORIGIN" and navio["progress_nautical_mile_milli"] == 0
            and tick <= politicas.ULTIMO_TICK_COMPRA and compras_feitas < 2)
    if not pode:
        return opcoes
    espaco = (navio["fuel_capacity_milli"] - navio["fuel_milli"] - navio["pending_fuel_milli"]) // 1000
    maximo_caixa = caixa_livre_cent // preco
    teto = min(espaco, maximo_caixa) // PASSO_T * PASSO_T
    minimo = max(PASSO_T, math.floor(max(alvo - util, 0) / PASSO_T) * PASSO_T)   # a partir da cobertura
    for q in range(minimo, teto + 1, PASSO_T):
        if q < PASSO_T:
            continue
        opcoes.append({"id": f"buy-{q}", "quantidade_t": q, "custo_cent": q * preco,
                       "folga_pos_compra_t": round(util + q - alvo, 2),
                       "cobre_necessidade": util + q >= alvo})
    return opcoes


def opcao_por_id(opcoes: list[dict[str, Any]], opcao_id: str) -> dict[str, Any] | None:
    """Localiza uma linha da tabela pelo id; `None` se o LLM inventou um id."""
    return next((o for o in opcoes if o["id"] == opcao_id), None)
