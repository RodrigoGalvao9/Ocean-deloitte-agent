"""Cliente HTTP do simulador Ocean: sessao keep-alive, retentativas e identidades."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("ocean")

STATUS_TRANSITORIOS = {429, 502, 503, 504}
MAX_TENTATIVAS = 5
VERSAO_CONTRATO = "1.1.0-pilot"


def hash_acao(acao: dict[str, Any]) -> str:
    """Calcula o SHA-256 da acao canonica, equivalente ao JCS para o catalogo atual.

    Args:
        acao: Acao serializavel com nomes de campos canonicos.

    Returns:
        Hash SHA-256 hexadecimal com prefixo de dominio Ocean.
    """
    canonico = json.dumps(acao, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(b"ocean:action:v1\n" + canonico.encode("utf-8")).hexdigest()


class ErroOcean(RuntimeError):
    """Erro de comunicacao ou resposta inesperada da API Ocean."""

    pass


@dataclass
class Agente:
    """Identidade autenticada de um agente da frota."""

    actor_id: str
    papel: str
    token: str
    ultimo_heartbeat: str | None = None

    @property
    def cabecalhos(self) -> dict[str, str]:
        """Retorna os cabecalhos de autenticacao e identificacao do agente.

        Returns:
            Cabecalhos HTTP Bearer e `X-Actor-Id`.
        """
        return {"Authorization": f"Bearer {self.token}", "X-Actor-Id": self.actor_id}


class ClienteOcean:
    """Cliente HTTP para operacoes do simulador Ocean."""

    def __init__(self, url_base: str, token_admin: str | None = None, timeout_s: float = 15.0) -> None:
        """Cria a sessao HTTP e prepara os cabecalhos administrativos.

        Args:
            url_base: URL base HTTP do servidor Ocean.
            token_admin: Token administrativo opcional para operacoes de controle.
            timeout_s: Timeout padrao das requisicoes, em segundos.
        """
        self.url = url_base.rstrip("/")
        self.http = httpx.Client(base_url=self.url, timeout=timeout_s)
        self.cabecalhos_admin = {"Authorization": f"Bearer {token_admin}"} if token_admin else {}

    def fechar(self) -> None:
        """Fecha a sessao HTTP e libera suas conexoes."""
        self.http.close()

    def _enviar(self, metodo: str, caminho: str, corpo: dict | None = None,
                cabecalhos: dict | None = None) -> tuple[int, dict[str, Any]]:
        """Reenvia o mesmo corpo em falha transitoria.

        Comandos devem ser protegidos por `idempotency_key` para permitir repeticoes seguras.

        Args:
            metodo: Metodo HTTP da requisicao.
            caminho: Caminho da API relativo a URL base.
            corpo: Corpo JSON opcional da requisicao.
            cabecalhos: Cabecalhos adicionais ou de autenticacao.

        Returns:
            Tupla com o status HTTP e os dados JSON (ou detalhe textual limitado).

        Raises:
            ErroOcean: Se ocorrer falha de transporte em todas as tentativas.
        """
        for tentativa in range(1, MAX_TENTATIVAS + 1):
            try:
                resp = self.http.request(metodo, caminho, json=corpo, headers=cabecalhos)
                if resp.status_code not in STATUS_TRANSITORIOS or tentativa == MAX_TENTATIVAS:
                    try:
                        return resp.status_code, resp.json()
                    except ValueError:
                        return resp.status_code, {"detail": resp.text[:300]}
            except httpx.TransportError as erro:
                if tentativa == MAX_TENTATIVAS:
                    raise ErroOcean(f"{metodo} {caminho} falhou: {erro}") from erro
            log.warning("tentativa %d de %s %s falhou, repetindo", tentativa, metodo, caminho)
            time.sleep(0.5 * tentativa)
        raise ErroOcean(f"{metodo} {caminho} esgotou as tentativas")

    def _exigir_ok(self, metodo: str, caminho: str, corpo: dict | None = None,
                   cabecalhos: dict | None = None) -> dict[str, Any]:
        """Envia uma requisicao e exige resposta HTTP 200.

        Args:
            metodo: Metodo HTTP da requisicao.
            caminho: Caminho da API relativo a URL base.
            corpo: Corpo JSON opcional da requisicao.
            cabecalhos: Cabecalhos adicionais ou de autenticacao.

        Returns:
            Dados JSON da resposta.

        Raises:
            ErroOcean: Se a resposta nao tiver status HTTP 200 ou houver falha de transporte.
        """
        status, dados = self._enviar(metodo, caminho, corpo, cabecalhos)
        if status != 200:
            raise ErroOcean(f"{metodo} {caminho} -> HTTP {status}: {dados.get('detail', dados)}")
        return dados

    # Leitura
    def saude(self) -> dict[str, Any]:
        """Consulta o endpoint de saude do servidor.

        Returns:
            Dados de saude retornados pela API.
        """
        return self._exigir_ok("GET", "/health")

    def estado(self) -> dict[str, Any]:
        """Consulta o estado atual da rodada.

        Returns:
            Estado da rodada retornado pela API.
        """
        return self._exigir_ok("GET", "/api/v1/state")

    def eventos(self) -> list[dict[str, Any]]:
        """Consulta o historico de eventos da rodada.

        Returns:
            Lista de eventos retornada pela API.
        """
        return self._exigir_ok("GET", "/api/v1/events")["events"]

    def resultado(self) -> dict[str, Any]:
        """Consulta o resultado final da rodada.

        Returns:
            Envelope do resultado retornado pela API.
        """
        return self._exigir_ok("GET", "/api/v1/result")

    # Webhooks
    def criar_webhook(self, url: str, segredo: str) -> str:
        """Registra um webhook autenticado no servidor.

        Args:
            url: URL que recebera os eventos.
            segredo: Segredo compartilhado para assinatura dos payloads.

        Returns:
            Identificador do webhook criado.

        Raises:
            ErroOcean: Se a API nao aceitar o registro.
        """
        corpo = {"url": url, "secret": segredo, "description": "ocean-agentes"}
        return self._exigir_ok("POST", "/api/v1/webhooks", corpo)["webhook_id"]

    def remover_webhook(self, webhook_id: str) -> None:
        """Remove um webhook previamente registrado.

        Args:
            webhook_id: Identificador do webhook a remover.

        Raises:
            ErroOcean: Se a API nao confirmar a remocao.
        """
        self._exigir_ok("DELETE", f"/api/v1/webhooks/{webhook_id}")

    # Controle da rodada
    def reiniciar(self, modo: str, especificacoes: list[dict[str, Any]]) -> dict[str, Any]:
        """Reinicia a rodada com o modo e os agentes informados.

        Args:
            modo: Modo de cenario aceito pelo servidor.
            especificacoes: Configuracoes dos agentes a cadastrar na rodada.

        Returns:
            Estado ou confirmacao retornada pela API.

        Raises:
            ErroOcean: Se o servidor nao aceitar a reinicializacao.
        """
        corpo = {"scenario_mode": modo, "agents": especificacoes, "replace_defaults": True,
                 "reset_identities": True}
        return self._exigir_ok("POST", "/api/v1/round/reset", corpo, self.cabecalhos_admin)

    def abrir_tick(self, tick: int) -> int | None:
        """Abre um tick e retorna quantas mensagens o servidor emitiu.

        Retorna `None` se o tick abriu, mas a resposta se perdeu.

        Args:
            tick: Indice do tick que sera aberto.

        Returns:
            Quantidade de mensagens emitidas, ou `None` se o tick abriu mas a resposta se perdeu.

        Raises:
            ErroOcean: Se a requisicao falhar e o estado indicar que o tick nao foi aberto.
        """
        status, dados = self._enviar("POST", "/api/v1/round/open-tick", {"tick": tick}, self.cabecalhos_admin)
        if status == 200:
            return dados["emitted_count"]
        atual = self.estado()  # a resposta anterior pode ter se perdido
        if not (atual["window_open"] and atual["tick"] == tick):
            raise ErroOcean(f"open-tick {tick} -> HTTP {status}: {dados.get('detail', dados)}")
        return None

    def fechar_tick(self, tick: int) -> None:
        """Fecha o tick, aceitando tambem uma resposta perdida se o servidor o concluiu.

        Args:
            tick: Indice do tick que sera fechado.

        Raises:
            ErroOcean: Se a requisicao falhar e o estado nao confirmar o fechamento.
        """
        status, dados = self._enviar("POST", "/api/v1/round/close-tick", {"tick": tick}, self.cabecalhos_admin)
        if status == 200:
            return
        atual = self.estado()
        if atual["window_open"] or atual["ticks_completed"] != tick + 1:
            raise ErroOcean(f"close-tick {tick} -> HTTP {status}: {dados.get('detail', dados)}")

    def finalizar(self) -> None:
        """Finaliza a rodada no servidor.

        Raises:
            ErroOcean: Se a API nao confirmar a finalizacao.
        """
        self._exigir_ok("POST", "/api/v1/round/finalize", {}, self.cabecalhos_admin)

    # Mensagens dos agentes
    def heartbeat(self, agente: Agente, tick: int) -> bool:
        """Envia o heartbeat do agente e atualiza o elo da cadeia se aceito.

        Args:
            agente: Agente que envia o heartbeat.
            tick: Tick atual da rodada.

        Returns:
            `True` se aceito; `False` se recusado pela API.

        Raises:
            ErroOcean: Se as tentativas de transporte se esgotarem.
        """
        corpo: dict[str, Any] = {
            "heartbeat_id": f"hb-{agente.actor_id}-{tick:03d}",
            "agent_role": agente.papel,
            "status": "alive",
            "previous_heartbeat_message_id": agente.ultimo_heartbeat,
            "lease_expires_tick": min(431, tick + 2),  # o schema nao aceita 432
        }
        status, dados = self._enviar("POST", "/api/v1/heartbeats", corpo, agente.cabecalhos)
        if status != 200 and (dados.get("error") or {}).get("code") == "INTEGRITY_FAILURE":
            # Cadeia perdida (resposta nao recebida ou processo retomado): o servidor preenche o elo.
            corpo.pop("previous_heartbeat_message_id")
            status, dados = self._enviar("POST", "/api/v1/heartbeats", corpo, agente.cabecalhos)
        if status == 200:
            agente.ultimo_heartbeat = dados["heartbeat"]["message_id"]
            return True
        log.warning("tick %d: heartbeat de %s recusado: %s", tick, agente.actor_id, dados.get("detail"))
        return False

    def decisao(self, agente: Agente, corpo: dict[str, Any]) -> dict[str, Any] | None:
        """Envia uma proposta ou revisao e devolve a decisao aceita.

        Args:
            agente: Agente autenticado que envia a decisao.
            corpo: Payload completo de decisao exigido pela API.

        Returns:
            Objeto da decisao aceita ou `None` se recusada.

        Raises:
            ErroOcean: Se as tentativas de transporte se esgotarem.
        """
        status, dados = self._enviar("POST", "/api/v1/decisions", corpo, agente.cabecalhos)
        if status == 200:
            return dados["decision"]
        log.warning("decisao %s recusada: %s", corpo["decision_id"], dados.get("detail"))
        return None

    def comando(self, agente: Agente, corpo: dict[str, Any]) -> bool:
        """Envia um comando e informa se foi confirmado ou reconhecido como duplicado.

        Args:
            agente: Agente autenticado que envia o comando.
            corpo: Payload completo do comando exigido pela API.

        Returns:
            `True` se o comando foi confirmado ou ja havia sido processado; caso contrario, `False`.

        Raises:
            ErroOcean: Se as tentativas de transporte se esgotarem.
        """
        status, dados = self._enviar("POST", "/api/v1/commands", corpo, agente.cabecalhos)
        if status == 200 and dados.get("status") in ("committed", "duplicate"):
            return True
        log.warning("comando %s recusado (HTTP %d): %s", corpo["command_id"], status, dados.get("detail"))
        return False
