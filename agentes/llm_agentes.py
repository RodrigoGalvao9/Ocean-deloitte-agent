"""Agentes LLM (proponente e revisor) sobre a politica deterministica.

O LLM propoe e revisa, mas nunca tem a ultima palavra sobre seguranca:
  * resposta a E01 (tempestade) e E04 (avaria) continua deterministica (o LLM nao a sobrescreve);
  * toda proposta do LLM passa por `politicas.revisar` + portoes extras (prazo, combustivel, anti-churn);
  * proposta vetada, invalida ou falha do Groq => a decisao deterministica do mesmo navio e usada.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

from . import politicas
from .decisao import (REVISOR, ler_tempestade, montar_decisao, propostas_do_turno, tick_atual, adaptar_navio)
from .groq import ClienteGroq, ErroLLM, OrcamentoEsgotado
from .politicas import Memoria, Proposta, Tempestade

log = logging.getLogger("ocean")

ACOES_LLM = ("SET_SPEED", "HOLD", "REPAIR", "BUY_FUEL")
FOLGA_PRAZO_LLM = 15   # ticks de folga exigidos de uma desaceleracao proposta pelo LLM
MODOS = ("completo", "proposta")

SISTEMA_PROPOSITOR = """[PAPEL:PROPOSITOR] Voce e a equipe de propositores (navigation, economy, safety) de uma frota de 4 navios graneleiros na Operacao Oceanica. Objetivo: entregar a carga no prazo, sem violar a reserva de combustivel, ao menor custo; seguranca em primeiro lugar.
Regras:
- No maximo 1 acao por navio e so com motivo material. Acao redundante (mesma velocidade) ou nova troca de velocidade em menos de 6 ticks da anterior e rejeitada como churn.
- Acoes: SET_SPEED {speed_kn: multiplo de 0.5, 0 a vel_limite}; HOLD {reason_code}; REPAIR {fault_id}; BUY_FUEL {quantity_t: multiplo de 5, so AT_ORIGIN ate o tick 11}.
- papel: economy para BUY_FUEL, safety para HOLD/REPAIR/tempestade, navigation para velocidade.
- "sugestao_deterministica" e uma recomendacao calculada e verificada: so se afaste dela com motivo claro; se concorda, repita-a.
Responda SOMENTE com JSON: {"propostas":[{"navio":"S1-FAROL","papel":"navigation","acao":{"name":"SET_SPEED","arguments":{"speed_kn":"12.50"}},"justificativa":"ate 25 palavras"}]}. Use {"propostas":[]} se nada deve ser feito."""

SISTEMA_REVISOR = """[PAPEL:REVISOR] Voce e o revisor independente da frota (safety revisa navigation e economy; navigation revisa safety). Confira cada proposta contra o estado do navio: reserva de combustivel, prazo, tempestade, estado operacional e limites. Aprove apenas o que for seguro e materialmente util; vete o que arriscar reserva, prazo ou seguranca, ou for redundante.
Responda SOMENTE com JSON: {"revisoes":[{"id":"p1","resultado":"approved","codigos":[],"justificativa":"ate 20 palavras"}]} (resultado: approved ou rejected; codigos so quando rejected)."""


# --------------------------------------------------------------------------- contexto compacto
def _compactar_navio(nav: dict[str, Any], tick: int, tempestade: Tempestade | None,
                     memoria: Memoria, base: Proposta | None) -> dict[str, Any]:
    n = adaptar_navio(nav)
    alvo, atual = politicas.velocidade_alvo(n, tick, tempestade), float(n["target_speed_kn"])
    chegada, gasto = politicas.projetar(n, atual if atual > 0 else alvo, tick, tempestade)
    total = n["route_total_nm"]
    ultima = memoria.ultima_velocidade.get(n["logical_id"])
    return {
        "navio": n["logical_id"], "estado": n["ship_state"], "vel_alvo_kn": atual,
        "vel_efetiva_kn": float(n["effective_speed_kn"]), "vel_limite_kn": float(n["active_limit_speed_kn"]),
        "progresso_pct": round(100 * n["progress_nautical_mile_milli"] / 1000 / total, 1) if total else 0,
        "prazo_em_ticks": n["deadline_tick"] - tick,
        "chegada_prevista_tick": chegada if chegada <= politicas.TOTAL_TICKS else None,
        "comb_t": round(n["fuel_milli"] / 1000, 1), "comb_util_t": round(politicas.combustivel_util(n), 1),
        "consumo_previsto_t": round(gasto, 1), "tanque_t": n["fuel_capacity_milli"] // 1000,
        "avaria_e04": bool(n["e04_active"]), "idade_telemetria": n["state_age_ticks"],
        "vel_recomendada_kn": alvo,
        "troca_vel_ha_ticks": None if ultima is None else tick - ultima,
        "acoes_permitidas": [a.get("type") for a in nav.get("allowed_actions_now", []) if a.get("type") in ACOES_LLM],
        "sugestao_deterministica": None if base is None else
        {"papel": base.papel, "acao": base.acao, "motivo": base.motivo},
    }


def montar_contexto(pacote: dict[str, Any], memoria: Memoria,
                    base: dict[str, Proposta]) -> dict[str, Any]:
    tick, tempestade = tick_atual(pacote), ler_tempestade(pacote)
    eco = pacote["economy"]
    ctx: dict[str, Any] = {
        "tick": tick, "tick_final": politicas.TOTAL_TICKS, "caixa_livre_cent": eco["available_cent"],
        "preco_origem_cent_t": eco.get("origin_fuel_price_cent_per_t"),
        "eventos": [{"codigo": e["event_code"], "de": e["effective_from_tick"], "ate": e["effective_until_tick"],
                     "severidade": e["severity"], "parametros": e.get("parameters") or {}}
                    for e in pacote.get("active_events", [])],
        "navios": [_compactar_navio(nav, tick, tempestade, memoria, base.get(lid))
                   for lid, nav in pacote["ships"].items()],
    }
    if tempestade:
        ctx["tempestade"] = {"de": tempestade.de, "ate": tempestade.ate, "vel_segura_kn": tempestade.velocidade_segura}
    return ctx


def turno_relevante(pacote: dict[str, Any], base: dict[str, Proposta]) -> bool:
    """O LLM so e chamado quando ha o que decidir: poupa o TPM/TPD."""
    if base or pacote["meta"].get("window_summary", {}).get("new_events"):
        return True
    tempestade, tick = ler_tempestade(pacote), tick_atual(pacote)
    return bool(tempestade and tempestade.ativa(tick)) or any(
        n["operational_state"]["e04_active"] for n in pacote["ships"].values())


# --------------------------------------------------------------------------- validacao
def normalizar_acao(acao: Any, navio: dict[str, Any]) -> dict[str, Any] | None:
    """Acao do LLM -> formato canonico, ou None se for invalida."""
    if not isinstance(acao, dict):
        return None
    nome = str(acao.get("name") or acao.get("type") or "").upper()
    args = acao.get("arguments") if isinstance(acao.get("arguments"), dict) else acao
    try:
        if nome == "SET_SPEED":
            v = float(args["speed_kn"])
            if not 0 <= v <= float(navio["active_limit_speed_kn"]) or v * 2 != int(v * 2):
                return None
            return {"name": nome, "arguments": {"speed_kn": f"{v:.2f}"}}
        if nome == "BUY_FUEL":
            q = int(float(args["quantity_t"]))
            return {"name": nome, "arguments": {"quantity_t": q}} if q >= 5 and q % 5 == 0 else None
        if nome == "HOLD":
            codigo = str(args.get("reason_code") or "WEATHER_PRECAUTION")
            return {"name": nome, "arguments": {"reason_code": codigo if re.fullmatch(r"[A-Z_]{3,40}", codigo)
                                                else "WEATHER_PRECAUTION"}}
        if nome == "REPAIR":
            return {"name": nome, "arguments": {"fault_id": "E04_ENGINE_FAULT"}}
    except (KeyError, TypeError, ValueError):
        return None
    return None


def portao(p: Proposta, navio: dict[str, Any], tick: int, tempestade: Tempestade | None,
           caixa: int, memoria: Memoria) -> list[str]:
    """Vetos para uma proposta do LLM: os da politica + prazo, combustivel e anti-churn."""
    vetos = politicas.revisar(p, navio, tick, tempestade, caixa)
    nome, args = p.acao["name"], p.acao["arguments"]
    if p.papel != "safety" and navio["state_age_ticks"] > 0:
        vetos.append("STALE_TELEMETRY")
    if nome == "SET_SPEED":
        v, atual = float(args["speed_kn"]), float(navio["target_speed_kn"])
        if abs(v - atual) < 0.5:
            vetos.append("NOOP_SPEED")
        if (navio["ship_state"] == "UNDERWAY"
                and tick - memoria.ultima_velocidade.get(navio["logical_id"], -99) < politicas.INTERVALO_MIN_TICKS):
            vetos.append("CHURN_INTERVAL")
        chegada, gasto = politicas.projetar(navio, v, tick, tempestade)
        if v < atual and chegada > navio["deadline_tick"] - politicas.TICKS_DESCARGA - FOLGA_PRAZO_LLM:
            vetos.append("DEADLINE_AT_RISK")
        if gasto > politicas.combustivel_util(navio) and v > 0:
            vetos.append("FUEL_RESERVE_AT_RISK")
    elif nome == "BUY_FUEL":
        if tick > politicas.ULTIMO_TICK_COMPRA:
            vetos.append("PURCHASE_WINDOW_CLOSED")
        if memoria.compras.get(navio["logical_id"], 0) >= 2:
            vetos.append("PURCHASE_LIMIT")
    elif nome == "HOLD":
        tempestade_ativa = bool(tempestade and tempestade.ativa(tick))
        if not navio["e04_active"] and not tempestade_ativa:
            vetos.append("HOLD_NOT_JUSTIFIED")
    return list(dict.fromkeys(vetos))


# --------------------------------------------------------------------------- cerebros
class CerebroDeterministico:
    """Politica pura (etapas 2 e 3): sem LLM."""

    def decidir(self, pacote: dict[str, Any], memoria: Memoria) -> tuple[list[dict], list[Proposta]]:
        itens = propostas_do_turno(pacote, memoria)
        return [montar_decisao(p, v) for p, _, v in itens], [p for p, _, _ in itens]

    def resumo(self) -> dict[str, Any]:
        return {}


class CerebroLLM:
    def __init__(self, groq: ClienteGroq, modo: str = "completo", orcamento_tokens: int | None = None) -> None:
        if modo not in MODOS:
            raise ValueError(f"modo LLM invalido: {modo}")
        self.groq, self.modo, self.orcamento = groq, modo, orcamento_tokens
        self.stats: Counter[str] = Counter()
        self.desligado = False

    def resumo(self) -> dict[str, Any]:
        return {**self.stats, "chamadas": self.groq.chamadas, "tokens": self.groq.tokens_sessao,
                "tokens_dia": self.groq.uso.tokens}

    # --- chamadas ao modelo
    def _perguntar(self, sistema: str, ctx: dict[str, Any]) -> dict[str, Any] | None:
        if self.desligado:
            return None
        usuario = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
        if self.orcamento is not None and self.groq.tokens_sessao + self.groq.estimar(sistema, usuario) > self.orcamento:
            self.desligado = True
            log.warning("orcamento de tokens da execucao esgotado (%d): seguindo so com a politica deterministica",
                        self.orcamento)
            return None
        try:
            return self.groq.conversar(sistema, usuario)
        except OrcamentoEsgotado as erro:
            self.desligado = True
            log.warning("%s: seguindo so com a politica deterministica", erro)
        except ErroLLM as erro:
            self.stats["falhas_llm"] += 1
            log.warning("LLM falhou (%s); usando a politica deterministica neste turno", erro)
        return None

    # --- decisao do turno
    def decidir(self, pacote: dict[str, Any], memoria: Memoria) -> tuple[list[dict], list[Proposta]]:
        itens = propostas_do_turno(pacote, memoria)
        base = {p.logico: p for p, _, _ in itens}
        base_vetos = {p.logico: v for p, _, v in itens}
        if not turno_relevante(pacote, base):
            return [], []
        self.stats["turnos_relevantes"] += 1

        tick, tempestade, caixa = tick_atual(pacote), ler_tempestade(pacote), pacote["economy"]["available_cent"]
        navios = {lid: adaptar_navio(n) for lid, n in pacote["ships"].items()}
        criticas = {lid for lid, p in base.items() if p.papel == "safety" and not base_vetos[lid]}
        ctx = montar_contexto(pacote, memoria, base)

        candidatas = self._propostas_llm(ctx, navios, criticas, tick, tempestade, caixa, memoria)
        revisoes = self._revisar(ctx, candidatas, navios) if candidatas else {}

        decisoes: list[dict] = []
        propostas: list[Proposta] = []
        for lid in pacote["ships"]:
            cand = candidatas.get(lid)
            usar_base = base.get(lid)
            if cand is not None:
                proposta, vetos, rev_texto, autor = cand["proposta"], cand["vetos"], "", "llm"
                rev = revisoes.get(cand["id"])
                if rev and rev["resultado"] == "rejected":
                    if usar_base is not None and usar_base.acao == proposta.acao and not vetos:
                        self.stats["veto_llm_ignorado"] += 1   # coincide com a politica ja verificada
                    else:
                        vetos = vetos + [c for c in rev["codigos"] if c not in vetos] or ["LLM_REVIEW_REJECTED"]
                        self.stats["vetos_revisor_llm"] += 1
                if rev:
                    rev_texto = rev["justificativa"]
                if vetos:
                    self.stats["propostas_vetadas"] += 1
                else:
                    self.stats["propostas_llm_aprovadas"] += 1
                    if usar_base is not None and usar_base.acao == proposta.acao:
                        self.stats["iguais_a_politica"] += 1
                decisoes.append(montar_decisao(proposta, vetos, cand["justificativa"],
                                               rev_texto or None, prefixo=f"LLM_{proposta.papel}"))
                propostas.append(proposta)
                if vetos and usar_base is not None and usar_base.acao != proposta.acao:
                    self.stats["fallback_politica"] += 1
                    decisoes.append(montar_decisao(usar_base, base_vetos[lid]))
                    propostas.append(usar_base)
            elif usar_base is not None:
                if lid in criticas:
                    self.stats["resposta_critica_deterministica"] += 1
                decisoes.append(montar_decisao(usar_base, base_vetos[lid]))
                propostas.append(usar_base)
        return decisoes, propostas

    def _propostas_llm(self, ctx, navios, criticas, tick, tempestade, caixa, memoria) -> dict[str, dict]:
        resposta = self._perguntar(SISTEMA_PROPOSITOR, ctx)
        if resposta is None:
            return {}
        saida: dict[str, dict] = {}
        for item in resposta.get("propostas") or []:
            if not isinstance(item, dict) or item.get("navio") not in navios or item["navio"] in saida:
                self.stats["propostas_descartadas"] += 1
                continue
            lid, papel = item["navio"], str(item.get("papel") or "")
            acao = normalizar_acao(item.get("acao"), navios[lid])
            if papel not in REVISOR or acao is None:
                self.stats["propostas_descartadas"] += 1
                log.warning("proposta do LLM descartada (formato): %s", json.dumps(item, ensure_ascii=False)[:160])
                continue
            if lid in criticas:   # E01/E04: a resposta segura e da politica, o LLM nao a substitui
                self.stats["propostas_ignoradas_criticas"] += 1
                continue
            proposta = Proposta(papel, lid, acao, f"LLM_{papel}", "llm", "plan_change")
            vetos = portao(proposta, navios[lid], tick, tempestade, caixa, memoria)
            if not vetos and acao["name"] == "BUY_FUEL":
                caixa -= acao["arguments"]["quantity_t"] * politicas.PRECO_TETO_CENT_T
            saida[lid] = {"id": f"p{len(saida) + 1}", "proposta": proposta, "vetos": vetos,
                          "justificativa": str(item.get("justificativa") or "sem justificativa")[:240]}
        return saida

    def _revisar(self, ctx, candidatas: dict[str, dict], navios) -> dict[str, dict]:
        if self.modo != "completo":
            return {}
        a_revisar = {lid: c for lid, c in candidatas.items() if not c["vetos"]}
        if not a_revisar:
            return {}
        pedido = {"tick": ctx["tick"], "eventos": ctx["eventos"], "caixa_livre_cent": ctx["caixa_livre_cent"],
                  "navios": [n for n in ctx["navios"] if n["navio"] in a_revisar],
                  "propostas": [{"id": c["id"], "navio": lid, "papel": c["proposta"].papel,
                                 "revisor": REVISOR[c["proposta"].papel], "acao": c["proposta"].acao,
                                 "justificativa": c["justificativa"]} for lid, c in a_revisar.items()]}
        resposta = self._perguntar(SISTEMA_REVISOR, pedido)
        if resposta is None:
            return {}
        saida: dict[str, dict] = {}
        for r in resposta.get("revisoes") or []:
            if isinstance(r, dict) and r.get("resultado") in ("approved", "rejected") and r.get("id"):
                codigos = [str(c)[:40] for c in (r.get("codigos") or []) if isinstance(c, str)][:4]
                saida[str(r["id"])] = {"resultado": r["resultado"], "codigos": codigos,
                                       "justificativa": str(r.get("justificativa") or "")[:240]}
        return saida
