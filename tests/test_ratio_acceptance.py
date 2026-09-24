"""双指标配比验收模块测试：配置校验、包内配组、无效原因与整体结论。"""

import itertools

from fastapi.testclient import TestClient
import pytest

from app.main import app

# 数据库文件在测试进程内共享，逻辑设备名 acc 按用例加唯一后缀隔离
_suffix = {"v": 0}
_counter = itertools.count()


@pytest.fixture(autouse=True)
def _unique_device_suffix():
    _suffix["v"] = next(_counter)


def _dev(name="acc"):
    return f"{name}-{_suffix['v']}"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def make_config(
    client,
    device=None,
    numerator_metric="num",
    denominator_metric="den",
    numerator_unit="uN",
    denominator_unit="uD",
    ratio_lower_limit=0.9,
    ratio_upper_limit=1.1,
    min_valid_groups=1,
):
    device = device or _dev()
    r = client.post("/api/v1/ratio-acceptance/configs", json={
        "device_id": device,
        "numerator_metric": numerator_metric,
        "denominator_metric": denominator_metric,
        "numerator_unit": numerator_unit,
        "denominator_unit": denominator_unit,
        "ratio_lower_limit": ratio_lower_limit,
        "ratio_upper_limit": ratio_upper_limit,
        "min_valid_groups": min_valid_groups,
    })
    assert r.status_code == 201, r.text
    return device, r.json()


def post_pkg(client, device, seq, measurements, key=None):
    return client.post("/api/v1/packages", json={
        "device_id": device,
        "seq": seq,
        "idempotency_key": key or f"{device}-k{seq}",
        "measurements": measurements,
    })


def n(value, unit="uN", **kw):
    return {"metric": "num", "value": value, "unit": unit, **kw}


def d(value, unit="uD", **kw):
    return {"metric": "den", "value": value, "unit": unit, **kw}


def evaluate(client, device, from_seq=1, to_seq=None, config_id=None):
    params = {"from_seq": from_seq, "to_seq": to_seq if to_seq is not None else from_seq}
    if config_id is not None:
        params["config_id"] = config_id
    return client.get(
        f"/api/v1/devices/{device}/ratio-acceptance/evaluate", params=params
    )


# ---------------------------------------------------------------------------
# 配置保存与校验
# ---------------------------------------------------------------------------

def test_create_config_persists_all_fields(client):
    device, cfg = make_config(
        client, ratio_lower_limit=0.5, ratio_upper_limit=2.0, min_valid_groups=3
    )
    assert cfg["device_id"] == device
    assert cfg["numerator_metric"] == "num"
    assert cfg["denominator_metric"] == "den"
    assert cfg["numerator_unit"] == "uN"
    assert cfg["denominator_unit"] == "uD"
    assert cfg["ratio_lower_limit"] == 0.5
    assert cfg["ratio_upper_limit"] == 2.0
    assert cfg["min_valid_groups"] == 3
    assert cfg["id"] >= 1 and cfg["created_at"]

    # 列表与详情接口可读回
    listing = client.get(
        "/api/v1/ratio-acceptance/configs", params={"device_id": device}
    ).json()
    assert listing["count"] == 1 and listing["configs"][0]["id"] == cfg["id"]
    detail = client.get(f"/api/v1/ratio-acceptance/configs/{cfg['id']}")
    assert detail.status_code == 200 and detail.json()["id"] == cfg["id"]


def test_config_rejects_same_metric(client):
    r = client.post("/api/v1/ratio-acceptance/configs", json={
        "device_id": _dev(),
        "numerator_metric": "x", "denominator_metric": "x",
        "numerator_unit": "u", "denominator_unit": "u",
        "ratio_lower_limit": 1, "ratio_upper_limit": 1,
    })
    assert r.status_code == 422
    assert "指标" in str(r.json()["detail"])


def test_config_rejects_lower_greater_than_upper(client):
    r = client.post("/api/v1/ratio-acceptance/configs", json={
        "device_id": _dev(),
        "numerator_metric": "x", "denominator_metric": "y",
        "numerator_unit": "u", "denominator_unit": "u",
        "ratio_lower_limit": 1.2, "ratio_upper_limit": 1.1,
    })
    assert r.status_code == 422


