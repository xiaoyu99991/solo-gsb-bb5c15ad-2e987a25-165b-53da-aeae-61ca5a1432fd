"""双指标配比验收：在已提交包内把两种指标的读数按出现顺序一对一配组并判定。

配置（ratio_acceptance_configs 表，按设备维护）：
- 分子指标 / 分母指标及其各自单位、比例闭区间上下限、最低有效组数；
- 分子指标必须不同于分母指标，下限不得大于上限（Pydantic 与 DB CHECK 双保险）。

验收评估（纯只读，仅读取 committed 表）：
- 按设备 + 序号闭区间 [from_seq, to_seq] 取已提交包，按 seq 升序处理；
- 每个包内分别抽取两种指标的读数（保留其在 measurements 中的原始下标），
  按各自出现顺序一对一配组，**绝不跨包配组**；某一侧条数不足时，多出的
  另一侧读数仍各成一组（该组因缺对应读数而无效），但不会去别的包找补；
- 无效原因（按判定优先级，一组只记一个）：
  缺分子读数 / 缺分母读数 / 分子单位不符 / 分母单位不符 / 分母为零；
- 有效组比例 = 分子读数 / 分母读数，落在闭区间 [下限, 上限] 内才算合格
  （边界相等算合格）；
- 整体验收通过当且仅当有效组数达到最低有效组数且所有有效组合格。

缓冲包不在 committed 中，作废序号也永久不在 committed 中，二者天然不参与
计算；闭区间内没有已提交包的序号（未到达 / 仍在缓冲 / 已作废）在
absent_seqs 中回报。
"""

import json
import sqlite3
from typing import List, NamedTuple, Optional

from .schemas import (
    RatioAcceptanceConfigOut,
    RatioAcceptanceGroup,
    RatioAcceptanceResponse,
)

# 无效原因代码 -> 中文说明
REASON_MISSING_NUMERATOR = "missing_numerator"
REASON_MISSING_DENOMINATOR = "missing_denominator"
REASON_NUMERATOR_UNIT_MISMATCH = "numerator_unit_mismatch"
REASON_DENOMINATOR_UNIT_MISMATCH = "denominator_unit_mismatch"
REASON_DENOMINATOR_ZERO = "denominator_zero"

_REASON_TEXT = {
    REASON_MISSING_NUMERATOR: "缺失对应分子读数",
    REASON_MISSING_DENOMINATOR: "缺失对应分母读数",
    REASON_NUMERATOR_UNIT_MISMATCH: "分子读数单位与配置不符",
    REASON_DENOMINATOR_UNIT_MISMATCH: "分母读数单位与配置不符",
    REASON_DENOMINATOR_ZERO: "分母读数为零，无法计算比例",
}


class _Reading(NamedTuple):
    """包内某一指标的一次读数及其在 measurements 中的原始下标。"""

    position: int
    value: float
    unit: Optional[str]


class RatioAcceptanceError(ValueError):
    """验收参数非法（序号区间倒置、设备有多个配置却未指定 config_id 等）。"""


class RatioConfigNotFound(LookupError):
    """验收配置不存在，或配置不属于所查询的设备。"""


