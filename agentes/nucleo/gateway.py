"""Gateway (DEC-07): biblioteca e unico ponto de escrita. Nenhum agente chama o servidor direto.

Toda proposta (de LLM ou de regra) passa por `Gateway.avaliar_lote` antes de virar `decisions[]`.
Funcao pura em relacao a rede: so decide, nao envia. Vetos sao codigos curtos e auditaveis.

Camadas (nesta ordem): identidade/allow-list (DEC-06) -> schema -> maker-checker -> precondicao fisica
e telemetria (`politicas.revisar`) -> janela de compra -> reserva no horizonte inteiro (DEC-08)
-> caixa -> anti-churn -> idempotencia e cota.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import politicas
from ..cliente import hash_acao
from ..decisao import REVISOR
from ..politicas import Memoria, Proposta, Tempestade
from .gemeo import simular_horizonte

# DEC-06: o servidor faz RBAC por escopo/navio, nao por acao; a restricao por acao e nossa.
# DEC-04: a Sentinela vive no Navegador, entao o Navegador responde a evento (HOLD/REPAIR/velocidade).
# DEC-10: SAFE_STOP nao e proposto por agente; so o gateway o libera no modo degradado.
ALLOW_LIST: dict[str, frozenset[str]] = {
    "navigation": frozenset({"SET_SPEED", "HOLD", "REPAIR"}),
    "economy": frozenset({"BUY_FUEL", "CANCEL_FUEL"}),
    "safety": frozenset({"HOLD", "REPAIR"}),
    "coordinator": frozenset(),
}
MAX_ACOES_POR_NAVIO_NO_TURNO = 1
FOLGA_PRAZO_DESACELERAR = 15   # ticks; igual ao portao do LLM da novoModelo


@dataclass
class Contexto:
    """Fotografia do turno que o gateway precisa para decidir."""

    tick: int
    avanco: int
    tempestade: Tempestade | None
    caixa_livre_cent: int
    memoria: Memoria
    degradado: bool = False   # libera SAFE_STOP por regra explicita (DEC-10)


@dataclass(frozen=True)
class Veredito:
    proposta: Proposta
    vetos: tuple[str, ...]
    hash_acao: str
    chave_idempotencia: str

    @property
    def aprovada(self) -> bool:
        return not self.vetos


@dataclass
class Gateway:
    """Mantem so o que precisa sobreviver entre turnos: chaves de idempotencia ja usadas."""

    chaves_usadas: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------ camadas
    @staticmethod
    def _schema(acao: Any) -> list[str]:
        if not isinstance(acao, dict) or not isinstance(acao.get("name"), str) \
                or not isinstance(acao.get("arguments"), dict):
            return ["SCHEMA_INVALID"]
        nome, args = acao["name"], acao["arguments"]
        try:
            if nome == "SET_SPEED":
                float(args["speed_kn"])
            elif nome == "BUY_FUEL":
                q = args["quantity_t"]
                if not isinstance(q, int) or isinstance(q, bool) or q < 5 or q % 5:
                    return ["SCHEMA_INVALID"]
            elif nome == "HOLD":
                if not isinstance(args.get("reason_code"), str):
                    return ["SCHEMA_INVALID"]
            elif nome == "REPAIR":
                if not isinstance(args.get("fault_id"), str):
                    return ["SCHEMA_INVALID"]
        except (KeyError, TypeError, ValueError):
            return ["SCHEMA_INVALID"]
        return []

    @staticmethod
    def _papel_e_acao(p: Proposta, ctx: Contexto) -> list[str]:
        nome = p.acao.get("name") if isinstance(p.acao, dict) else None
        if p.papel not in ALLOW_LIST:
            return ["ROLE_UNKNOWN"]
        if nome == "SAFE_STOP":
            return [] if ctx.degradado else ["SAFE_STOP_NOT_AUTHORIZED"]
        if nome not in ALLOW_LIST[p.papel]:
            return ["ACTION_NOT_ALLOWED_FOR_ROLE"]
        return []

    @staticmethod
    def _reserva_no_horizonte(p: Proposta, navio: dict, ctx: Contexto) -> list[str]:
        """DEC-08: a velocidade pedida vale ate o fim do turno; nenhum tick pode furar a reserva."""
        vetos: list[str] = []
        v = float(p.acao["arguments"]["speed_kn"])
        if v > 0 and simular_horizonte(navio, v, ctx.tick, ctx.avanco, ctx.tempestade).violou_reserva:
            vetos.append("FUEL_RESERVE_AT_RISK_HORIZON")
        atual = float(navio["target_speed_kn"])
        if v < atual and p.papel == "navigation" and not navio["e04_active"]:
            chegada = politicas.projetar(navio, v, ctx.tick, ctx.tempestade)[0]
            if chegada > navio["deadline_tick"] - politicas.TICKS_DESCARGA - FOLGA_PRAZO_DESACELERAR \
                    and not (ctx.tempestade and ctx.tempestade.ativa(ctx.tick)):
                vetos.append("DEADLINE_AT_RISK")
        return vetos

    # ------------------------------------------------------------------ avaliacao
    def avaliar(self, p: Proposta, navio: dict, ctx: Contexto, caixa_cent: int) -> list[str]:
        """Vetos de uma proposta; lista vazia = pode virar decisao."""
        vetos = self._papel_e_acao(p, ctx)
        vetos += self._schema(p.acao)
        if vetos:
            return list(dict.fromkeys(vetos))
        if p.papel not in REVISOR:   # coordinator nao tem revisor definido: so SAFE_STOP degradado
            return [] if p.acao["name"] == "SAFE_STOP" else ["MAKER_CHECKER_UNDEFINED"]
        if REVISOR[p.papel] == p.papel:
            vetos.append("MAKER_CHECKER_SAME_AGENT")
        if p.acao["name"] != "SAFE_STOP":
            vetos += politicas.revisar(p, navio, ctx.tick, ctx.tempestade, caixa_cent)
        nome = p.acao["name"]
        if nome == "SET_SPEED":
            v, atual = float(p.acao["arguments"]["speed_kn"]), float(navio["target_speed_kn"])
            if abs(v - atual) < 0.5:
                vetos.append("NOOP_SPEED")
            if (navio["ship_state"] == "UNDERWAY" and v > 0 and
                    ctx.tick - ctx.memoria.ultima_velocidade.get(p.logico, -99) < politicas.INTERVALO_MIN_TICKS
                    and not (ctx.tempestade and ctx.tempestade.ativa(ctx.tick))):
                vetos.append("CHURN_INTERVAL")
            vetos += self._reserva_no_horizonte(p, navio, ctx)
        elif nome == "BUY_FUEL":
            if ctx.tick > politicas.ULTIMO_TICK_COMPRA:
                vetos.append("PURCHASE_WINDOW_CLOSED")
            if ctx.memoria.compras.get(p.logico, 0) >= 2:
                vetos.append("PURCHASE_LIMIT")
        return list(dict.fromkeys(vetos))

    def avaliar_lote(self, propostas: list[Proposta], navios: dict[str, dict],
                     ctx: Contexto) -> list[Veredito]:
        """Avalia o turno inteiro: cota por navio, caixa abatida a cada compra, idempotencia."""
        saida: list[Veredito] = []
        caixa = ctx.caixa_livre_cent
        por_navio: dict[str, int] = {}
        for p in propostas:
            navio = navios.get(p.logico)
            h = hash_acao(p.acao) if isinstance(p.acao, dict) else ""
            chave = f"{p.logico}:{ctx.tick}:{h[:16]}"
            if navio is None:
                saida.append(Veredito(p, ("SHIP_NOT_ASSIGNED",), h, chave))
                continue
            vetos = self.avaliar(p, navio, ctx, caixa)
            if por_navio.get(p.logico, 0) >= MAX_ACOES_POR_NAVIO_NO_TURNO:
                vetos.append("QUOTA_EXCEEDED")
            if chave in self.chaves_usadas:
                vetos.append("DUPLICATE_IDEMPOTENCY")
            vetos = list(dict.fromkeys(vetos))
            if not vetos:
                por_navio[p.logico] = por_navio.get(p.logico, 0) + 1
                self.chaves_usadas.add(chave)
                if p.acao["name"] == "BUY_FUEL":
                    caixa -= p.acao["arguments"]["quantity_t"] * politicas.PRECO_TETO_CENT_T
            saida.append(Veredito(p, tuple(vetos), h, chave))
        return saida
