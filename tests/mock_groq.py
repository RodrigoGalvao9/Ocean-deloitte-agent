"""Groq simulado (API compativel com OpenAI) para testar o modo LLM sem rede nem chave.

    python tests/mock_groq.py --porta 9100 --modo ecoa

Modos: ecoa (segue a sugestao deterministica), ruidoso (mistura respostas ruins), erro500, rate429
(429 a cada 3 chamadas), veta (revisor rejeita tudo), lixo (resposta que nao e JSON), vazio (sem proposta).
"""
from __future__ import annotations

import argparse
import json
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _proposta_ruim(rng: random.Random, navios: list[dict]) -> dict:
    n = rng.choice(navios)
    ruins = [
        {"name": "SET_SPEED", "arguments": {"speed_kn": "18.00"}},          # perigoso em tempestade/combustivel
        {"name": "SET_SPEED", "arguments": {"speed_kn": "7.30"}},           # passo invalido
        {"name": "SET_SPEED", "arguments": {"speed_kn": str(n["vel_alvo_kn"])}},  # noop
        {"name": "SET_SPEED", "arguments": {"speed_kn": "0.00"}},           # parar o navio
        {"name": "BUY_FUEL", "arguments": {"quantity_t": 7}},               # nao multiplo de 5
        {"name": "BUY_FUEL", "arguments": {"quantity_t": 500}},             # estoura o tanque
        {"name": "HOLD", "arguments": {"reason_code": "BECAUSE"}},          # sem motivo
        {"name": "EXPLODE", "arguments": {}},                               # acao inexistente
    ]
    return {"navio": n["navio"], "papel": rng.choice(["navigation", "economy", "safety", "pirata"]),
            "acao": rng.choice(ruins), "justificativa": "palpite do modelo ruidoso"}


class Mock(BaseHTTPRequestHandler):
    modo = "ecoa"
    contador = 0
    rng = random.Random(7)

    def log_message(self, *a): pass

    def _responder(self, status: int, corpo: dict, cabecalhos: dict | None = None) -> None:
        dados = json.dumps(corpo).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        for k, v in (cabecalhos or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(dados)

    def do_POST(self) -> None:
        Mock.contador += 1
        pedido = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        sistema, usuario = pedido["messages"][0]["content"], pedido["messages"][1]["content"]
        ctx = json.loads(usuario)
        if self.modo == "erro500":
            return self._responder(500, {"error": {"message": "boom"}})
        if self.modo == "rate429" and Mock.contador % 3 == 0:
            return self._responder(429, {"error": {"message": "rate limit"}}, {"retry-after": "1"})
        if self.modo == "lixo":
            texto = "desculpe, nao consigo responder isso em JSON"
        elif "[PAPEL:PROPOSITOR]" in sistema:
            propostas = []
            if self.modo != "vazio":
                for n in ctx["navios"]:
                    sug = n.get("sugestao_deterministica")
                    if sug:
                        propostas.append({"navio": n["navio"], "papel": sug["papel"], "acao": sug["acao"],
                                          "justificativa": f"segue a sugestao ({sug['motivo']})"})
                if self.modo == "ruidoso":
                    propostas.append(_proposta_ruim(self.rng, ctx["navios"]))
                    if self.rng.random() < 0.3:
                        propostas = propostas[::-1]
            texto = json.dumps({"propostas": propostas})
            if self.modo == "ruidoso" and self.rng.random() < 0.15:
                texto = "```json\n" + texto + "\n```"   # cerca de codigo: deve ser tolerada
        else:
            rej = self.modo == "veta" or (self.modo == "ruidoso" and self.rng.random() < 0.3)
            texto = json.dumps({"revisoes": [
                {"id": p["id"], "resultado": "rejected" if rej else "approved",
                 "codigos": ["MOCK_VETO"] if rej else [], "justificativa": "revisao simulada"}
                for p in ctx["propostas"]]})
        entrada = (len(sistema) + len(usuario)) // 3
        self._responder(200, {"choices": [{"message": {"content": texto}, "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": entrada, "completion_tokens": 180,
                                        "total_tokens": entrada + 180}})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=9100)
    ap.add_argument("--modo", default="ecoa")
    a = ap.parse_args()
    Mock.modo = a.modo
    ThreadingHTTPServer(("127.0.0.1", a.porta), Mock).serve_forever()
