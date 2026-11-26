"""Regras de decisao dos agentes. Funcoes puras: nao fazem HTTP.

Nenhum tick de evento e fixado aqui: tudo vem de /api/v1/state e /api/v1/events.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

TOTAL_TICKS = 432
TICKS_DESCARGA = 12

VELOCIDADE_MIN = 8.0
VELOCIDADE_MAX = 18.0
FOLGA_PRAZO_TICKS = 30        # reserva de tempo para avarias, rampa e imprevistos
INTERVALO_MIN_TICKS = 6       # evita oscilacao (churn) de velocidade
FOLGA_COMBUSTIVEL_T = 1.0     # margem acima da reserva durante a viagem
CONTINGENCIA_PARTIDA = 0.08   # folga de combustivel exigida antes de zarpar
PRECO_TETO_CENT_T = 78_000    # maior preco visto, usado para estimar o custo da compra
ULTIMO_TICK_COMPRA = 11       # depois disso ficar na origem gera multa

# Calibracao de consumo (t/h): (hotelaria, coeficiente propulsivo a 14 kn ja com o fator de carga)
CONSUMO = {
    "S1-FAROL": (0.10, 0.8100),
    "S2-MARE": (0.12, 1.0139),
    "S3-CORAL": (0.14, 1.2177),
    "S4-ATLAS": (0.16, 1.4175),
}

PARADOS = ("AT_ORIGIN", "HOLDING", "AT_BUNKER_STATION", "WAITING_BERTH")
NAVEGAVEIS = ("AT_ORIGIN", "UNDERWAY", "HOLDING", "AT_BUNKER_STATION")


@dataclass(frozen=True)
class Tempestade:
    de: int
    ate: int
    velocidade_segura: float
    multiplicador: float

    def ativa(self, tick: int) -> bool:
        return self.de <= tick < self.ate


@dataclass(frozen=True)
class Proposta:
    papel: str                 # agente que propoe
    logico: str
    acao: dict[str, Any]
    motivo: str
    chave: str                 # materiality.key
    tipo: str                  # materiality.kind


@dataclass
class Memoria:
    ultima_velocidade: dict[str, int]   # logico -> tick do ultimo SET_SPEED
    compras: dict[str, int]             # logico -> tentativas de abastecimento

    @classmethod
    def vazia(cls) -> "Memoria":
        return cls({}, {})


def ler_tempestade(eventos: list[dict[str, Any]]) -> Tempestade | None:
    for ev in eventos:
        p = ev["payload"]
        if p["event_code"] == "E01_STORM":
            params = p["parameters"]
            return Tempestade(p["effective_from_tick"], p["effective_until_tick"],
                              float(params["safe_speed_kn"]), float(params["consumption_multiplier"]))
    return None


def queima_por_tick(logico: str, velocidade: float, multiplicador: float = 1.0) -> float:
    hotel, coef = CONSUMO[logico]
    return (hotel + coef * (velocidade / 14) ** 3 * multiplicador) / 6


def projetar(navio: dict, velocidade: float, tick: int, tempestade: Tempestade | None) -> tuple[int, float]:
    """Retorna (tick de chegada, combustivel em t ate o fim da descarga) navegando a `velocidade`."""
    logico = navio["logical_id"]
    falta_nm = navio["route_total_nm"] - navio["progress_nautical_mile_milli"] / 1000
    gasto = CONSUMO[logico][0] / 6 * TICKS_DESCARGA
    while falta_nm > 0 and tick < TOTAL_TICKS:
        v, mult = velocidade, 1.0
        if tempestade and tempestade.ativa(tick):
            v, mult = min(v, tempestade.velocidade_segura), tempestade.multiplicador
        gasto += queima_por_tick(logico, v, mult)
        falta_nm -= v / 6
        tick += 1
    return (tick if falta_nm <= 0 else TOTAL_TICKS + 1), gasto


def combustivel_util(navio: dict) -> float:
    return (navio["fuel_milli"] + navio["pending_fuel_milli"] - navio["reserve_min_milli"]) / 1000


def velocidade_por_prazo(navio: dict, tick: int, tempestade: Tempestade | None) -> float:
    """Menor velocidade (passo 0,5) que chega no prazo com folga."""
    limite = navio["deadline_tick"] - TICKS_DESCARGA - FOLGA_PRAZO_TICKS
    v = VELOCIDADE_MIN
    while v < VELOCIDADE_MAX and projetar(navio, v, tick, tempestade)[0] > limite:
        v += 0.5
    return v


def velocidade_alvo(navio: dict, tick: int, tempestade: Tempestade | None) -> float:
    v = velocidade_por_prazo(navio, tick, tempestade)
    # Combustivel tem prioridade sobre prazo: atrasar custa pouco, violar a reserva zera a seguranca.
    while v > VELOCIDADE_MIN and projetar(navio, v, tick, tempestade)[1] > combustivel_util(navio) - FOLGA_COMBUSTIVEL_T:
        v -= 0.5
    if tempestade and tempestade.ativa(tick):
        v = min(v, tempestade.velocidade_segura)
    return min(v, float(navio["active_limit_speed_kn"]))


def _velocidade(v: float) -> dict[str, Any]:
    return {"name": "SET_SPEED", "arguments": {"speed_kn": f"{v:.2f}"}}


def agente_seguranca(navio: dict, tick: int, tempestade: Tempestade | None) -> Proposta | None:
    logico, estado = navio["logical_id"], navio["ship_state"]
    if navio["e04_active"]:
        if estado == "UNDERWAY" and float(navio["target_speed_kn"]) > 0:
            return Proposta("safety", logico, {"name": "HOLD", "arguments": {"reason_code": "ENGINE_FAULT_CONTAINMENT"}},
                            "ENGINE_FAULT_ACTIVE", "engine-fault-hold", "event")
        if estado in PARADOS and float(navio["effective_speed_kn"]) == 0:
            return Proposta("safety", logico, {"name": "REPAIR", "arguments": {"fault_id": "E04_ENGINE_FAULT"}},
                            "ENGINE_FAULT_ACTIVE", "engine-fault-repair", "service")
        return None
    if (tempestade and tempestade.ativa(tick) and estado == "UNDERWAY"
            and float(navio["target_speed_kn"]) > tempestade.velocidade_segura):
        return Proposta("safety", logico, _velocidade(tempestade.velocidade_segura),
                        "STORM_SAFE_SPEED", "storm-cap", "event")
    return None


def agente_economia(navio: dict, tick: int, tempestade: Tempestade | None, caixa_livre_cent: int,
                    memoria: Memoria) -> Proposta | None:
    logico = navio["logical_id"]
    if navio["ship_state"] != "AT_ORIGIN" or navio["progress_nautical_mile_milli"] > 0:
        return None
    if tick > ULTIMO_TICK_COMPRA or memoria.compras.get(logico, 0) >= 2:
        return None
    necessario = projetar(navio, velocidade_por_prazo(navio, tick, tempestade), tick, tempestade)[1]
    falta = necessario * (1 + CONTINGENCIA_PARTIDA) - combustivel_util(navio)
    if falta <= 0:
        return None
    espaco = (navio["fuel_capacity_milli"] - navio["fuel_milli"] - navio["pending_fuel_milli"]) // 1000
    quantidade = min(math.ceil(falta / 5) * 5, espaco // 5 * 5)
    if quantidade < 5 or quantidade * PRECO_TETO_CENT_T > caixa_livre_cent:
        return None
    return Proposta("economy", logico, {"name": "BUY_FUEL", "arguments": {"quantity_t": quantidade}},
                    "PROJECTED_FUEL_SHORTFALL", "pre-departure-bunker", "service")


def agente_navegacao(navio: dict, tick: int, tempestade: Tempestade | None, memoria: Memoria) -> Proposta | None:
    logico, estado = navio["logical_id"], navio["ship_state"]
    if estado not in NAVEGAVEIS or navio["e04_active"]:
        return None
    desejada = velocidade_alvo(navio, tick, tempestade)
    atual = float(navio["target_speed_kn"])
    if abs(desejada - atual) < 0.5:
        return None
    if estado == "UNDERWAY" and tick - memoria.ultima_velocidade.get(logico, -99) < INTERVALO_MIN_TICKS:
        return None
    if navio["progress_nautical_mile_milli"] == 0 and atual == 0:
        motivo, chave = "DEPARTURE_PLAN", "departure"
    elif atual == 0:
        motivo, chave = "RESUME_VOYAGE", "resume-voyage"
    else:
        motivo, chave = "ETA_FUEL_REPLAN", "eta-replan"
    return Proposta("navigation", logico, _velocidade(desejada), motivo, chave, "plan_change")


def propor(navio: dict, tick: int, tempestade: Tempestade | None, caixa_livre_cent: int,
           memoria: Memoria) -> Proposta | None:
    """Uma proposta por navio por tick, em ordem de prioridade: seguranca, economia, navegacao."""
    if navio["state_age_ticks"] > 2:  # o servidor so aceitaria contencao; esperar o sinal voltar
        return None
    seguranca = agente_seguranca(navio, tick, tempestade)
    if seguranca or navio["state_age_ticks"] > 0:  # economia e navegacao exigem telemetria fresca
        return seguranca
    return (agente_economia(navio, tick, tempestade, caixa_livre_cent, memoria)
            or agente_navegacao(navio, tick, tempestade, memoria))


def revisar(p: Proposta, navio: dict, tick: int, tempestade: Tempestade | None,
            caixa_livre_cent: int) -> list[str]:
    """Checagem independente do revisor. Lista vazia = aprovado; senao, codigos do veto."""
    vetos: list[str] = []
    nome, args, estado = p.acao["name"], p.acao["arguments"], navio["ship_state"]
    parado = float(navio["effective_speed_kn"]) == 0
    if navio["state_age_ticks"] > 2 and nome not in ("HOLD", "SAFE_STOP"):
        vetos.append("STALE_TELEMETRY")
    if nome == "SET_SPEED":
        v = float(args["speed_kn"])
        if estado not in NAVEGAVEIS:
            vetos.append("STATE_NOT_ALLOWED")
        if navio["e04_active"]:
            vetos.append("ENGINE_FAULT_ACTIVE")
        if not 0 <= v <= float(navio["active_limit_speed_kn"]) or v * 2 != int(v * 2):
            vetos.append("SPEED_OUT_OF_LIMIT")
        if tempestade and tempestade.ativa(tick) and v > tempestade.velocidade_segura:
            vetos.append("UNSAFE_STORM_SPEED")
        # Acelerar so e aceito se a projecao ainda terminar acima da reserva.
        if v > max(VELOCIDADE_MIN, float(navio["target_speed_kn"])) and \
                projetar(navio, v, tick, tempestade)[1] > combustivel_util(navio):
            vetos.append("FUEL_RESERVE_AT_RISK")
    elif nome == "HOLD":
        if estado not in ("UNDERWAY", "HOLDING"):
            vetos.append("STATE_NOT_ALLOWED")
    elif nome == "REPAIR":
        if not navio["e04_active"] or estado not in PARADOS or not parado:
            vetos.append("REPAIR_PRECONDITION")
    elif nome == "BUY_FUEL":
        q = args["quantity_t"]
        if estado not in ("AT_ORIGIN", "AT_BUNKER_STATION") or not parado:
            vetos.append("STATE_NOT_ALLOWED")
        if navio["fuel_milli"] + navio["pending_fuel_milli"] + q * 1000 > navio["fuel_capacity_milli"]:
            vetos.append("TANK_CAPACITY")
        if q * PRECO_TETO_CENT_T > caixa_livre_cent:
            vetos.append("OPERATIONAL_RESERVE")
    else:
        vetos.append("ACTION_NOT_SUPPORTED")
    return vetos
