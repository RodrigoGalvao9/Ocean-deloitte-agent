"""Agentes LLM (proponente e revisor) sobre a politica deterministica.

O LLM propoe e revisa, mas nunca tem a ultima palavra sobre seguranca:
  * resposta a E01 (tempestade) e E04 (avaria) continua deterministica (o LLM nao a sobrescreve);
  * toda proposta do LLM passa por `politicas.revisar` + portoes extras (prazo, combustivel, anti-churn);
  * proposta vetada, invalida ou falha do LLM => a decisao deterministica do mesmo navio e usada.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from typing import Any

from . import politicas, telemetria
from .decisao import (REVISOR, ler_tempestade, montar_decisao, propostas_do_turno, tick_atual, adaptar_navio)
from .groq import ClienteLLM, ErroLLM, OrcamentoEsgotado
from .politicas import Memoria, Proposta, Tempestade

log = logging.getLogger("ocean")

ACOES_LLM = ("SET_SPEED", "HOLD", "REPAIR", "BUY_FUEL")
FOLGA_PRAZO_LLM = 15   # ticks de folga exigidos de uma desaceleracao proposta pelo LLM
MODOS = ("completo", "proposta")
MAX_FALHAS_SEGUIDAS = 3   # provedor fora do ar: desliga o LLM em vez de esperar em todo turno

SISTEMA_PROPOSITOR = """[PAPEL:PROPOSITOR] Voce e a equipe de propositores (navigation, economy, safety) de uma frota de 4 navios graneleiros na Operacao Oceanica. Objetivo: entregar a carga no prazo, sem violar a reserva de combustivel, ao menor custo; seguranca em primeiro lugar.
Regras:
- No maximo 1 acao por navio e so com motivo material. Acao redundante (mesma velocidade) ou nova troca de velocidade em menos de 6 ticks da anterior e rejeitada como churn.
- Acoes: SET_SPEED {speed_kn: multiplo de 0.5, 0 a vel_limite}; HOLD {reason_code}; REPAIR {fault_id}; BUY_FUEL {quantity_t: multiplo de 5, so AT_ORIGIN ate o tick 11}.
- papel: economy para BUY_FUEL, safety para HOLD/REPAIR/tempestade, navigation para velocidade.
- "sugestao_deterministica" e uma recomendacao calculada e verificada: so se afaste dela com motivo claro; se concorda, repita-a.
Responda SOMENTE com JSON: {"propostas":[{"navio":"S1-FAROL","papel":"navigation","acao":{"name":"SET_SPEED","arguments":{"speed_kn":"12.50"}},"justificativa":"ate 25 palavras"}]}. Use {"propostas":[]} se nada deve ser feito."""

SISTEMA_REVISOR = """[PAPEL:REVISOR] Voce e o revisor independente da frota (safety revisa navigation e economy; navigation revisa safety). Confira cada proposta contra o estado do navio: reserva de combustivel, prazo, tempestade, estado operacional e limites. Aprove apenas o que for seguro e materialmente util; vete o que arriscar reserva, prazo ou seguranca, ou for redundante.
Quando houver "alerta", o servidor nao permite a acao ou recusou a anterior desse navio: aprove somente se ela continuar correta.
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


def turno_relevante(pacote: dict[str, Any], base: dict[str, Proposta], criticas: set[str]) -> bool:
    """O LLM so e chamado quando pode mudar algo: evento novo ou proposta nao critica.

    Tempestade/avaria sem nada alem da resposta critica nao conta: o LLM nao pode substitui-la.
    """
    return bool(set(base) - criticas or pacote["meta"].get("window_summary", {}).get("new_events"))


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
    except (KeyError, TypeError, ValueError, OverflowError):
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


def fora_do_permitido(navio: dict[str, Any], acao: dict[str, Any]) -> str | None:
    """Motivo pelo qual o servidor nao permite agora uma acao que passou pelas regras, ou None."""
    permitidas = {a["action"]: a.get("constraints") or {} for a in navio.get("allowed_actions_now", [])}
    nome = acao["name"]
    if nome not in permitidas:
        return navio.get("blocked_actions_reasons", {}).get(nome) or f"{nome} fora de allowed_actions_now"
    maximo = permitidas[nome].get("max_quantity_t")
    if nome == "BUY_FUEL" and maximo is not None and acao["arguments"]["quantity_t"] > maximo:
        return f"quantidade acima do maximo aceito pelo servidor ({maximo} t)"
    return None


