# ocean-agentes

Back-end multiagente da frota Operacao Oceanica. Nao importa nada do simulador: fala com ele so pela API
(rotas em `<OCEAN_BASE_URL>/docs`).

## Como rodar (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env      # ajuste OCEAN_BASE_URL
.\.venv\Scripts\python.exe -m agentes
```

| Opcao | Efeito |
|---|---|
| `--canal sse` / `ws` / `webhook` / `rest` | por onde chegam os eventos (padrao `sse`) |
| `--modo conformance` / `official` | cenario |
| `--reiniciar` | **descarta** a rodada atual do servidor |
| `--retomar` | continua uma rodada interrompida (ainda nao testado) |
| `--url` | substitui `OCEAN_BASE_URL` |

### Webhook com servidor remoto

O servidor precisa alcancar sua maquina. Abra um tunel (ngrok, Cloudflare Tunnel) para a porta 9000 e use
`--canal webhook --webhook-url https://seu-tunel`. Cada entrega e validada por HMAC-SHA256.

## Rotas usadas

| Finalidade | Rota |
|---|---|
| Saude e estado | `GET /health`, `GET /api/v1/state`, `GET /api/v1/events`, `GET /api/v1/result` |
| Tempo real | `GET /api/v1/stream`, `WS /ws/telemetry`, `POST` e `DELETE /api/v1/webhooks` |
| Rodada | `POST /api/v1/round/reset`, `open-tick`, `close-tick`, `finalize` |
| Agentes | `POST /api/v1/heartbeats`, `decisions`, `commands` |

## Funcionamento

Por tick: abre a janela, envia heartbeats, espera o canal entregar as mensagens do tick, proposta,
revisao por outro agente, comando e fecha. Se o canal atrasar mais de 2 s, os eventos sao lidos por REST.
O estado dos navios vem sempre de `/api/v1/state`, porque a telemetria nao traz todos os campos usados.

- `navegador`: velocidade por prazo e combustivel
- `guardiao`: tempestade, avaria e revisao
- `economista`: abastecimento antes de zarpar

Os agentes conduzem os ticks porque, no simulador testado (modo aluno), o modo continuo (`round/start`)
nao deixou janela aberta para agentes externos. Os coeficientes de consumo em `agentes/politicas.py`
vieram das especificacoes no codigo do servidor.
