"""查詢規格編譯（SPEC 9.1 / 9.2 / 13.4）。純離線，只編譯 SQL 不連資料庫。"""
import copy
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from app.catalog import ColumnDef, DatasetDef
from app.services.query.spec import QuerySpecError, compile_query, count_query, paginate
from app.services.query.tables import build_table

D = postgresql.psycopg2.dialect()


def ds(name, cols, dataset_id=1):
    return DatasetDef(dataset_id=dataset_id, name=name, columns=tuple(cols))


POLICY = ds("policy", [
    ColumnDef("yyyymm", "integer", required=True, is_pk=True, pk_order=1),
    ColumnDef("policy_no", "varchar(20)", required=True, is_pk=True, pk_order=2),
    ColumnDef("plan", "varchar(10)"),
    ColumnDef("prem", "numeric(18,2)"),
    ColumnDef("eff_date", "date"),
    ColumnDef("old_col", "varchar(5)", disabled=True),
])
CLAIM = ds("claim", [
    ColumnDef("yyyymm", "integer", required=True, is_pk=True, pk_order=1),
    ColumnDef("policy_no", "varchar(20)", required=True, is_pk=True, pk_order=2),
    ColumnDef("amt", "numeric(18,2)"),
    ColumnDef("deduct", "numeric(18,2)"),
], dataset_id=2)
CAT = {"policy": POLICY, "claim": CLAIM}

EXAMPLE = {
    "from": "policy",
    "joins": [{"table": "claim", "type": "left", "on": [["policy_no", "policy_no"]]}],
    "select": ["policy.policy_no", "claim.amt"],
    "calc": [{"name": "net", "expr": "claim.amt - claim.deduct"}],
    "where": [["policy.yyyymm", "=", "202610"]],
    "order": [["net", "desc", "nulls_last"]],
}


def sql(cq):
    c = cq.select.compile(dialect=D, compile_kwargs={"render_postcompile": True})
    return str(c), c.params


def q(spec=None, big=frozenset(), **over):
    s = copy.deepcopy(spec or EXAMPLE)
    s.update(over)
    return compile_query(s, CAT, big_tables=set(big))


def bad(msg, spec=None, big=frozenset(), **over):
    with pytest.raises(QuerySpecError, match=msg):
        q(spec, big, **over)


def test_build_table_excludes_disabled():
    t = build_table(POLICY)
    assert "old_col" not in t.c and "prem" in t.c


def test_example_compiles():
    cq = q()
    s, p = sql(cq)
    assert "LEFT OUTER JOIN claim ON policy.policy_no = claim.policy_no" in s
    assert "NULLS LAST" in s and "DESC" in s
    assert "::" not in s and "ILIKE" not in s
    assert cq.columns == ["policy__policy_no", "claim__amt", "net"]
    assert p["w0"] == 202610   # '202610' 依 integer 轉成 int


def test_inner_join_and_big_table_partition_ok():
    spec = copy.deepcopy(EXAMPLE)
    spec["joins"][0]["type"] = "inner"
    spec["where"].append(["claim.yyyymm", "between", [202601, 202610]])
    s, _ = sql(q(spec, big={"policy", "claim"}))
    assert "JOIN claim" in s and "LEFT OUTER" not in s


def test_big_table_without_partition_rejected():
    bad("指定期別", big={"policy"}, where=[["policy.plan", "=", "A"]])
    bad("指定期別", big={"policy"}, where=[])
    # 運算子不對（is_null / like_prefix）不算
    bad("指定期別", big={"policy"}, where=[["policy.yyyymm", "not_null"]])


def test_big_table_join_side_needs_its_own_partition():
    bad("claim", big={"claim"})   # 只有 policy.yyyymm


