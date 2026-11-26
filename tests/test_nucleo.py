"""Testes das funcoes puras do nucleo (sem rede, sem LLM)."""
from agentes import politicas
from agentes.nucleo.gemeo import simular_horizonte
from agentes.nucleo.opcoes import opcao_por_id, tabela_economista


def navio(**kw):
    base = {"logical_id": "S2-MARE", "ship_state": "AT_ORIGIN", "progress_nautical_mile_milli": 0,
            "route_total_nm": 1500.0, "deadline_tick": 300, "fuel_milli": 40_000, "pending_fuel_milli": 0,
            "reserve_min_milli": 5_000, "fuel_capacity_milli": 120_000, "target_speed_kn": "0",
            "effective_speed_kn": "0", "active_limit_speed_kn": "18", "e04_active": False,
            "state_age_ticks": 0}
    base.update(kw)
    return base


def test_gemeo_detecta_violacao_no_meio_do_turno():
    n = navio(fuel_milli=6_000)   # 1 t acima da reserva
    r = simular_horizonte(n, 14.0, 0, 25, None)
    assert r.violou_reserva and r.tick_violacao is not None and r.tick_violacao < 24


def test_gemeo_sem_violacao_com_combustivel_folgado():
    r = simular_horizonte(navio(fuel_milli=100_000), 12.0, 0, 25, None)
    assert not r.violou_reserva and r.menor_folga_t > 0


def test_gemeo_considera_compra():
    n = navio(fuel_milli=6_000)
    assert simular_horizonte(n, 14.0, 0, 25, None).violou_reserva
    assert not simular_horizonte(n, 14.0, 0, 25, None, compra_t=40).violou_reserva


def test_tabela_sempre_tem_nada_e_multiplos_de_5():
    ops = tabela_economista(navio(), 0, None, 10**12)
    assert ops[0]["id"] == "nada"
    assert all(o["quantidade_t"] % 5 == 0 for o in ops)
    assert all(o["custo_cent"] == o["quantidade_t"] * politicas.PRECO_TETO_CENT_T for o in ops)


def test_tabela_respeita_caixa_e_tanque():
    ops = tabela_economista(navio(fuel_milli=100_000), 0, None, 10**12)
    assert max(o["quantidade_t"] for o in ops) <= 20           # espaco no tanque
    pobre = tabela_economista(navio(), 0, None, 5 * politicas.PRECO_TETO_CENT_T)
    assert max(o["quantidade_t"] for o in pobre) <= 5


def test_tabela_fora_da_janela_so_tem_nada():
    assert [o["id"] for o in tabela_economista(navio(), 20, None, 10**12)] == ["nada"]
    assert [o["id"] for o in tabela_economista(navio(ship_state="UNDERWAY"), 5, None, 10**12)] == ["nada"]


def test_id_inventado_nao_resolve():
    ops = tabela_economista(navio(), 0, None, 10**12)
    assert opcao_por_id(ops, "buy-9999") is None