def test_config_boundary_lower_equals_upper_ok(client):
    _, cfg = make_config(client, ratio_lower_limit=1.0, ratio_upper_limit=1.0)
    assert cfg["ratio_lower_limit"] == cfg["ratio_upper_limit"] == 1.0


def test_config_rejects_min_valid_groups_below_one(client):
    r = client.post("/api/v1/ratio-acceptance/configs", json={
        "device_id": _dev(),
        "numerator_metric": "x", "denominator_metric": "y",
        "numerator_unit": "u", "denominator_unit": "u",
        "ratio_lower_limit": 0, "ratio_upper_limit": 1, "min_valid_groups": 0,
    })
    assert r.status_code == 422


def test_get_unknown_config_404(client):
    r = client.get("/api/v1/ratio-acceptance/configs/999999")
    assert r.status_code == 404


def test_multiple_configs_require_config_id(client):
    device, cfg1 = make_config(client, numerator_metric="num")
    cfg2 = client.post("/api/v1/ratio-acceptance/configs", json={
        "device_id": device,
        "numerator_metric": "other_num", "denominator_metric": "other_den",
        "numerator_unit": "uA", "denominator_unit": "uB",
        "ratio_lower_limit": 1, "ratio_upper_limit": 2,
    }).json()
    post_pkg(client, device, 1, [n(1.0), d(1.0)])
    # 未指定 config_id：多套配置 -> 422
    r = evaluate(client, device, 1, 1)
    assert r.status_code == 422
    assert "config_id" in r.json()["detail"]
    # 指定后两套均可执行
    assert evaluate(client, device, 1, 1, config_id=cfg1["id"]).status_code == 200
    body = evaluate(client, device, 1, 1, config_id=cfg2["id"]).json()
    # 第二套方案的两种指标在包中均不存在 -> 无读数可配，零组
    assert body["valid_group_count"] == 0
    assert body["groups"] == []
    assert body["accepted"] is False


def test_config_id_of_other_device_404(client):
    device1, cfg1 = make_config(client)
    device2 = _dev("other")
    post_pkg(client, device2, 1, [n(1.0), d(1.0)])
    r = evaluate(client, device2, 1, 1, config_id=cfg1["id"])
    assert r.status_code == 404


def test_evaluate_unknown_device_404(client):
    make_config(client)
    r = client.get(
        f"/api/v1/devices/{_dev('ghost')}/ratio-acceptance/evaluate",
        params={"from_seq": 1, "to_seq": 1},
    )
    assert r.status_code == 404


def test_evaluate_without_config_404(client):
    device = _dev("nocfg")
    post_pkg(client, device, 1, [n(1.0), d(1.0)])
    r = evaluate(client, device, 1, 1)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 基本验收：比例、闭区间边界、整体结论
# ---------------------------------------------------------------------------

def test_basic_pairing_ratio_and_acceptance(client):
    device, _ = make_config(client)
    post_pkg(client, device, 1, [n(1.0), d(1.0)])  # ratio 1.0
    body = evaluate(client, device, 1, 1).json()
    assert body["accepted"] is True
    assert body["scanned_package_count"] == 1
    assert body["valid_group_count"] == 1
    assert body["invalid_group_count"] == 0
    assert body["passed_group_count"] == 1
    g = body["groups"][0]
    assert g["group_index"] == 1
    assert g["seq"] == 1
    assert g["numerator_position"] == 0
    assert g["denominator_position"] == 1
    assert g["ratio"] == pytest.approx(1.0)
    assert g["valid"] is True and g["passed"] is True
    assert g["reason_code"] is None and g["reason"] is None


def test_closed_interval_boundaries_pass(client):
    device, _ = make_config(
        client, ratio_lower_limit=0.5, ratio_upper_limit=2.0
    )
    # 三个包：比例恰为下限、中间、恰为上限 -> 全部合格（闭区间）
    post_pkg(client, device, 1, [n(0.5), d(1.0)])
    post_pkg(client, device, 2, [n(1.5), d(1.0)])
    post_pkg(client, device, 3, [n(2.0), d(1.0)])
    body = evaluate(client, device, 1, 3).json()
    assert body["valid_group_count"] == 3
    assert all(g["passed"] for g in body["groups"])
    assert body["accepted"] is True
    assert [g["group_index"] for g in body["groups"]] == [1, 2, 3]
    assert [g["seq"] for g in body["groups"]] == [1, 2, 3]