def test_in_is_expanding():
    s, p = sql(q(where=[["policy.yyyymm", "=", 202610], ["policy.policy_no", "in", ["A1", "B2", "C3"]]]))
    assert "IN (%(w1_1)s, %(w1_2)s, %(w1_3)s)" in s
    assert [p["w1_1"], p["w1_2"], p["w1_3"]] == ["A1", "B2", "C3"]
    raw = q(where=[["policy.policy_no", "in", ["A1", "B2"]]]).select.compile(dialect=D)
    assert "POSTCOMPILE" in str(raw)   # 未展開前是 expanding 參數


def test_in_limits():
    bad("1～10000", where=[["policy.policy_no", "in", []]])
    bad("1～10000", where=[["policy.policy_no", "in", ["a"] * 10001]])
    bad("陣列", where=[["policy.policy_no", "in", "A1"]])
    q(where=[["policy.policy_no", "in", ["a"] * 10000]])


def test_like_prefix_escaping():
    s, p = sql(q(where=[["policy.plan", "like_prefix", "a%_\\"]]))
    assert "LIKE %(w0)s ESCAPE '\\'" in s
    assert p["w0"] == "a\\%\\_\\\\%"
    assert "ILIKE" not in s


def test_like_prefix_only_for_text_and_string():
    bad("文字欄位", where=[["policy.prem", "like_prefix", "1"]])
    bad("字串", where=[["policy.plan", "like_prefix", 5]])
    bad("超過欄位上限", where=[["policy.plan", "like_prefix", "x" * 11]])


def test_is_null_not_null_between():
    s, _ = sql(q(where=[["policy.plan", "is_null"], ["policy.prem", "not_null"],
                        ["policy.eff_date", "between", ["2026-01-01", "2026-12-31"]]]))
    assert "policy.plan IS NULL" in s and "policy.prem IS NOT NULL" in s and "BETWEEN" in s
    bad("不接受值", where=[["policy.plan", "is_null", 1]])
    bad("需要值", where=[["policy.plan", "="]])
    bad("2 個元素", where=[["policy.prem", "between", [1]]])


def test_value_conversion():
    _, p = sql(q(where=[["policy.prem", "=", "12.50"], ["policy.eff_date", "=", "2026-10-01"],
                        ["policy.yyyymm", "=", 202610]]))
    assert p["w0"] == Decimal("12.50") and isinstance(p["w0"], Decimal)
    assert p["w1"] == date(2026, 10, 1)
    assert p["w2"] == 202610 and isinstance(p["w2"], int)
    _, p = sql(q(where=[["policy.prem", "=", 0.1]]))
    assert p["w0"] == Decimal("0.1")   # float 經 repr，不產生 0.1000000000000000055…


@pytest.mark.parametrize("w", [
    ["policy.yyyymm", "=", "abc"],
    ["policy.yyyymm", "=", 1.5],
    ["policy.yyyymm", "=", True],
    ["policy.yyyymm", "=", None],
    ["policy.prem", "=", "NaN"],
    ["policy.prem", "=", "x"],
    ["policy.eff_date", "=", "2026/10/01"],
    ["policy.eff_date", "=", 20261001],
    ["policy.plan", "=", "x" * 11],
    ["policy.plan", "=", 5],
    ["policy.plan", "=", ["a"]],
])
def test_value_conversion_failures(w):
    bad("無法轉成|超過欄位上限|值型別不合法", where=[w])


def test_unknown_operator_rejected():
    bad("不支援的運算子", where=[["policy.plan", "ilike", "a"]])
    bad("不支援的運算子", where=[["policy.plan", "<", "a"]])
    bad("不支援的運算子", where=[["policy.plan", "= 1 OR 1=1", "a"]])


def test_disabled_and_unknown_columns_rejected():
    bad("old_col", select=["policy.old_col"])
    bad("old_col", where=[["policy.old_col", "=", "a"]])
    bad("nope", select=["policy.nope"])
    bad("未出現", select=["other.x"])
    bad("需寫成", select=["policy_no"])
    bad("old_col", calc=[{"name": "c", "expr": "policy.old_col"}])


