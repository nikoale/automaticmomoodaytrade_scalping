"""為替: 口座 → Yahoo (保存した値) → 設定値 の順。"""
import json
import time

import pytest

from swing import fx
from swing.account import effective_capital
from swing_helpers import cfg as make_cfg


@pytest.fixture
def c(tmp_path):
    return make_cfg(data={"dir": str(tmp_path)})


def test_order_account_then_yahoo_then_config(c):
    assert fx.current(c) == (150.0, fx.current(c)[1]) and "設定値" in fx.current(c)[1]
    assert fx.refresh(c, fetch=lambda: 143.21)["rate"] == 143.21
    rate, src = fx.current(c)
    assert rate == 143.21 and "Yahoo" in src
    assert fx.current(c, 1.0)[0] == 143.21                    # 模擬口座の 1 は使わない
    assert fx.current(c, 147.5) == (147.5, "口座 (moomoo の換算レート)")
    cap = effective_capital(c, 1e6, 1.0)
    assert cap["usd"] == pytest.approx(200000 / 143.21) and "Yahoo" in cap["fx_source"]


def test_bad_or_stale_values_are_ignored(c):
    assert fx.refresh(c, fetch=lambda: 1.0) is None            # 範囲外
    assert fx.refresh(c, fetch=lambda: (_ for _ in ()).throw(OSError("offline"))) is None
    assert "設定値" in fx.current(c)[1]
    fx.refresh(c, fetch=lambda: 151.0)
    p = c.path("fx.json")
    rec = json.loads(p.read_text())
    rec["time"] = time.time() - 25 * 3600                       # 24 時間より古い
    p.write_text(json.dumps(rec))
    assert fx.current(c)[0] == 150.0