def test_out_of_interval_fails_overall(client):
    device, _ = make_config(
        client, ratio_lower_limit=0.9, ratio_upper_limit=1.1, min_valid_groups=2
    )
    post_pkg(client, device, 1, [n(1.0), d(1.0)])    # 1.0 合格
    post_pkg(client, device, 2, [n(1.2), d(1.0)])    # 1.2 超上限，不合格
    body = evaluate(client, device, 1, 2).json()
    assert body["valid_group_count"] == 2
    assert body["passed_group_count"] == 1
    assert body["accepted"] is False
    assert body["groups"][1]["passed"] is False
    # 有效但不合格的组仍是有效组：ratio 有值、reason 为空
    assert body["groups"][1]["valid"] is True
    assert body["groups"][1]["ratio"] == pytest.approx(1.2)


def test_min_valid_groups_not_reached(client):
    device, _ = make_config(client, min_valid_groups=2)
    post_pkg(client, device, 1, [n(1.0), d(0.0)])    # 分母为零：无效
    body = evaluate(client, device, 1, 1).json()
    assert body["valid_group_count"] == 0
    assert body["invalid_group_count"] == 1
    assert body["accepted"] is False


def test_min_valid_groups_met_but_invalid_groups_present_still_accepts(client):
    # 整体只要求"达到最低有效组数且所有有效组合格"，无效组本身不否决，
    # 但最低有效组数必须满足
    device, _ = make_config(client, min_valid_groups=2)
    post_pkg(client, device, 1, [
        n(1.0), d(1.0),       # 组1：有效合格
        n(2.0),               # 组2：缺分母 -> 无效
    ])
    post_pkg(client, device, 2, [n(1.0), d(1.0)])    # 组3：有效合格
    body = evaluate(client, device, 1, 2).json()
    assert body["valid_group_count"] == 2
    assert body["invalid_group_count"] == 1
    assert body["passed_group_count"] == 2
    assert body["accepted"] is True


# ---------------------------------------------------------------------------
# 包内一对一配组：按出现顺序、禁止跨包、多余读数成无效组
# ---------------------------------------------------------------------------

def test_within_package_pairing_by_occurrence_order(client):
    device, _ = make_config(
        client, ratio_lower_limit=0.0, ratio_upper_limit=100.0
    )
    post_pkg(client, device, 1, [
        n(10.0), d(2.0),   # 组1：5.0
        n(30.0), d(3.0),   # 组2：10.0
    ])
    body = evaluate(client, device, 1, 1).json()
    assert [g["ratio"] for g in body["groups"]] == [5.0, 10.0]
    assert [(g["numerator_position"], g["denominator_position"])
            for g in body["groups"]] == [(0, 1), (2, 3)]


def test_extra_numerator_is_missing_denominator_group(client):
    device, _ = make_config(
        client, ratio_lower_limit=0.0, ratio_upper_limit=100.0
    )
    post_pkg(client, device, 1, [
        n(1.0), d(1.0),
        n(2.0),  # 多出的分子：缺对应分母（不去下一个包找补）
    ])
    post_pkg(client, device, 2, [
        d(9.0),  # 下一个包的分母不能与上包多余分子配组，只与本包分子配
        n(1.0), d(1.0),
    ])
    body = evaluate(client, device, 1, 2).json()
    # 包1：组1(n1,d1)有效、组2(n2,缺分母)无效
    # 包2：组3(n1,d9)有效（按各自出现顺序，与下标交错无关）、组4(缺分子,d1)无效
    assert len(body["groups"]) == 4
    g1, g2, g3, g4 = body["groups"]
    assert g1["valid"] is True and g1["ratio"] == 1.0
    assert g2["seq"] == 1
    assert g2["valid"] is False
    assert g2["reason_code"] == "missing_denominator"
    assert g2["numerator_value"] == 2.0 and g2["denominator_value"] is None
    assert g3["seq"] == 2 and g3["valid"] is True
    assert g3["numerator_position"] == 1 and g3["denominator_position"] == 0
    assert g3["ratio"] == pytest.approx(1.0 / 9.0)
    assert g4["seq"] == 2
    assert g4["valid"] is False
    assert g4["reason_code"] == "missing_numerator"
    assert g4["denominator_value"] == 1.0 and g4["numerator_value"] is None
    assert body["valid_group_count"] == 2


