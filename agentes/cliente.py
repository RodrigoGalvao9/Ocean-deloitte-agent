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
    """SHA-256 da acao canonica. Para strings e inteiros (todo o catalogo), equivale ao JCS."""
    canonico = json.dumps(acao, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(b"ocean:action:v1\n" + canonico.encode("utf-8")).hexdigest()


class ErroOcean(RuntimeError):
    pass


@dataclass
class Agente:
    actor_id: str
    papel: str
    token: str
    ultimo_heartbeat: str | None = None

    @property
    def cabecalhos(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "X-Actor-Id": self.actor_id}


class ClienteOcean:
    def __init__(self, url_base: str, token_admin: str | None = None, timeout_s: float = 15.0) -> None:
        self.url = url_base.rstrip("/")
        self.http = httpx.Client(base_url=self.url, timeout=timeout_s)
        self.cabecalhos_admin = {"Authorization": f"Bearer {token_admin}"} if token_admin else {}

    def fechar(self) -> None:
        self.http.close()

    def _enviar(self, metodo: str, caminho: str, corpo: dict | None = None,
                cabecalhos: dict | None = None) -> tuple[int, dict[str, Any]]:
        """Reenvia o MESMO corpo em falha transitoria (comandos sao protegidos por idempotency_key)."""
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
        status, dados = self._enviar(metodo, caminho, corpo, cabecalhos)
        if status != 200:
            raise ErroOcean(f"{metodo} {caminho} -> HTTP {status}: {dados.get('detail', dados)}")
        return dados

    # Leitura
    def saude(self) -> dict[str, Any]:
        return self._exigir_ok("GET", "/health")

    def estado(self) -> dict[str, Any]:
        return self._exigir_ok("GET", "/api/v1/state")

    def eventos(self) -> list[dict[str, Any]]:
        return self._exigir_ok("GET", "/api/v1/events")["events"]

    def resultado(self) -> dict[str, Any]:
        return self._exigir_ok("GET", "/api/v1/result")

    # Webhooks
    def criar_webhook(self, url: str, segredo: str) -> str:
        corpo = {"url": url, "secret": segredo, "description": "ocean-agentes"}
        return self._exigir_ok("POST", "/api/v1/webhooks", corpo)["webhook_id"]

    def remover_webhook(self, webhook_id: str) -> None:
        self._exigir_ok("DELETE", f"/api/v1/webhooks/{webhook_id}")

    # Controle da rodada
    def reiniciar(self, modo: str, especificacoes: list[dict[str, Any]]) -> dict[str, Any]:
        corpo = {"scenario_mode": modo, "agents": especificacoes, "replace_defaults": True,
                 "reset_identities": True}
        return self._exigir_ok("POST", "/api/v1/round/reset", corpo, self.cabecalhos_admin)

    def abrir_tick(self, tick: int) -> int | None:
        """Retorna quantas mensagens o servidor emitiu (None se a resposta se perdeu)."""
        status, dados = self._enviar("POST", "/api/v1/round/open-tick", {"tick": tick}, self.cabecalhos_admin)
        if status == 200:
            return dados["emitted_count"]
        atual = self.estado()  # a resposta anterior pode ter se perdido
        if not (atual["window_open"] and atual["tick"] == tick):
            raise ErroOcean(f"open-tick {tick} -> HTTP {status}: {dados.get('detail', dados)}")
        return None

    def fechar_tick(self, tick: int) -> None:
        status, dados = self._enviar("POST", "/api/v1/round/close-tick", {"tick": tick}, self.cabecalhos_admin)
        if status == 200:
            return
        atual = self.estado()
        if atual["window_open"] or atual["ticks_completed"] != tick + 1:
            raise ErroOcean(f"close-tick {tick} -> HTTP {status}: {dados.get('detail', dados)}")

    def finalizar(self) -> None:
        self._exigir_ok("POST", "/api/v1/round/finalize", {}, self.cabecalhos_admin)

    # Mensagens dos agentes
    def heartbeat(self, agente: Agente, tick: int) -> bool:
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
        status, dados = self._enviar("POST", "/api/v1/decisions", corpo, agente.cabecalhos)
        if status == 200:
            return dados["decision"]
        log.warning("decisao %s recusada: %s", corpo["decision_id"], dados.get("detail"))
        return None

    def comando(self, agente: Agente, corpo: dict[str, Any]) -> bool:
        status, dados = self._enviar("POST", "/api/v1/commands", corpo, agente.cabecalhos)
        if status == 200 and dados.get("status") in ("committed", "duplicate"):
            return True
        log.warning("comando %s recusado (HTTP %d): %s", corpo["command_id"], status, dados.get("detail"))
        return False
