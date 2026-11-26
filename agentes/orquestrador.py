"""Loop de ticks: heartbeat -> proposta -> revisao -> comando -> fechamento."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import politicas, rodada_turnos
from .rodada_turnos import AVANCO_BASE
from .canais import TIPOS, Caixa, abrir_canal
from .cliente import VERSAO_CONTRATO, Agente, ClienteOcean, ErroOcean, hash_acao

log = logging.getLogger("ocean")

RAIZ = Path(__file__).resolve().parent.parent
VERSAO_POLITICA = "politicas-1.0.0"
PRAZO_CANAL_S = 2.0

# papel do proponente -> papel do revisor (sempre um agente diferente)
REVISOR = {"navigation": "safety", "safety": "navigation", "economy": "safety"}
NOMES = {"navigation": "navegador", "safety": "guardiao", "economy": "economista"}


def carregar_env(caminho: Path) -> None:
    if not caminho.is_file():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            chave, valor = linha.split("=", 1)
            os.environ.setdefault(chave.strip(), valor.strip().strip("\"'"))


def especificacoes(equipe: str) -> list[dict[str, Any]]:
    return [{"actor_id": f"{equipe}-{nome}", "role": papel, "version": VERSAO_POLITICA}
            for papel, nome in NOMES.items()]


def agentes_da_equipe(equipe: str) -> dict[str, Agente]:
    # Token deterministico atribuido pelo servidor quando nenhum e informado no cadastro.
    return {e["role"]: Agente(e["actor_id"], e["role"], f"tok-{e['actor_id']}-poc")
            for e in especificacoes(equipe)}


def executar_cadeia(cliente: ClienteOcean, agentes: dict[str, Agente], proposta: politicas.Proposta,
                    estado: dict, tick: int, tempestade: politicas.Tempestade | None,
                    memoria: politicas.Memoria) -> str:
    """Proposta, revisao cruzada e comando. Retorna 'committed', 'vetoed' ou 'rejected'."""
    navio = estado["ships"][proposta.logico]
    navio_id = navio["ship_id"]
    telemetria = estado["latest_telemetry"][navio_id]
    autor, revisor = agentes[proposta.papel], agentes[REVISOR[proposta.papel]]
    sufixo = f"{navio_id}-{tick:03d}"
    acao_sha = hash_acao(proposta.acao)
    base = {
        "ship_id": navio_id,
        "policy_version": VERSAO_POLITICA,
        "input_state_version": navio["state_version"],
        "action_hash": acao_sha,
        "materiality": {"kind": proposta.tipo, "key": proposta.chave},
    }
    enviada = cliente.decisao(autor, {
        **base,
        "decision_id": f"dec-{sufixo}-p",
        "decision_kind": "proposal",
        "agent_role": autor.papel,
        "input_refs": [{"message_id": telemetria["message_id"],
                        "payload_sha256": telemetria["integrity"]["payload_sha256"]}],
        "action": proposta.acao,
        "reason_codes": [proposta.motivo],
        "constraints_checked": [{"constraint": "PROPOSER_PRECONDITIONS", "result": "pass"}],
    })
    if enviada is None:
        return "rejected"

    vetos = politicas.revisar(proposta, navio, tick, tempestade, estado["economy"]["available_cent"])
    revisao = cliente.decisao(revisor, {
        **base,
        "decision_id": f"dec-{sufixo}-r",
        "decision_kind": "review",
        "agent_role": revisor.papel,
        "input_refs": [{"message_id": enviada["message_id"],
                        "payload_sha256": enviada["integrity"]["payload_sha256"]}],
        "proposal_message_id": enviada["message_id"],
        "proposal_agent_id": autor.actor_id,
        "review_result": "rejected" if vetos else "approved",
        "review_codes": vetos or ["INDEPENDENT_CHECK_PASSED"],
    })
    if revisao is None:
        return "rejected"
    if vetos:
        log.info("tick %d %s: %s vetado por %s (%s)", tick, proposta.logico, proposta.acao["name"],
                 revisor.actor_id, ", ".join(vetos))
        return "vetoed"

    if proposta.acao["name"] == "BUY_FUEL":
        memoria.compras[proposta.logico] = memoria.compras.get(proposta.logico, 0) + 1
    comando_id = f"cmd-{sufixo}"
    aceito = cliente.comando(autor, {
        "contract_version": VERSAO_CONTRATO,
        "command_id": comando_id,
        "proposal_decision_id": f"dec-{sufixo}-p",
        "review_decision_id": f"dec-{sufixo}-r",
        "action_hash": acao_sha,
        "ship_id": navio_id,
        "expected_state_version": navio["state_version"],
        "valid_from_tick": tick,
        "expires_at_tick": tick,
        "idempotency_key": f"{estado['team_id']}:{estado['round_id']}:{proposta.acao['name']}:{comando_id}",
        "action": proposta.acao,
    })
    if not aceito:
        return "rejected"
    if proposta.acao["name"] == "SET_SPEED":
        memoria.ultima_velocidade[proposta.logico] = tick
    log.info("tick %3d %-8s %-9s %s [%s]", tick, proposta.logico, proposta.acao["name"],
             json.dumps(proposta.acao["arguments"]), proposta.motivo)
    return "committed"


def jogar_tick(cliente: ClienteOcean, agentes: dict[str, Agente], pool: ThreadPoolExecutor, tick: int,
               memoria: politicas.Memoria, placar: dict[str, int], caixa_canal: Caixa | None) -> None:
    emitidas = cliente.abrir_tick(tick)
    placar["heartbeats_falhos"] += list(pool.map(lambda a: cliente.heartbeat(a, tick), agentes.values())).count(False)

    futuro_estado = pool.submit(cliente.estado)
    if caixa_canal is None:
        eventos = cliente.eventos()
    else:
        if emitidas is None or not caixa_canal.esperar(tick, emitidas, PRAZO_CANAL_S):
            placar["canal_atrasado"] += 1  # reconcilia pelo REST para nao decidir sem os eventos
            caixa_canal.guardar_eventos(cliente.eventos())
        eventos = caixa_canal.eventos()
    estado, tempestade = futuro_estado.result(), politicas.ler_tempestade(eventos)

    caixa = estado["economy"]["available_cent"]
    propostas = [p for navio in estado["ships"].values()
                 if (p := politicas.propor(navio, tick, tempestade, caixa, memoria))]
    # Compras sao sequenciais para nao disputar o mesmo caixa; o resto vai em paralelo.
    compras = [p for p in propostas if p.acao["name"] == "BUY_FUEL"][:1]
    demais = [p for p in propostas if p.acao["name"] != "BUY_FUEL"]
    cadeia = lambda p: executar_cadeia(cliente, agentes, p, estado, tick, tempestade, memoria)
    for resultado in [*map(cadeia, compras), *pool.map(cadeia, demais)]:
        placar[resultado] += 1

    cliente.fechar_tick(tick)


def imprimir_resultado(resultado: dict[str, Any]) -> None:
    carga = resultado["result_envelope"]["payload"]
    log.info("score_final=%s rank_eligible=%s", carga["score_final"], carga["rank_eligible"])
    log.info("dimensoes: %s", json.dumps(carga.get("dimensions"), ensure_ascii=False))
    log.info("hard gates: %s", carga.get("hard_gates") or "nenhum")


def main(argv: list[str] | None = None) -> int:
    carregar_env(RAIZ / ".env")
    porta = os.getenv("OCEAN_PORT", "8000")
    args = argparse.ArgumentParser(prog="python -m agentes", description="Agentes autonomos da frota Ocean")
    args.add_argument("--url", default=os.getenv("OCEAN_BASE_URL", f"http://127.0.0.1:{porta}"))
    args.add_argument("--modo", choices=("conformance", "official"),
                      default=os.getenv("OCEAN_DEFAULT_SCENARIO_MODE", "conformance"))
    grupo = args.add_mutually_exclusive_group()
    grupo.add_argument("--reiniciar", action="store_true", help="descarta a rodada atual do servidor")
    grupo.add_argument("--retomar", action="store_true", help="continua uma rodada que este programa iniciou")
    args.add_argument("--canal", choices=TIPOS, default=os.getenv("OCEAN_CANAL", "sse"))
    args.add_argument("--webhook-url", default=os.getenv("OCEAN_WEBHOOK_URL") or None,
                      help="URL publica que o servidor chama (tunel); padrao: http://127.0.0.1:<porta>/")
    args.add_argument("--webhook-porta", type=int, default=int(os.getenv("OCEAN_WEBHOOK_PORTA") or 9000))
    args.add_argument("--saida", type=Path, default=RAIZ / "results")
    args.add_argument("--modo-turnos", action="store_true",
                      help="usa /api/v1/turn/*: sem heartbeats, janela de tempo nem canal em tempo real")
    args.add_argument("--avanco", type=int, default=int(os.getenv("OCEAN_AVANCO") or AVANCO_BASE),
                      help="ticks por turno no modo por turnos (padrao %(default)s)")
    args.add_argument("--llm", action="store_true", help="agentes LLM sobre a politica (Groq propoe, OpenRouter revisa); exige --modo-turnos")
    args.add_argument("--llm-modo", choices=("completo", "proposta"), default="completo",
                      help="completo: LLM propoe e revisa; proposta: so propoe (gasta ~metade dos tokens)")
    args.add_argument("--llm-orcamento", type=int, default=int(os.getenv("OCEAN_LLM_ORCAMENTO") or 120_000),
                      help="maximo de tokens do Groq nesta execucao; ao esgotar segue so com a politica")
    opcoes = args.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if opcoes.llm and not opcoes.modo_turnos:
        args.error("--llm exige --modo-turnos")
    if opcoes.modo_turnos:
        if not 1 <= opcoes.avanco <= 432:
            args.error("--avanco deve estar entre 1 e 432")
        return rodada_turnos.executar(opcoes)

    cliente = ClienteOcean(opcoes.url, os.getenv("OCEAN_ADMIN_TOKEN") or None)
    try:
        saude = cliente.saude()
        if not saude["package_valid"]:
            raise ErroOcean("o servidor informa package_valid=false; nao vale a pena rodar")
        estado = cliente.estado()
        equipe, modo = estado["team_id"], opcoes.modo
        virgem = estado["status"] == "IDLE" and estado["ticks_completed"] == 0 and not estado["window_open"]

        if opcoes.retomar:
            if estado["finalized"]:
                raise ErroOcean("a rodada ja foi finalizada; use --reiniciar")
            primeiro, modo = estado["ticks_completed"], estado["scenario_mode"]
        else:
            if not virgem and not opcoes.reiniciar:
                raise ErroOcean(f"ja existe uma rodada ({estado['status']}, tick {estado['tick']}). "
                                "Use --retomar para continuar ou --reiniciar para descarta-la")
            cliente.reiniciar(modo, especificacoes(equipe))
            primeiro = 0

        agentes = agentes_da_equipe(equipe)
        memoria = politicas.Memoria.vazia()
        placar = {"committed": 0, "vetoed": 0, "rejected": 0, "heartbeats_falhos": 0, "canal_atrasado": 0}
        log.info("%s | %s | modo %s | canal %s | a partir do tick %d", opcoes.url, equipe, modo,
                 opcoes.canal, primeiro)
        inicio = time.perf_counter()
        with abrir_canal(opcoes.canal, cliente, opcoes.webhook_url, opcoes.webhook_porta) as caixa_canal, \
                ThreadPoolExecutor(max_workers=6) as pool:
            if caixa_canal:
                caixa_canal.guardar_eventos(cliente.eventos())  # historico, caso seja retomada
            for tick in range(primeiro, politicas.TOTAL_TICKS):
                jogar_tick(cliente, agentes, pool, tick, memoria, placar, caixa_canal)
                if tick % 48 == 47:
                    log.info("--- tick %d concluido (%.0f s)", tick, time.perf_counter() - inicio)

        cliente.finalizar()
        resultado = cliente.resultado()
        opcoes.saida.mkdir(parents=True, exist_ok=True)
        destino = opcoes.saida / f"final_result_{modo}.json"
        destino.write_text(json.dumps(resultado, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("placar: %s", placar)
        imprimir_resultado(resultado)
        log.info("resultado salvo em %s", destino)
        return 0
    except ErroOcean as erro:
        log.error("%s", erro)
        return 1
    except KeyboardInterrupt:
        log.error("interrompido; continue com --retomar")
        return 130
    finally:
        cliente.fechar()


if __name__ == "__main__":
    sys.exit(main())