def test_no_cross_package_pairing_with_interleaved_metrics(client):
    device, _ = make_config(client, ratio_lower_limit=0.0, ratio_upper_limit=100.0)
    # 包1 只有分子；包2 只有分母 —— 禁止跨包，两组都无效
    post_pkg(client, device, 1, [n(4.0)])
    post_pkg(client, device, 2, [d(2.0)])
    body = evaluate(client, device, 1, 2).json()
    assert body["valid_group_count"] == 0
    assert body["groups"][0]["reason_code"] == "missing_denominator"
    assert body["groups"][1]["reason_code"] == "missing_numerator"


def test_positions_are_indices_in_full_measurements_list(client):
    device, _ = make_config(client)
    post_pkg(client, device, 1, [
        {"metric": "other", "value": 0.0},
        n(1.0),
        {"metric": "another", "value": 0.0},
        d(1.0),
    ])
    g = evaluate(client, device, 1, 1).json()["groups"][0]
    assert g["numerator_position"] == 1
    assert g["denominator_position"] == 3


def test_empty_range_returns_zero_groups_not_accepted(client):
    device, _ = make_config(client, min_valid_groups=1)
    post_pkg(client, device, 5, [n(1.0), d(1.0)])
    body = evaluate(client, device, 1, 4).json()
    assert body["scanned_package_count"] == 0
    assert body["groups"] == []
    assert body["valid_group_count"] == 0
    assert body["accepted"] is False
    assert body["absent_seqs"] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# 无效原因：单位不符 / 分母为零（含优先级）
# ---------------------------------------------------------------------------

def test_numerator_unit_mismatch(client):
    device, _ = make_config(client)
    post_pkg(client, device, 1, [n(1.0, unit="BAD"), d(1.0)])
    g = evaluate(client, device, 1, 1).json()["groups"][0]
    assert g["valid"] is False and g["passed"] is False
    assert g["reason_code"] == "numerator_unit_mismatch"
    assert g["ratio"] is None
    assert "uN" in g["reason"] and "BAD" in g["reason"]


def test_denominator_unit_mismatch(client):
    device, _ = make_config(client)
    post_pkg(client, device, 1, [n(1.0), d(1.0, unit="BAD")])
    g = evaluate(client, device, 1, 1).json()["groups"][0]
    assert g["reason_code"] == "denominator_unit_mismatch"
    assert g["valid"] is False


def test_missing_unit_on_reading_is_mismatch(client):
    device, _ = make_config(client)
    post_pkg(client, device, 1, [
        {"metric": "num", "value": 1.0},   # 缺单位
        d(1.0),
    ])
    g = evaluate(client, device, 1, 1).json()["groups"][0]
    assert g["reason_code"] == "numerator_unit_mismatch"


def test_denominator_zero_invalid(client):
    device, _ = make_config(client)
    post_pkg(client, device, 1, [n(1.0), d(0.0)])
    g = evaluate(client, device, 1, 1).json()["groups"][0]
    assert g["valid"] is False
    assert g["reason_code"] == "denominator_zero"
    assert g["ratio"] is None
    assert g["numerator_value"] == 1.0 and g["denominator_value"] == 0.0


def test_unit_check_precedes_denominator_zero(client):
    # 单位不符且分母为零：按优先级先报单位不符
    device, _ = make_config(client)
    post_pkg(client, device, 1, [n(1.0), d(0.0, unit="BAD")])
    g = evaluate(client, device, 1, 1).json()["groups"][0]
    assert g["reason_code"] == "denominator_unit_mismatch"


def test_negative_zero_denominator_invalid(client):
    device, _ = make_config(client)
    post_pkg(client, device, 1, [n(1.0), d(-0.0)])
    g = evaluate(client, device, 1, 1).json()["groups"][0]
    assert g["reason_code"] == "denominator_zero"


