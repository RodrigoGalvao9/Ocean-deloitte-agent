"""Rodada completa pelo modo por turnos: pacote -> decisoes -> submit, ate o tick 432."""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from . import decisao, telemetria
from .cliente import ClienteOcean, ErroOcean
from .groq import (ClienteLLM, ErroLLM, Limites, UsoDiario, MODELO_PADRAO, MODELO_REVISOR, URL_PADRAO,
                   URL_REVISOR)
from .llm_agentes import CerebroDeterministico, CerebroLLM
from .politicas import Memoria, Proposta
from .turnos import ClienteTurnos, ErroTurno, ResultadoTurno

log = logging.getLogger("ocean")

AVANCO_BASE = 6       # mesmo score do avanco de 1 tick com ~5x menos turnos (medido na etapa 2)
MAX_DESCARTES = 3     # tentativas de reenvio apos descartar decisoes recusadas pelo servidor


def _descartar_invalidas(decisoes: list[dict], propostas: list[Proposta],
                         erro: ErroTurno) -> tuple[list[dict], list[Proposta]] | None:
    """Remove as decisoes que o servidor apontou (`index`). None se o erro nao permite isolar."""
    motivos = {e["index"]: e.get("message", "") for e in erro.erros
               if isinstance(e, dict) and isinstance(e.get("index"), int) and 0 <= e["index"] < len(decisoes)}
    if not motivos:
        return None
    for i, motivo in sorted(motivos.items()):
        log.warning("decisao descartada (%s %s): %s", decisoes[i]["ship_id"], decisoes[i]["action"]["name"], motivo)
    manter = [i for i in range(len(decisoes)) if i not in motivos]
    return [decisoes[i] for i in manter], [propostas[i] for i in manter]


def jogar_turno(turnos: ClienteTurnos, pacote: dict[str, Any], memoria: Memoria, base: int,
                placar: dict[str, int], cerebro: Any = None) -> ResultadoTurno:
    """Um turno, registrado no Langfuse (quando ativo) com decisoes e desfecho."""
    tick = decisao.tick_atual(pacote)
    with telemetria.observar("turno", input={"tick": tick, "avanco": base}) as turno:
        resultado, decisoes = _jogar_turno(turnos, pacote, memoria, base, placar, cerebro)
        relatorio = resultado.relatorio
        turno.update(output={
            "decisoes": decisoes, "ate_tick": relatorio.get("to_tick"),
            "motivos_parada": relatorio.get("stop_reasons"),
            "aplicadas": [{"navio": a["ship_id"], "acao": a["action"], "status": a["status"]}
                          for a in relatorio.get("applied_decisions", [])]})
    return resultado


