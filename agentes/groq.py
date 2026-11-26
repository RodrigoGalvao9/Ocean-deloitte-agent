"""Cliente LLM (API compativel com OpenAI: Groq, OpenRouter) respeitando os limites do plano gratuito.

Plano `openai/gpt-oss-120b`: 30 RPM, 1.000 RPD, 8.000 TPM, 200.000 TPD. O gargalo e o TPM/TPD, por isso
ha um limitador de janela deslizante (requisicoes e tokens por minuto) e um contador diario persistente.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("ocean")

MODELO_PADRAO = "openai/gpt-oss-120b"
URL_PADRAO = "https://api.groq.com/openai/v1"
MODELO_REVISOR = "nvidia/nemotron-3.5-lightning:free"
URL_REVISOR = "https://openrouter.ai/api/v1"
SAIDA_ESTIMADA = 500     # tokens de saida (incluindo raciocinio) usados na reserva antes da chamada
MAX_TENTATIVAS = 3


class ErroLLM(RuntimeError):
    """Falha ao obter uma resposta utilizavel do modelo (o chamador deve usar o fallback)."""


class OrcamentoEsgotado(ErroLLM):
    pass


@dataclass(frozen=True)
class Limites:
    """Margem de ~15% sobre o plano (30 RPM / 8.000 TPM / 200.000 TPD) para nao bater no teto."""
    rpm: int = 25
    tpm: int = 6_800
    tpd: int = 170_000


class UsoDiario:
    """Tokens e requisicoes do dia (UTC), persistidos para valer entre execucoes."""

    def __init__(self, caminho: Path | None, hoje: Callable[[], str] | None = None) -> None:
        self.caminho = caminho
        self._hoje = hoje or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        self.dia, self.tokens, self.requisicoes = self._hoje(), 0, 0
        if caminho and caminho.is_file():
            try:
                dados = json.loads(caminho.read_text(encoding="utf-8"))
                if dados.get("dia") == self.dia:
                    self.tokens, self.requisicoes = int(dados["tokens"]), int(dados["requisicoes"])
            except (ValueError, KeyError, OSError):
                log.warning("contador diario ilegivel em %s; recomecando do zero", caminho)

    def somar(self, tokens: int) -> None:
        if self._hoje() != self.dia:
            self.dia, self.tokens, self.requisicoes = self._hoje(), 0, 0
        self.tokens += tokens
        self.requisicoes += 1
        if self.caminho:
            try:
                self.caminho.write_text(json.dumps({"dia": self.dia, "tokens": self.tokens,
                                                    "requisicoes": self.requisicoes}), encoding="utf-8")
            except OSError:
                pass


class Limitador:
    """Janela deslizante de 60 s para requisicoes e tokens."""

    def __init__(self, limites: Limites, relogio: Callable[[], float], dormir: Callable[[float], None]) -> None:
        self.limites, self._relogio, self._dormir = limites, relogio, dormir
        self._reqs: deque[float] = deque()
        self._toks: deque[list] = deque()   # [instante, tokens]

    def _expirar(self, agora: float) -> None:
        while self._reqs and agora - self._reqs[0] >= 60:
            self._reqs.popleft()
        while self._toks and agora - self._toks[0][0] >= 60:
            self._toks.popleft()

    def reservar(self, estimativa: int) -> list:
        """Espera ate caber e reserva `estimativa` tokens. Retorna o registro (para `acertar`)."""
        estimativa = min(estimativa, self.limites.tpm)  # uma chamada maior que o TPM nunca caberia
        while True:
            agora = self._relogio()
            self._expirar(agora)
            usados = sum(t for _, t in self._toks)
            if len(self._reqs) < self.limites.rpm and usados + estimativa <= self.limites.tpm:
                registro = [agora, estimativa]
                self._reqs.append(agora)
                self._toks.append(registro)
                return registro
            espera = 60 - (agora - min(self._reqs[0] if self._reqs else agora,
                                       self._toks[0][0] if self._toks else agora))
            espera = max(0.5, min(espera, 60))
            log.info("LLM aguardando %.0f s pelo limite por minuto (usados %d/%d tokens, %d/%d requisicoes)",
                     espera, usados, self.limites.tpm, len(self._reqs), self.limites.rpm)
            self._dormir(espera)

    @staticmethod
    def acertar(registro: list, real: int) -> None:
        registro[1] = real

    def penalizar(self, segundos: float) -> None:
        self._dormir(min(max(segundos, 1.0), 60.0))


def extrair_json(texto: str) -> dict[str, Any]:
    """JSON do conteudo, tolerando cercas ``` e texto ao redor."""
    limpo = re.sub(r"```(?:json)?", "", texto or "").strip()
    ini, fim = limpo.find("{"), limpo.rfind("}")
    if ini < 0 or fim <= ini:
        raise ErroLLM(f"resposta sem JSON: {limpo[:120]!r}")
    try:
        dados = json.loads(limpo[ini:fim + 1])
    except ValueError as erro:
        raise ErroLLM(f"JSON invalido: {erro}") from erro
    if not isinstance(dados, dict):
        raise ErroLLM("a resposta nao e um objeto JSON")
    return dados


