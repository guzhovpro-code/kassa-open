"""Ревью 15.10.2026 (Раф P2-12): правдоподобие курсов и приоритет более свежего источника."""
import copy
from datetime import timedelta

import pytest

from kassa_api import rates
from test_stage5 import CBR, ECB, NOW, Clock, Net


def store(tmp_path, net=None, clock=None, alarms=None):
    return rates.RateStore(tmp_path / "rates-cache.json", fetch=net or Net(), clock=clock or Clock(),
                           log=lambda m: None, alarm=(lambda kind, text: alarms.append((kind, text))) if alarms is not None
                           else None)


def test_jump_over_thirty_percent_is_not_applied_and_alarms(tmp_path):
    net, clock, alarms = Net(), Clock(), []
    s = store(tmp_path, net, clock, alarms)
    s.refresh_if_stale()
    assert s.usable()["TRY"] == 40.0
    bad = copy.deepcopy(ECB)
    bad["rates"]["TRY"] = 0.0004                         # ошибочный отсчёт
    bad["rates"]["EUR"] = 0.95                           # обычное колебание (5%) проходит
    net.answers[rates.ECB_URL] = bad
    clock.t = NOW + timedelta(hours=13)
    out = s.refresh_if_stale()
    assert out["ecb"] is True
    usable = s.usable()
    assert usable.get("TRY") != 0.0004 and usable["EUR"] == 0.95
    assert "TRY" not in s._load()["ecb"]["rates"]        # в кэш не попал
    assert [a[0] for a in alarms] == ["rate:ecb:TRY"] and "30%" in alarms[0][1]
    # P2-3 (ревью 15.10, второй раунд): эталон - последний принятый курс, а не «прошлый кэш». Повтор того же ошибочного
    # значения на следующем обновлении НЕ применяется, тревога звучит снова (в проде раз в сутки по ключу).
    clock.t = NOW + timedelta(hours=26)
    s.refresh_if_stale()
    assert "TRY" not in s.usable()
    assert [a[0] for a in alarms] == ["rate:ecb:TRY", "rate:ecb:TRY"]
    clock.t = NOW + timedelta(hours=40)
    s.refresh_if_stale()
    assert "TRY" not in s.usable() and s._load()["ecb"]["ref"]["TRY"][0] == 40.0


def test_real_shift_is_applied_only_when_second_source_confirms(tmp_path):
    net, clock, alarms = Net(), Clock(), []
    cbr = copy.deepcopy(CBR)
    cbr["Valute"]["TRY"] = {"Nominal": 1, "Value": 2.0}               # 80 / 2 = 40 лир за доллар
    net.answers[rates.CBR_URL] = cbr
    s = store(tmp_path, net, clock, alarms)
    s.refresh_if_stale()
    assert s.usable()["TRY"] == 40.0
    shifted = copy.deepcopy(ECB)
    shifted["rates"]["TRY"] = 80.0                                    # девальвация вдвое: только ECB
    net.answers[rates.ECB_URL] = shifted
    clock.t = NOW + timedelta(hours=13)
    s.refresh_if_stale()
    assert s.usable()["TRY"] == 40.0 and [a[0] for a in alarms] == ["rate:ecb:TRY"]
    # ЦБ РФ в том же обновлении называет то же значение -> сдвиг подтверждён обоими
    cbr2 = copy.deepcopy(CBR)
    cbr2["Valute"]["TRY"] = {"Nominal": 1, "Value": 1.0}              # 80 / 1 = 80
    net.answers[rates.CBR_URL] = cbr2
    clock.t = NOW + timedelta(hours=26)
    s.refresh_if_stale()
    assert s.usable()["TRY"] == 80.0
    assert s._load()["ecb"]["ref"]["TRY"][0] == 80.0


def test_first_start_without_baseline_rejects_code_where_sources_disagree(tmp_path):
    net, alarms = Net(), []
    cbr = copy.deepcopy(CBR)
    cbr["Valute"]["TRY"] = {"Nominal": 1, "Value": 0.5}               # 160 лир: ECB говорит 40
    net.answers[rates.CBR_URL] = cbr
    s = store(tmp_path, net, None, alarms)
    s.refresh_if_stale()
    assert "TRY" not in s.usable() and sorted(a[0] for a in alarms) == ["rate:cbr:TRY", "rate:ecb:TRY"]
    assert s.usable()["EUR"] == 0.9                                   # остальные коды не пострадали


def test_small_moves_pass_and_first_load_has_no_baseline(tmp_path):
    net, clock, alarms = Net(), Clock(), []
    s = store(tmp_path, net, clock, alarms)
    s.refresh_if_stale()
    moved = copy.deepcopy(ECB)
    moved["rates"]["TRY"] = 40.0 * 1.25                  # +25%: ещё допустимо
    net.answers[rates.ECB_URL] = moved
    clock.t = NOW + timedelta(hours=13)
    s.refresh_if_stale()
    assert s.usable()["TRY"] == 50.0 and alarms == []


def test_old_baseline_is_ignored(tmp_path):
    net, clock, alarms = Net(), Clock(), []
    s = store(tmp_path, net, clock, alarms)
    s.refresh_if_stale()
    far = copy.deepcopy(ECB)
    far["rates"]["TRY"] = 400.0
    net.answers[rates.ECB_URL] = far
    clock.t = NOW + timedelta(days=15)                   # прошлому курсу больше двух недель: сравнивать не с чем
    s.refresh_if_stale()
    assert s._load()["ecb"]["rates"]["TRY"] == 400.0 and alarms == []


def test_fresher_source_wins_where_both_have_the_currency(tmp_path):
    net, clock = Net(), Clock()
    s = store(tmp_path, net, clock)
    s.refresh_if_stale()
    assert s.usable()["EUR"] == 0.9                      # одинаково свежие: приоритет у ECB
    data = s._load()
    data["ecb"]["at"] = (NOW - timedelta(days=2)).strftime(rates.ISO)       # ECB постарел, ЦБ РФ свежее
    assert s.usable()["EUR"] == pytest.approx(80.0 / 88.0)