def _jogar_turno(turnos: ClienteTurnos, pacote: dict[str, Any], memoria: Memoria, base: int,
                 placar: dict[str, int], cerebro: Any) -> tuple[ResultadoTurno, list[dict]]:
    """Delibera e envia um turno. Decisao recusada pelo servidor e descartada, nao derruba a rodada."""
    tick = decisao.tick_atual(pacote)
    cerebro = cerebro or CerebroDeterministico()
    decisoes, propostas = cerebro.decidir(pacote, memoria)
    avanco = base
    for tentativa in range(MAX_DESCARTES + 2):
        try:
            resultado = turnos.enviar(pacote, decisoes, avancar=avanco)
            break
        except ErroTurno as erro:
            if not decisoes:
                raise   # nem avancar sem decisoes funciona
            for e in erro.erros:
                if isinstance(e, dict) and isinstance(e.get("index"), int) and 0 <= e["index"] < len(decisoes):
                    cerebro.alertar(decisoes[e["index"]]["ship_id"], str(e.get("message") or erro.codigo))
                    telemetria.evento("recusa-servidor", "WARNING", tick=tick, decisao=decisoes[e["index"]],
                                      motivo=e.get("message"))
            restante = _descartar_invalidas(decisoes, propostas, erro)
            if restante is None or tentativa >= MAX_DESCARTES:
                log.warning("submit recusado (%s); avancando 1 tick sem decisoes para redeliberar", erro)
                restante, avanco = ([], []), 1
                telemetria.evento("submit-sem-decisoes", "WARNING", tick=tick, motivo=str(erro))
            placar["descartadas"] += len(decisoes) - len(restante[0])
            decisoes, propostas = restante
    if resultado.recuperado:
        placar["respostas_perdidas"] += 1  # sem relatorio por decisao: a memoria nao e atualizada
    else:
        decisao.atualizar_memoria(memoria, propostas, resultado.relatorio, tick)
        for a in resultado.relatorio["applied_decisions"]:
            if a["status"] == "committed":
                log.info("tick %3d %-8s %-9s %s [%s]", tick, a["ship_id"], a["action"]["name"],
                         json.dumps(a["action"]["arguments"]), a["proposal_rationale"].split(":")[0])
            elif a["status"] != "vetoed":
                log.warning("tick %3d %-8s %s %s: %s", tick, a["ship_id"], a["action"]["name"], a["status"], a["reason"])
                cerebro.alertar(a["ship_id"], a["reason"])
                telemetria.evento("recusa-servidor", "WARNING", tick=tick, navio=a["ship_id"], motivo=a["reason"])
            placar[a["status"] if a["status"] in ("committed", "vetoed") else "rejected"] += 1
    return resultado, decisoes


def _criar_cerebro(opcoes: argparse.Namespace) -> CerebroDeterministico | CerebroLLM:
    if not opcoes.llm:
        return CerebroDeterministico()
    limites = Limites(rpm=int(os.getenv("GROQ_RPM") or Limites.rpm), tpm=int(os.getenv("GROQ_TPM") or Limites.tpm),
                      tpd=int(os.getenv("GROQ_TPD") or Limites.tpd))
    uso = UsoDiario(Path(os.getenv("GROQ_USO_ARQUIVO") or opcoes.saida / ".groq_uso.json"))
    propositor = ClienteLLM(os.getenv("GROQ_API_KEY", ""), os.getenv("GROQ_MODEL") or MODELO_PADRAO,
                            os.getenv("GROQ_BASE_URL") or URL_PADRAO, limites, uso)
    revisor = None
    if opcoes.llm_modo == "completo" and os.getenv("OPENROUTER_API_KEY"):
        revisor = ClienteLLM(
            os.environ["OPENROUTER_API_KEY"], os.getenv("OPENROUTER_MODEL") or MODELO_REVISOR,
            os.getenv("OPENROUTER_BASE_URL") or URL_REVISOR,
            Limites(rpm=int(os.getenv("OPENROUTER_RPM") or 18), tpm=1_000_000, tpd=10_000_000),
            UsoDiario(Path(os.getenv("OPENROUTER_USO_ARQUIVO") or opcoes.saida / ".openrouter_uso.json")),
            timeout_s=30.0, nome="OpenRouter", opcionais={"reasoning": {"effort": "low"}},
            campo_saida="max_tokens", max_saida=4000)   # folga: o raciocinio conta na saida
    cerebro = CerebroLLM(propositor, opcoes.llm_modo, opcoes.llm_orcamento, revisor)
    log.info("LLM propositor: %s %s | revisor: %s %s | modo %s | orcamento do propositor %s tokens", propositor.nome,
             propositor.modelo, cerebro.revisor.nome, cerebro.revisor.modelo, opcoes.llm_modo, opcoes.llm_orcamento)
    return cerebro