class ClienteLLM:
    def __init__(self, chave: str, modelo: str = MODELO_PADRAO, url_base: str = URL_PADRAO,
                 limites: Limites | None = None, uso: UsoDiario | None = None, timeout_s: float = 60.0,
                 http: httpx.Client | None = None, relogio: Callable[[], float] = time.monotonic,
                 dormir: Callable[[float], None] = time.sleep, nome: str = "Groq",
                 opcionais: dict[str, Any] | None = None, campo_saida: str = "max_completion_tokens",
                 max_saida: int = 800) -> None:
        self.nome, self.campo_saida, self.max_saida = nome, campo_saida, max_saida
        if not chave:
            raise ErroLLM(f"chave de API do {nome} nao configurada")
        if not url_base.startswith(("http://", "https://")):
            raise ErroLLM(f"URL base do {nome} invalida: {url_base!r} (deve ser http(s); o modelo vai em *_MODEL)")
        self.modelo, self.url = modelo, url_base.rstrip("/")
        self.limites = limites or Limites()
        self.uso = uso or UsoDiario(None)
        self.espera_s = 0.0                          # total dormido: limite por minuto, 429, retentativas
        self.ultimo_uso: dict[str, Any] | None = None

        def contar(segundos: float) -> None:
            self.espera_s += segundos
            dormir(segundos)

        self.limitador = Limitador(self.limites, relogio, contar)
        self._dormir = contar
        self.http = http or httpx.Client(timeout=timeout_s)
        self._cabecalhos = {"Authorization": f"Bearer {chave}", "Content-Type": "application/json"}
        self._opcionais: dict[str, Any] = dict(opcionais if opcionais is not None else
                                               {"reasoning_effort": "low", "response_format": {"type": "json_object"}})
        self.tokens_sessao = self.chamadas = 0

    def fechar(self) -> None:
        self.http.close()

    def cabe(self, estimativa: int) -> bool:
        return self.uso.tokens + estimativa <= self.limites.tpd

    @staticmethod
    def estimar(*textos: str) -> int:
        return sum(len(t) for t in textos) // 3 + SAIDA_ESTIMADA   # ~3 caracteres/token: conservador

    def conversar(self, sistema: str, usuario: str) -> dict[str, Any]:
        estimativa, self.ultimo_uso = self.estimar(sistema, usuario), None
        if not self.cabe(estimativa):
            raise OrcamentoEsgotado(f"orcamento diario de tokens esgotado ({self.uso.tokens}/{self.limites.tpd})")
        registro = self.limitador.reservar(estimativa)
        corpo = {"model": self.modelo, "temperature": 0.2, self.campo_saida: self.max_saida,
                 "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}],
                 **self._opcionais}
        ultimo_erro, limitadas = "sem tentativas", 0
        for tentativa in range(1, MAX_TENTATIVAS + 1):
            try:
                resp = self.http.post(f"{self.url}/chat/completions", json=corpo, headers=self._cabecalhos)
            except httpx.TransportError as erro:
                ultimo_erro = f"transporte: {erro}"
                self._dormir(1.0 * tentativa)
                continue
            if resp.status_code == 429:
                espera = _segundos(resp.headers.get("retry-after"), 5.0)
                limitadas += 1
                if espera > 60 or limitadas == MAX_TENTATIVAS:   # cota do provedor: insistir so trava a rodada
                    raise OrcamentoEsgotado(f"limite do provedor (429, retry-after {espera:.0f} s)")
                ultimo_erro = f"429 (aguardando {espera:.0f}s)"
                log.warning("%s 429: aguardando %.0f s", self.nome, espera)
                self.limitador.penalizar(espera)
                continue
            if resp.status_code >= 500:
                ultimo_erro = f"HTTP {resp.status_code}"
                self._dormir(1.0 * tentativa)
                continue
            if resp.status_code == 400 and "json_validate_failed" not in resp.text:
                recusados = [c for c in self._opcionais if c in resp.text]   # so o parametro citado
                if recusados:
                    log.warning("%s recusou %s; repetindo sem", self.nome, ", ".join(recusados))
                    for chave in recusados:
                        corpo.pop(chave, None)
                        del self._opcionais[chave]
                    ultimo_erro = "HTTP 400"
                    continue
            if resp.status_code != 200:
                raise ErroLLM(f"{self.nome} HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                dados = resp.json()
            except ValueError as erro:
                raise ErroLLM(f"resposta 200 sem JSON: {erro}") from erro
            return self._tratar(dados, registro)
        raise ErroLLM(f"{self.nome} indisponivel apos {MAX_TENTATIVAS} tentativas: {ultimo_erro}")

    def _tratar(self, dados: dict[str, Any], registro: list) -> dict[str, Any]:
        real = int((dados.get("usage") or {}).get("total_tokens") or registro[1])
        self.limitador.acertar(registro, real)
        self.uso.somar(real)
        self.ultimo_uso = dados.get("usage")
        self.tokens_sessao += real
        self.chamadas += 1
        try:
            escolha = dados["choices"][0]
            texto = escolha["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as erro:
            raise ErroLLM(f"resposta fora do formato esperado: {erro}") from erro
        if not isinstance(texto, str) or not texto.strip():
            raise ErroLLM(f"resposta vazia (finish_reason={escolha.get('finish_reason')}); raciocinio pode ter esgotado a saida")
        return extrair_json(texto)


def _segundos(valor: str | None, padrao: float) -> float:
    try:
        return float(valor) if valor is not None else padrao
    except ValueError:
        return padrao