def create_acceptance_config(
    conn: sqlite3.Connection,
    device_id: str,
    numerator_metric: str,
    denominator_metric: str,
    numerator_unit: str,
    denominator_unit: str,
    ratio_lower_limit: float,
    ratio_upper_limit: float,
    min_valid_groups: int,
) -> int:
    """创建一条双指标配比验收配置，返回新配置 id。

    与其它写路径一样由调用方持有 ingest_lock；指标相同 / 下限大于上限
    抛 RatioAcceptanceError（路由转 422），并由表级 CHECK 兜底。
    """
    if numerator_metric == denominator_metric:
        raise RatioAcceptanceError("分子指标与分母指标必须不同")
    if ratio_lower_limit > ratio_upper_limit:
        raise RatioAcceptanceError("比例下限不能大于比例上限")
    if min_valid_groups < 1:
        raise RatioAcceptanceError("最低有效组数至少为 1")
    cur = conn.execute(
        """
        INSERT INTO ratio_acceptance_configs
            (device_id, numerator_metric, denominator_metric,
             numerator_unit, denominator_unit,
             ratio_lower_limit, ratio_upper_limit, min_valid_groups)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            device_id,
            numerator_metric,
            denominator_metric,
            numerator_unit,
            denominator_unit,
            ratio_lower_limit,
            ratio_upper_limit,
            min_valid_groups,
        ),
    )
    return cur.lastrowid


def list_acceptance_configs(
    conn: sqlite3.Connection, device_id: Optional[str] = None
) -> List[sqlite3.Row]:
    """列出验收配置（可按设备过滤），按 id 升序。"""
    if device_id is not None:
        return conn.execute(
            "SELECT * FROM ratio_acceptance_configs WHERE device_id = ? ORDER BY id",
            (device_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM ratio_acceptance_configs ORDER BY id"
    ).fetchall()


def get_acceptance_config(
    conn: sqlite3.Connection, config_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ratio_acceptance_configs WHERE id = ?", (config_id,)
    ).fetchone()


def _resolve_config(
    conn: sqlite3.Connection, device_id: str, config_id: Optional[int]
) -> sqlite3.Row:
    """确定本次验收使用的配置行。

    - 显式给 config_id：必须存在且属于该设备，否则 RatioConfigNotFound；
    - 未给 config_id：该设备恰有一条配置时自动选用；无配置 -> 404，
      多条配置（无法判断用哪套指标 / 区间）-> RatioAcceptanceError(422)。
    """
    if config_id is not None:
        row = get_acceptance_config(conn, config_id)
        if row is None or row["device_id"] != device_id:
            raise RatioConfigNotFound(
                f"验收配置不存在或不属于设备 {device_id}: config_id={config_id}"
            )
        return row
    rows = list_acceptance_configs(conn, device_id)
    if not rows:
        raise RatioConfigNotFound(
            f"设备 {device_id} 尚未配置双指标配比验收方案，请先创建配置"
        )
    if len(rows) > 1:
        raise RatioAcceptanceError(
            f"设备 {device_id} 配置了 {len(rows)} 套验收方案（id="
            f"{[r['id'] for r in rows]}），请用 config_id 指定要执行的方案"
        )
    return rows[0]


def _extract_readings(payload: dict, metric: str) -> List[_Reading]:
    """抽取包内某指标的全部读数，按其在 measurements 中出现的顺序排列。"""
    readings: List[_Reading] = []
    for position, measurement in enumerate(payload.get("measurements", [])):
        if measurement.get("metric") == metric:
            readings.append(
                _Reading(
                    position=position,
                    value=float(measurement["value"]),
                    unit=measurement.get("unit"),
                )
            )
    return readings


def evaluate_acceptance(
    conn: sqlite3.Connection,
    device_id: str,
    from_seq: int,
    to_seq: int,
    config_id: Optional[int] = None,
) -> RatioAcceptanceResponse:
    """在 [from_seq, to_seq] 闭区间内执行双指标配比验收（只读）。

    调用方需保证设备存在；配置缺失抛 RatioConfigNotFound，参数非法抛
    RatioAcceptanceError。
    """
    if from_seq < 1 or to_seq < 1:
        raise RatioAcceptanceError("序号必须为正整数")
    if from_seq > to_seq:
        raise RatioAcceptanceError("from_seq 不能大于 to_seq")

    cfg = _resolve_config(conn, device_id, config_id)
    numerator_metric = cfg["numerator_metric"]
    denominator_metric = cfg["denominator_metric"]
    numerator_unit = cfg["numerator_unit"]
    denominator_unit = cfg["denominator_unit"]
    lower = cfg["ratio_lower_limit"]
    upper = cfg["ratio_upper_limit"]
    min_valid_groups = cfg["min_valid_groups"]

    rows = conn.execute(
        """
        SELECT seq, payload_json FROM committed
         WHERE device_id = ? AND seq >= ? AND seq <= ?
         ORDER BY seq
        """,
        (device_id, from_seq, to_seq),
    ).fetchall()

    groups: List[RatioAcceptanceGroup] = []
    present_seqs = set()
    group_index = 0
    for row in rows:
        seq = row["seq"]
        present_seqs.add(seq)
        payload = json.loads(row["payload_json"])
        numerators = _extract_readings(payload, numerator_metric)
        denominators = _extract_readings(payload, denominator_metric)

        # 包内按各自出现顺序一对一配组；一侧条数不足时多出的另一侧读数
        # 仍成组（缺对应读数无效），配组严格限定在本包内。
        pair_count = max(len(numerators), len(denominators))
        for k in range(pair_count):
            group_index += 1
            num = numerators[k] if k < len(numerators) else None
            den = denominators[k] if k < len(denominators) else None

            kwargs = dict(
                group_index=group_index,
                seq=seq,
                numerator_position=num.position if num is not None else None,
                denominator_position=den.position if den is not None else None,
                numerator_value=num.value if num is not None else None,
                denominator_value=den.value if den is not None else None,
            )

            if num is None:
                groups.append(
                    RatioAcceptanceGroup(
                        **kwargs,
                        valid=False,
                        reason_code=REASON_MISSING_NUMERATOR,
                        reason=_REASON_TEXT[REASON_MISSING_NUMERATOR],
                        ratio=None,
                        passed=False,
                    )
                )
                continue
            if den is None:
                groups.append(
                    RatioAcceptanceGroup(
                        **kwargs,
                        valid=False,
                        reason_code=REASON_MISSING_DENOMINATOR,
                        reason=_REASON_TEXT[REASON_MISSING_DENOMINATOR],
                        ratio=None,
                        passed=False,
                    )
                )
                continue
            if num.unit != numerator_unit:
                # 读数缺单位（None）同样视为与配置单位不符
                groups.append(
                    RatioAcceptanceGroup(
                        **kwargs,
                        valid=False,
                        reason_code=REASON_NUMERATOR_UNIT_MISMATCH,
                        reason=(
                            f"{_REASON_TEXT[REASON_NUMERATOR_UNIT_MISMATCH]}："
                            f"读数单位 {num.unit!r}，要求 {numerator_unit!r}"
                        ),
                        ratio=None,
                        passed=False,
                    )
                )
                continue
            if den.unit != denominator_unit:
                groups.append(
                    RatioAcceptanceGroup(
                        **kwargs,
                        valid=False,
                        reason_code=REASON_DENOMINATOR_UNIT_MISMATCH,
                        reason=(
                            f"{_REASON_TEXT[REASON_DENOMINATOR_UNIT_MISMATCH]}："
                            f"读数单位 {den.unit!r}，要求 {denominator_unit!r}"
                        ),
                        ratio=None,
                        passed=False,
                    )
                )
                continue
            if den.value == 0:
                groups.append(
                    RatioAcceptanceGroup(
                        **kwargs,
                        valid=False,
                        reason_code=REASON_DENOMINATOR_ZERO,
                        reason=_REASON_TEXT[REASON_DENOMINATOR_ZERO],
                        ratio=None,
                        passed=False,
                    )
                )
                continue

            ratio = num.value / den.value
            passed = lower <= ratio <= upper  # 闭区间：恰等于上下限也算合格
            groups.append(
                RatioAcceptanceGroup(
                    **kwargs,
                    valid=True,
                    reason_code=None,
                    reason=None,
                    ratio=ratio,
                    passed=passed,
                )
            )

    absent_seqs = [
        s for s in range(from_seq, to_seq + 1) if s not in present_seqs
    ]
    valid_groups = [g for g in groups if g.valid]
    valid_count = len(valid_groups)
    invalid_count = len(groups) - valid_count
    passed_count = sum(1 for g in valid_groups if g.passed)
    accepted = (
        valid_count >= min_valid_groups
        and passed_count == valid_count
    )

    return RatioAcceptanceResponse(
        config_id=cfg["id"],
        device_id=device_id,
        numerator_metric=numerator_metric,
        denominator_metric=denominator_metric,
        numerator_unit=numerator_unit,
        denominator_unit=denominator_unit,
        ratio_lower_limit=lower,
        ratio_upper_limit=upper,
        min_valid_groups=min_valid_groups,
        from_seq=from_seq,
        to_seq=to_seq,
        scanned_package_count=len(rows),
        valid_group_count=valid_count,
        invalid_group_count=invalid_count,
        passed_group_count=passed_count,
        accepted=accepted,
        absent_seqs=absent_seqs,
        groups=groups,
    )