def test_unknown_keys_rejected():
    bad("未知的鍵", **{"limit": 5})
    bad("未知的鍵", joins=[{"table": "claim", "type": "left", "on": [["policy_no", "policy_no"]], "x": 1}])
    bad("未知的鍵", calc=[{"name": "a", "expr": "1", "z": 1}])


def test_from_and_joins_validation():
    bad("未知的資料集", **{"from": "nope"})
    bad("重複", joins=[{"table": "policy", "type": "inner", "on": [["policy_no", "policy_no"]]}])
    bad("重複", joins=EXAMPLE["joins"] * 2)
    bad("inner / left", joins=[{"table": "claim", "type": "right", "on": [["policy_no", "policy_no"]]}])
    bad("非空", joins=[{"table": "claim", "type": "left", "on": []}])
    bad("型別不相容", joins=[{"table": "claim", "type": "left", "on": [["policy_no", "amt"]]}])
    bad("必須屬於", joins=[{"table": "claim", "type": "left", "on": [["policy_no", "policy.policy_no"]]}])
    bad("沒有可用欄位", joins=[{"table": "claim", "type": "left", "on": [["policy_no", "nope"]]}])
    # 合法的 table-qualified 寫法
    q(joins=[{"table": "claim", "type": "left", "on": [["policy.policy_no", "claim.policy_no"],
                                                       ["policy.yyyymm", "yyyymm"]]}])


def test_calc_name_rules_and_errors():
    bad("name 不合法", calc=[{"name": "Net", "expr": "1"}])
    bad("name 不合法", calc=[{"name": "1a", "expr": "1"}])
    bad("name 不合法", calc=[{"name": "a" * 31, "expr": "1"}])
    bad("重複", calc=[{"name": "x", "expr": "1"}, {"name": "x", "expr": "2"}])
    bad("重複", calc=[{"name": "policy__policy_no", "expr": "1"}])
    bad("calc\\[0\\].expr：不允許的語法：Subscript", calc=[{"name": "x", "expr": "policy.plan[0]"}])
    bad("沒有可用欄位", calc=[{"name": "x", "expr": "zzz + 1"}])


def test_calc_division_sql():
    s, _ = sql(q(calc=[{"name": "r", "expr": "claim.amt / claim.deduct"}], order=[]))
    assert "CAST(claim.amt AS NUMERIC)" in s and "nullif(claim.deduct, 0)" in s.lower()


def test_column_limit_50():
    calcs = [{"name": f"c{i}", "expr": "1"} for i in range(50)]
    q(select=[], calc=calcs, order=[])
    bad("超過上限 50", select=["policy.plan"], calc=calcs)
    bad("至少要有一欄", select=[], calc=[])


def test_order_rules():
    bad("明寫 NULLS", order=[["net", "desc"]])
    bad("asc / desc", order=[["net", "up", "nulls_last"]])
    bad("nulls_first / nulls_last", order=[["net", "asc", "last"]])
    bad("未出現|需寫成", order=[["nope", "asc", "nulls_last"]])
    s, _ = sql(q(order=[["policy.prem", "asc", "nulls_first"], ["net", "desc", "nulls_last"]]))
    assert "policy.prem ASC NULLS FIRST" in s and "DESC NULLS LAST" in s


def test_paginate_and_count():
    cq = q()
    s, p = sql(type("C", (), {"select": paginate(cq, 100, 50)}))
    assert "LIMIT" in s and "OFFSET" in s and 50 in p.values() and 100 in p.values()
    with pytest.raises(QuerySpecError):
        paginate(cq, 0, 1001)
    with pytest.raises(QuerySpecError):
        paginate(cq, 0, 0)
    with pytest.raises(QuerySpecError):
        paginate(cq, -1, 10)
    cs = str(count_query(cq).compile(dialect=D))
    assert "count(*)" in cs.lower() and "ORDER BY" not in cs


def test_spec_not_dict():
    with pytest.raises(QuerySpecError):
        compile_query([], CAT, big_tables=set())