# ---------------------------------------------------------------------------
# 仅读已提交包：缓冲包与作废序号不参与
# ---------------------------------------------------------------------------

def test_buffered_packages_do_not_participate(client):
    device, _ = make_config(client)
    post_pkg(client, device, 1, [n(1.0), d(1.0)])
    # seq 3 先到 -> 缓冲
    post_pkg(client, device, 3, [n(2.0), d(1.0)])
    body = evaluate(client, device, 1, 3).json()
    assert body["scanned_package_count"] == 1
    assert body["valid_group_count"] == 1
    assert body["absent_seqs"] == [2, 3]
    # 补齐 seq 2 后排水：seq 3 随之提交并参与
    post_pkg(client, device, 2, [n(1.0), d(1.0)])
    body = evaluate(client, device, 1, 3).json()
    assert body["scanned_package_count"] == 3
    assert body["absent_seqs"] == []
    assert body["valid_group_count"] == 3


def test_voided_seqs_do_not_participate(client):
    device, _ = make_config(
        client, ratio_lower_limit=0.0, ratio_upper_limit=100.0
    )
    post_pkg(client, device, 1, [n(1.0), d(1.0)])
    # seq 3 先到 -> 缓冲；作废 2（段尾不超过缓冲区最大序号）
    post_pkg(client, device, 3, [n(3.0), d(1.0)])
    r = client.post(f"/api/v1/devices/{device}/voids", json={
        "from_seq": 2, "to_seq": 2, "idempotency_key": f"{device}-void2"
    })
    assert r.status_code == 200
    body = evaluate(client, device, 1, 3).json()
    # 作废序号 2 永久缺席；1、3 参与
    assert body["absent_seqs"] == [2]
    assert body["scanned_package_count"] == 2
    assert [g["seq"] for g in body["groups"]] == [1, 3]
    assert body["accepted"] is True


def test_closed_interval_seq_filtering(client):
    device, _ = make_config(
        client, ratio_lower_limit=0.0, ratio_upper_limit=100.0
    )
    for seq in range(1, 5):
        post_pkg(client, device, seq, [n(float(seq)), d(1.0)])
    body = evaluate(client, device, 2, 3).json()
    assert body["from_seq"] == 2 and body["to_seq"] == 3
    assert [g["seq"] for g in body["groups"]] == [2, 3]
    assert body["absent_seqs"] == []


# ---------------------------------------------------------------------------
# 参数校验 / 只读
# ---------------------------------------------------------------------------

def test_invalid_seq_range_rejected(client):
    device, _ = make_config(client)
    post_pkg(client, device, 1, [n(1.0), d(1.0)])
    r = evaluate(client, device, 2, 1)
    assert r.status_code == 422
    for bad in (0, -1):
        r = client.get(
            f"/api/v1/devices/{device}/ratio-acceptance/evaluate",
            params={"from_seq": bad, "to_seq": 1},
        )
        assert r.status_code == 422


def test_missing_required_params_rejected(client):
    # 缺少必填查询参数时 FastAPI 在进入处理函数前即返回 422
    base = f"/api/v1/devices/{_dev()}/ratio-acceptance/evaluate"
    assert client.get(base, params={"to_seq": 1}).status_code == 422
    assert client.get(base, params={"from_seq": 1}).status_code == 422


def test_evaluate_is_read_only_and_repeatable(client):
    device, _ = make_config(client)
    post_pkg(client, device, 1, [
        n(1.0), d(1.0),
        n(1.0), d(0.0),  # 无效组
    ])
    body1 = evaluate(client, device, 1, 1).json()
    body2 = evaluate(client, device, 1, 1).json()
    assert body1 == body2
    # 原始数据未受影响
    assert client.get(f"/api/v1/devices/{device}/committed").json()["seqs"] == [1]
    assert client.get(f"/api/v1/devices/{device}/gaps").json()["gaps"] == []


def test_config_list_without_device_filter(client):
    _, cfg = make_config(client)
    body = client.get("/api/v1/ratio-acceptance/configs").json()
    assert body["count"] >= 1
    assert any(c["id"] == cfg["id"] for c in body["configs"])