def _chave(logico: str, acao: dict[str, Any]) -> tuple[str, str]:
    return logico, json.dumps(acao, sort_keys=True)


# --------------------------------------------------------------------------- cerebros
class CerebroDeterministico:
    """Politica pura (etapas 2 e 3): sem LLM."""

    def decidir(self, pacote: dict[str, Any], memoria: Memoria) -> tuple[list[dict], list[Proposta]]:
        itens = propostas_do_turno(pacote, memoria)
        return [montar_decisao(p, v) for p, _, v in itens], [p for p, _, _ in itens]

    def alertar(self, logico: str, motivo: str) -> None:
        pass   # sem LLM nao ha revisor reserva

    def resumo(self) -> dict[str, Any]:
        return {}


class CerebroLLM:
    def __init__(self, propositor: ClienteLLM, modo: str = "completo", orcamento_tokens: int | None = None,
                 revisor: ClienteLLM | None = None) -> None:
        if modo not in MODOS:
            raise ValueError(f"modo LLM invalido: {modo}")
        self.propositor, self.revisor = propositor, revisor or propositor   # sem revisor proprio: o mesmo
        self.modo, self.orcamento = modo, orcamento_tokens
        self.stats: Counter[str] = Counter()
        self.desligados: set[ClienteLLM] = set()                 # cada cliente cai sozinho
        self.falhas_seguidas: Counter[ClienteLLM] = Counter()
        self.alertas: dict[str, str] = {}   # navio -> recusa do servidor ainda nao revisada

    def alertar(self, logico: str, motivo: str) -> None:
        self.alertas[logico] = motivo[:240]

    def resumo(self) -> dict[str, Any]:
        uso = {papel: {"provedor": c.nome, "modelo": c.modelo, "chamadas": c.chamadas, "tokens": c.tokens_sessao,
                       "requisicoes_dia": c.uso.requisicoes, "desligado": c in self.desligados}
               for papel, c in (("propositor", self.propositor), ("revisor", self.revisor))}
        return {**self.stats, **uso}

    # --- chamadas ao modelo
    def _desligar(self, cliente: ClienteLLM, motivo: str) -> None:
        self.desligados.add(cliente)
        log.warning("%s: %s desligado, a politica deterministica continua", motivo, cliente.nome)
        telemetria.evento("llm-desligado", "WARNING", provedor=cliente.nome, motivo=motivo)

    def _perguntar(self, sistema: str, ctx: dict[str, Any]) -> dict[str, Any] | None:
        papel = "revisor" if sistema == SISTEMA_REVISOR else "propositor"
        cliente = self.revisor if papel == "revisor" else self.propositor
        if cliente in self.desligados:
            return None
        usuario = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
        if (self.orcamento is not None and cliente is self.propositor
                and cliente.tokens_sessao + cliente.estimar(sistema, usuario) > self.orcamento):
            self._desligar(cliente, f"orcamento de tokens da execucao esgotado ({self.orcamento})")
            return None
        antes, inicio, espera_antes = cliente.tokens_sessao, time.monotonic(), cliente.espera_s
        log.info("tick %3d LLM %s: consultando %s (%s)", ctx["tick"], papel, cliente.modelo, cliente.nome)
        with telemetria.observar(f"llm-{papel}", "generation", model=cliente.modelo,
                                 input=[{"role": "system", "content": sistema},
                                        {"role": "user", "content": usuario}]) as geracao:
            def medidas() -> dict[str, Any]:
                return {"tick": ctx["tick"], "papel": papel, "provedor": cliente.nome,
                        "espera_s": round(cliente.espera_s - espera_antes, 1)}
            try:
                resposta = cliente.conversar(sistema, usuario)
                uso = cliente.ultimo_uso or {}
                geracao.update(output=resposta, metadata=medidas(), usage_details={
                    chave: uso[origem] for chave, origem in (("input", "prompt_tokens"),
                    ("output", "completion_tokens"), ("total", "total_tokens"))
                    if isinstance(uso.get(origem), int)})
                log.info("tick %3d LLM %s: ok em %.1f s, %d tokens (hoje: %d tokens, %d requisicoes)", ctx["tick"],
                         papel, time.monotonic() - inicio, cliente.tokens_sessao - antes, cliente.uso.tokens,
                         cliente.uso.requisicoes)
                self.falhas_seguidas[cliente] = 0
                return resposta
            except OrcamentoEsgotado as erro:
                geracao.update(level="WARNING", status_message=str(erro), metadata=medidas())
                self._desligar(cliente, str(erro))
            except ErroLLM as erro:
                geracao.update(level="ERROR", status_message=str(erro), metadata=medidas())
                self.stats["falhas_llm"] += 1
                log.warning("LLM %s falhou (%s); seguindo sem ele neste turno", papel, erro)
                self.falhas_seguidas[cliente] += 1
                if self.falhas_seguidas[cliente] >= MAX_FALHAS_SEGUIDAS:
                    self._desligar(cliente, f"{MAX_FALHAS_SEGUIDAS} falhas seguidas")
        return None

    # --- decisao do turno
    def decidir(self, pacote: dict[str, Any], memoria: Memoria) -> tuple[list[dict], list[Proposta]]:
        itens = propostas_do_turno(pacote, memoria)
        base = {p.logico: p for p, _, _ in itens}
        base_vetos = {p.logico: v for p, _, v in itens}
        tick, tempestade, caixa = tick_atual(pacote), ler_tempestade(pacote), pacote["economy"]["available_cent"]
        navios = {lid: adaptar_navio(n) for lid, n in pacote["ships"].items()}
        criticas = {lid for lid, p in base.items() if p.papel == "safety" and not base_vetos[lid]}
        ctx = montar_contexto(pacote, memoria, base)

        candidatas: dict[str, dict] = {}
        if turno_relevante(pacote, base, criticas):
            self.stats["turnos_relevantes"] += 1
            candidatas = self._propostas_llm(ctx, navios, criticas, tick, tempestade, caixa, memoria)
        else:
            self.stats["turnos_sem_llm"] += 1
        pareceres = self._revisar(ctx, self._pedidos_de_revisao(pacote, base, base_vetos, criticas, candidatas))

        decisoes: list[dict] = []
        propostas: list[Proposta] = []

        def incluir(proposta: Proposta, vetos: list[str], racional: str | None = None,
                    prefixo: str | None = None) -> list[str]:
            origem = "regra"
            rev = None if vetos else pareceres.get(_chave(proposta.logico, proposta.acao))
            if rev and rev["resultado"] == "rejected":
                vetos = rev["codigos"] or ["LLM_REVIEW_REJECTED"]
                self.stats["vetos_revisor_llm"] += 1
                origem = "revisor-llm"
            if vetos:
                telemetria.evento("veto", "WARNING", tick=tick, navio=proposta.logico, acao=proposta.acao,
                                  origem=origem, codigos=vetos)
            decisoes.append(montar_decisao(proposta, vetos, racional,
                                           (rev or {}).get("justificativa") or None, prefixo))
            propostas.append(proposta)
            return vetos

        for lid in pacote["ships"]:
            cand = candidatas.get(lid)
            usar_base = base.get(lid)
            if cand is not None:
                proposta = cand["proposta"]
                diverge = usar_base is None or usar_base.acao != proposta.acao
                if incluir(proposta, cand["vetos"], cand["justificativa"], f"LLM_{proposta.papel}"):
                    self.stats["propostas_vetadas"] += 1
                    if usar_base is not None and diverge:
                        self.stats["fallback_politica"] += 1
                        telemetria.evento("fallback-politica", tick=tick, navio=lid, acao=usar_base.acao)
                        incluir(usar_base, base_vetos[lid])
                else:
                    self.stats["propostas_llm_aprovadas"] += 1
                    if not diverge:
                        self.stats["iguais_a_politica"] += 1
            elif usar_base is not None:
                if lid in criticas:
                    self.stats["resposta_critica_deterministica"] += 1
                incluir(usar_base, base_vetos[lid])
        return decisoes, propostas

    def _pedidos_de_revisao(self, pacote, base, base_vetos, criticas, candidatas) -> list[dict]:
        """O que vai ao revisor: divergencias do LLM e, como guarda reserva, acoes aprovadas pelas
        regras que o servidor nao permite ou de navios com recusa anterior. Resposta critica
        (E01/E04) permitida pelo servidor nao fica sujeita a veto do LLM."""
        pedidos: list[dict] = []
        janela = pacote["meta"].get("window_open")   # janela fechada bloqueia tudo: nao e alerta
        for lid, navio in pacote["ships"].items():
            cand, pol = candidatas.get(lid), base.get(lid)
            diverge = cand is not None and (pol is None or pol.acao != cand["proposta"].acao)
            opcoes = []
            if cand is not None and not cand["vetos"]:
                opcoes.append((cand["proposta"], cand["justificativa"], diverge, False))
            if pol is not None and not base_vetos[lid] and (cand is None or diverge):
                opcoes.append((pol, pol.motivo, False, lid in criticas))
            alerta_usado = False
            for proposta, justificativa, divergente, critica in opcoes:
                bloqueio = fora_do_permitido(navio, proposta.acao) if janela else None
                alerta = bloqueio or (None if critica else self.alertas.get(lid))
                if divergente or alerta:
                    pedidos.append({"proposta": proposta, "justificativa": justificativa, "alerta": alerta})
                    alerta_usado = alerta_usado or bool(alerta)
            if alerta_usado:
                self.alertas.pop(lid, None)
                self.stats["alertas_revisados"] += 1
        return pedidos

    def _propostas_llm(self, ctx, navios, criticas, tick, tempestade, caixa, memoria) -> dict[str, dict]:
        resposta = self._perguntar(SISTEMA_PROPOSITOR, ctx)
        if resposta is None:
            return {}
        saida: dict[str, dict] = {}
        lista = resposta.get("propostas")
        for item in lista if isinstance(lista, list) else []:
            if (not isinstance(item, dict) or not isinstance(item.get("navio"), str)
                    or item["navio"] not in navios or item["navio"] in saida):
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

    def _revisar(self, ctx, pedidos: list[dict]) -> dict[tuple[str, str], dict]:
        if self.modo != "completo" or not pedidos:
            return {}
        por_id = {f"p{i}": p for i, p in enumerate(pedidos, 1)}
        navios = {p["proposta"].logico for p in pedidos}
        pedido = {"tick": ctx["tick"], "eventos": ctx["eventos"], "caixa_livre_cent": ctx["caixa_livre_cent"],
                  "navios": [n for n in ctx["navios"] if n["navio"] in navios],
                  "propostas": [{"id": i, "navio": p["proposta"].logico, "papel": p["proposta"].papel,
                                 "revisor": REVISOR[p["proposta"].papel], "acao": p["proposta"].acao,
                                 "justificativa": p["justificativa"],
                                 **({"alerta": p["alerta"]} if p["alerta"] else {})} for i, p in por_id.items()]}
        resposta = self._perguntar(SISTEMA_REVISOR, pedido)
        lista = (resposta or {}).get("revisoes")
        saida: dict[tuple[str, str], dict] = {}
        for r in lista if isinstance(lista, list) else []:
            if not isinstance(r, dict) or r.get("resultado") not in ("approved", "rejected"):
                continue
            alvo = por_id.get(str(r.get("id")))
            if alvo is None:
                continue
            codigos = r.get("codigos") if isinstance(r.get("codigos"), list) else []
            saida[_chave(alvo["proposta"].logico, alvo["proposta"].acao)] = {
                "resultado": r["resultado"], "codigos": [c[:40] for c in codigos if isinstance(c, str)][:4],
                "justificativa": str(r.get("justificativa") or "")[:240]}
        return saida
