"""Canais em tempo real (SSE, WebSocket e webhook).

Os tres entregam o mesmo envelope: {seq, event_type, tick, ship_id, data}.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from .cliente import ClienteOcean, ErroOcean

log = logging.getLogger("ocean")

TIPOS = ("sse", "ws", "webhook", "rest")


class Caixa:
    """Guarda, de forma thread-safe, o que chegou pelo canal."""

    def __init__(self) -> None:
        """Inicializa os buffers e a condicao usados para sincronizar eventos."""
        self._cond = threading.Condition()
        self._vistos: set[int] = set()
        self._por_tick: Counter[int] = Counter()
        self._eventos: dict[str, dict] = {}

    def receber(self, registro: dict) -> None:
        """Armazena um registro recebido, ignorando sequencias ja processadas.

        Args:
            registro: Envelope do canal com sequencia, tipo, tick e dados.
        """
        with self._cond:
            if registro["seq"] in self._vistos:
                return
            self._vistos.add(registro["seq"])
            if registro["event_type"] == "event":
                self.guardar_eventos([registro["data"]])
            if registro.get("tick") is not None:
                self._por_tick[registro["tick"]] += 1
            self._cond.notify_all()

    def guardar_eventos(self, eventos: list[dict]) -> None:
        """Atualiza o historico de eventos, indexando cada um pelo identificador.

        Args:
            eventos: Eventos recebidos pela API REST ou pelo canal.
        """
        with self._cond:
            self._eventos.update({e["message_id"]: e for e in eventos})

    def eventos(self) -> list[dict]:
        """Retorna os eventos armazenados em ordem crescente de tick.

        Returns:
            Lista independente dos eventos ordenada pelo campo `tick`.
        """
        with self._cond:
            return sorted(self._eventos.values(), key=lambda e: e["tick"])

    def esperar(self, tick: int, quantidade: int, prazo_s: float) -> bool:
        """Espera ate chegarem as `quantidade` mensagens emitidas na abertura do tick.

        Args:
            tick: Tick cuja quantidade de mensagens sera aguardada.
            quantidade: Numero de mensagens esperado para o tick.
            prazo_s: Tempo maximo de espera em segundos.

        Returns:
            `True` se a quantidade foi recebida no prazo; caso contrario, `False`.
        """
        with self._cond:
            return self._cond.wait_for(lambda: self._por_tick[tick] >= quantidade, prazo_s)


def _ouvir_sse(url: str, caixa: Caixa, pronto: threading.Event, parar: threading.Event) -> None:
    """Consome o fluxo SSE e reconecta enquanto o sinal de parada nao for ativado.

    Args:
        url: URL base HTTP do servidor Ocean.
        caixa: Buffer no qual os envelopes recebidos serao armazenados.
        pronto: Evento sinalizado apos estabelecer a primeira conexao.
        parar: Evento que solicita o encerramento do consumidor.
    """
    while not parar.is_set():
        try:
            limites = httpx.Timeout(10.0, read=40.0)  # o servidor manda keepalive a cada 15 s
            with httpx.stream("GET", f"{url}/api/v1/stream", timeout=limites) as resposta:
                resposta.raise_for_status()
                pronto.set()
                for linha in resposta.iter_lines():
                    if parar.is_set():
                        return
                    if linha.startswith("data:"):
                        caixa.receber(json.loads(linha[5:]))
        except (httpx.HTTPError, ValueError) as erro:
            log.warning("SSE caiu (%s), reconectando", erro)
        parar.wait(1.0)


def _ouvir_ws(url: str, caixa: Caixa, pronto: threading.Event, parar: threading.Event) -> None:
    """Consome o fluxo WebSocket e reconecta enquanto o sinal de parada estiver inativo.

    Args:
        url: URL base HTTP do servidor Ocean.
        caixa: Buffer no qual os envelopes recebidos serao armazenados.
        pronto: Evento sinalizado apos estabelecer a primeira conexao.
        parar: Evento que solicita o encerramento do consumidor.
    """
    endereco = url.replace("http", "ws", 1) + "/ws/telemetry"
    while not parar.is_set():
        try:
            with connect(endereco, max_size=None) as ws:
                pronto.set()
                while not parar.is_set():
                    with contextlib.suppress(TimeoutError):
                        caixa.receber(json.loads(ws.recv(timeout=1.0)))
        except (OSError, ValueError, WebSocketException) as erro:
            log.warning("WebSocket caiu (%s), reconectando", erro)
        parar.wait(1.0)


class _ServidorWebhook(ThreadingHTTPServer):
    request_queue_size = 64  # o simulador entrega ate 32 webhooks em paralelo
    daemon_threads = True


def _receptor(segredo: str, caixa: Caixa) -> type[BaseHTTPRequestHandler]:
    """Cria o handler HTTP que valida assinaturas e recebe webhooks.

    Args:
        segredo: Segredo compartilhado usado para verificar a assinatura HMAC.
        caixa: Buffer que recebe os envelopes validos.

    Returns:
        Classe de handler configurada com o segredo e o buffer informados.
    """
    class Receptor(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            """Valida e processa um webhook POST, respondendo 204 ou 401."""
            corpo = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            esperado = hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
            valido = hmac.compare_digest(esperado, self.headers.get("X-Ocean-Signature-SHA256", ""))
            if valido:
                caixa.receber(json.loads(corpo))
            self.send_response(204 if valido else 401)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            """Suprime o log padrao do servidor HTTP.

            Args:
                *args: Argumentos fornecidos pelo handler HTTP padrao.
            """
            pass

    return Receptor


@contextlib.contextmanager
def abrir_canal(tipo: str, cliente: ClienteOcean, url_webhook: str | None = None,
                porta_webhook: int = 9000) -> Iterator[Caixa | None]:
    """Liga o canal escolhido e entrega a Caixa (None para `rest`).

    Args:
        tipo: Canal a abrir: `rest`, `sse`, `ws` ou `webhook`.
        cliente: Cliente usado para obter a URL e registrar/remover webhooks.
        url_webhook: URL publica de destino; se ausente, usa o endereco local.
        porta_webhook: Porta local em que o receptor de webhook sera iniciado.

    Yields:
        Buffer de eventos do canal ou `None` quando o tipo e `rest`.

    Raises:
        ErroOcean: Se SSE ou WebSocket nao conectar em ate 10 segundos.
        OSError: Se nao for possivel iniciar o servidor webhook local.
    """
    if tipo == "rest":
        yield None
        return
    caixa = Caixa()
    if tipo == "webhook":
        segredo = secrets.token_hex(24)
        servidor = _ServidorWebhook(("127.0.0.1", porta_webhook), _receptor(segredo, caixa))
        threading.Thread(target=servidor.serve_forever, daemon=True).start()
        webhook_id = None
        try:
            webhook_id = cliente.criar_webhook(url_webhook or f"http://127.0.0.1:{porta_webhook}/", segredo)
            yield caixa
        finally:
            if webhook_id:
                with contextlib.suppress(ErroOcean):
                    cliente.remover_webhook(webhook_id)
            servidor.shutdown()
            servidor.server_close()
        return
    pronto, parar = threading.Event(), threading.Event()
    ouvir: Callable = _ouvir_sse if tipo == "sse" else _ouvir_ws
    threading.Thread(target=ouvir, args=(cliente.url, caixa, pronto, parar), daemon=True).start()
    try:
        if not pronto.wait(10.0):
            raise ErroOcean(f"canal {tipo} nao conectou em 10 s")
        yield caixa
    finally:
        parar.set()