def executar(opcoes: argparse.Namespace) -> int:
    from .orquestrador import imprimir_resultado  # import tardio: orquestrador chama este modulo

    cliente = ClienteOcean(opcoes.url, os.getenv("OCEAN_ADMIN_TOKEN") or None)
    turnos = ClienteTurnos(cliente)
    cerebro = None
    try:
        saude = cliente.saude()
        if not saude["package_valid"]:
            raise ErroOcean("o servidor informa package_valid=false; nao vale a pena rodar")
        estado = cliente.estado()
        virgem = estado["status"] == "IDLE" and estado["ticks_completed"] == 0 and not estado["window_open"]

        if opcoes.retomar:
            if estado["finalized"]:
                raise ErroOcean("a rodada ja foi finalizada; use --reiniciar")
            modo = estado["scenario_mode"]
            turnos.configurar_agentes(reiniciar=False)           # idempotente: "already_configured"
            pacote = turnos.pacote(abrir=False)
        else:
            if not virgem and not opcoes.reiniciar:
                raise ErroOcean(f"ja existe uma rodada ({estado['status']}, tick {estado['tick']}). "
                                "Use --retomar para continuar ou --reiniciar para descarta-la")
            modo = opcoes.modo
            turnos.configurar_agentes(reiniciar=bool(opcoes.reiniciar), modo=modo, equipe=estado["team_id"])
            pacote = turnos.pacote(modo=modo)

        cerebro = _criar_cerebro(opcoes)
        telemetria.iniciar(f"{estado['team_id']}-{modo}-{time.strftime('%Y%m%d-%H%M%S')}",
                           [modo, "llm" if opcoes.llm else "deterministico"])
        memoria = Memoria.vazia()  # ao retomar, so o intervalo anti-churn recomeca do zero
        placar = {"committed": 0, "vetoed": 0, "rejected": 0, "descartadas": 0, "respostas_perdidas": 0}
        log.info("%s | %s | modo %s | MODO POR TURNOS (avanco %d) | a partir do tick %d", opcoes.url,
                 estado["team_id"], modo, opcoes.avanco, decisao.tick_atual(pacote))
        inicio, n_turnos, proximo_marco = time.perf_counter(), 0, (decisao.tick_atual(pacote) // 48 + 1) * 48

        while not pacote["meta"]["round_completed"]:
            resultado = jogar_turno(turnos, pacote, memoria, opcoes.avanco, placar, cerebro)
            pacote, n_turnos = resultado.proximo, n_turnos + 1
            if decisao.tick_atual(pacote) >= proximo_marco:
                log.info("--- tick %d (%d turnos, %.0f s)", decisao.tick_atual(pacote), n_turnos,
                         time.perf_counter() - inicio)
                proximo_marco = (decisao.tick_atual(pacote) // 48 + 1) * 48

        final = turnos.finalizar()
        resultado_final = cliente.resultado()
        opcoes.saida.mkdir(parents=True, exist_ok=True)
        destino = opcoes.saida / f"final_result_{modo}_turnos.json"
        destino.write_text(json.dumps(resultado_final, indent=2, ensure_ascii=False), encoding="utf-8")
        gov = pacote["meta"]["governance_progress"]
        log.info("turnos: %d | placar: %s | cadeias creditadas: %d (meta %d)", n_turnos, placar,
                 gov["credited_chains"], gov["credited_chains_target"])
        if opcoes.llm:
            log.info("LLM: %s", json.dumps(cerebro.resumo(), ensure_ascii=False))
        imprimir_resultado(resultado_final)
        score = resultado_final["result_envelope"]["payload"].get("score_final")
        telemetria.evento("resumo-rodada", placar=placar, turnos=n_turnos, llm=cerebro.resumo(), score_final=score)
        if score is not None:
            telemetria.pontuar("score_final", float(score))
        prova = (final.get("result_envelope", {}).get("payload", {}) or {}).get("multi_agent_proof") or {}
        log.info("prova multiagente: %s", prova.get("compliant", "n/d"))
        log.info("resultado salvo em %s", destino)
        return 0
    except (ErroOcean, ErroLLM) as erro:
        log.error("%s", erro)
        return 1
    except KeyboardInterrupt:
        log.error("interrompido; continue com --modo-turnos --retomar")
        return 130
    finally:
        telemetria.fechar()
        cliente.fechar()
        if isinstance(cerebro, CerebroLLM):
            cerebro.propositor.fechar()
            cerebro.revisor.fechar()
