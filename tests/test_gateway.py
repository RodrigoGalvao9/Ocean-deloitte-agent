"""Testes de aceite do gateway: cada camada precisa vetar o que deve vetar."""
from agentes import politicas
from agentes.nucleo.gateway import Contexto, Gateway
from agentes.politicas import Memoria, Proposta, Tempestade
from tests.test_nucleo import navio


def ctx(**kw):
    base = dict(tick=0, avanco=6, tempestade=None, caixa_livre_cent=10**12, memoria=Memoria.vazia())
    base.update(kw)
    return Contexto(**base)


def prop(papel, acao, logico="S2-MARE"):
    return Proposta(papel, logico, acao, "TESTE", "k", "plan_change")


def vel(v):
    return {"name": "SET_SPEED", "arguments": {"speed_kn": f"{v:.2f}"}}


def buy(q):
    return {"name": "BUY_FUEL", "arguments": {"quantity_t": q}}


def vetos(p, n=None, c=None):
    n, c = n or navio(), c or ctx()
    return Gateway().avaliar_lote([p], {n["logical_id"]: n}, c)[0].vetos


def test_economista_nao_muda_velocidade():
    assert "ACTION_NOT_ALLOWED_FOR_ROLE" in vetos(prop("economy", vel(12)))


def test_navegador_nao_compra_combustivel():
    assert "ACTION_NOT_ALLOWED_FOR_ROLE" in vetos(prop("navigation", buy(10)))


def test_papel_desconhecido():
    assert "ROLE_UNKNOWN" in vetos(prop("hacker", vel(12)))


def test_safe_stop_so_no_modo_degradado():
    acao = {"name": "SAFE_STOP", "arguments": {}}
    assert "SAFE_STOP_NOT_AUTHORIZED" in vetos(prop("coordinator", acao))
    assert vetos(prop("coordinator", acao), c=ctx(degradado=True)) == ()


def test_schema_invalido():
    assert "SCHEMA_INVALID" in vetos(prop("economy", {"name": "BUY_FUEL", "arguments": {"quantity_t": 7}}))
    assert "SCHEMA_INVALID" in vetos(prop("navigation", {"name": "SET_SPEED", "arguments": {}}))


def test_compra_valida_aprovada():
    assert vetos(prop("economy", buy(20))) == ()


def test_compra_fora_da_janela():
    assert "PURCHASE_WINDOW_CLOSED" in vetos(prop("economy", buy(20)), c=ctx(tick=15))


def test_compra_acima_do_caixa():
    assert "OPERATIONAL_RESERVE" in vetos(prop("economy", buy(20)), c=ctx(caixa_livre_cent=1))


def test_reserva_violada_no_meio_do_turno():
    n = navio(ship_state="UNDERWAY", fuel_milli=6_000, target_speed_kn="8.00", effective_speed_kn="8.00")
    v = vetos(prop("navigation", vel(14)), n, ctx(avanco=25, memoria=Memoria({"S2-MARE": -99}, {})))
    assert "FUEL_RESERVE_AT_RISK_HORIZON" in v


def test_anti_churn():
    n = navio(ship_state="UNDERWAY", target_speed_kn="10.00", effective_speed_kn="10.00", fuel_milli=100_000)
    c = ctx(tick=10, memoria=Memoria({"S2-MARE": 8}, {}))
    assert "CHURN_INTERVAL" in vetos(prop("navigation", vel(12)), n, c)


def test_noop_speed():
    n = navio(ship_state="UNDERWAY", target_speed_kn="12.00", effective_speed_kn="12.00", fuel_milli=100_000)
    assert "NOOP_SPEED" in vetos(prop("navigation", vel(12)), n)


def test_telemetria_velha_veta():
    assert "STALE_TELEMETRY" in vetos(prop("navigation", vel(12)), navio(state_age_ticks=3))


def test_cota_e_caixa_abatida_no_lote():
    n = navio()
    preco = politicas.PRECO_TETO_CENT_T
    g = Gateway()
    r = g.avaliar_lote([prop("economy", buy(10)), prop("economy", buy(10))], {"S2-MARE": n},
                       ctx(caixa_livre_cent=10 * preco))
    assert r[0].aprovada and "QUOTA_EXCEEDED" in r[1].vetos


def test_idempotencia_entre_chamadas():
    g, n, c = Gateway(), navio(), ctx()
    assert g.avaliar_lote([prop("economy", buy(10))], {"S2-MARE": n}, c)[0].aprovada
    assert "DUPLICATE_IDEMPOTENCY" in g.avaliar_lote([prop("economy", buy(10))], {"S2-MARE": n}, c)[0].vetos


def test_navio_nao_atribuido():
    r = Gateway().avaliar_lote([prop("economy", buy(10), logico="S9-X")], {"S2-MARE": navio()}, ctx())
    assert r[0].vetos == ("SHIP_NOT_ASSIGNED",)
