"""自訂欄位運算式（SPEC 9.3 / AC11）。

AC11：每個攻擊樣式都斷言「白名單拒絕」的訊息（指出節點型別），不是資料庫錯誤。
"""
from decimal import Decimal

import pytest
from sqlalchemy import Integer, Numeric, column, select, table
from sqlalchemy.dialects import postgresql

from app.services.query.expr import ExprError, compile_expr

T = table("claim", column("amt", Numeric(18, 2)), column("deduct", Numeric(18, 2)), column("n", Integer))


def resolve(t, c):
    if t not in (None, "claim") or c not in T.c:
        raise ExprError(f"找不到欄位：{c}")
    return T.c[c]


def sql(text):
    e = compile_expr(text, resolve)
    c = select(e).compile(dialect=postgresql.psycopg2.dialect())
    return str(c), c.params


@pytest.mark.parametrize("text,msg", [
    ("amt[0]", "不允許的語法：Subscript"),
    ("claim.amt.x", "不允許的語法：Attribute"),
    ("a.b.c", "不允許的語法：Attribute"),
    ("f().x", "不允許的語法：Attribute"),
    ("(1).real", "不允許的語法：Attribute"),
    ("amt if amt else 1", "不允許的語法：IfExp"),
    ("lambda: 1", "不允許的語法：Lambda"),
    ("amt > 1", "不允許的語法：Compare"),
    ("amt and n", "不允許的語法：BoolOp"),
    ("not amt", "不允許的語法：Not"),
    ("~amt", "不允許的語法：Invert"),
    ("amt ** 2", "不允許的語法：Pow"),
    ("amt // 2", "不允許的語法：FloorDiv"),
    ("amt % 2", "不允許的語法：Mod"),
    ("[amt]", "不允許的語法：List"),
    ("(amt, n)", "不允許的語法：Tuple"),
    ("{1: 2}", "不允許的語法：Dict"),
    ("[x for x in amt]", "不允許的語法：ListComp"),
    ("f'{amt}'", "不允許的語法：JoinedStr"),
    ("'abc'", "不允許的語法：Constant"),
    ("'abc' + amt", "不允許的語法：Constant"),
    ("None", "不允許的語法：Constant"),
    ("1j", "不允許的語法：Constant"),
    ("True", "不允許的語法：Constant"),
    ("amt + False", "不允許的語法：Constant"),
    ("(1)(2)", "不允許的語法：Constant"),
    ("amt()(1)", "不允許的語法：Call"),
    ("amt.f(1)", "不允許的語法：Attribute"),
    ("amt.__class__.__mro__", "不允許的語法：Attribute"),
    ("__import__('os').system('x')", "不允許的語法：Attribute"),
    ("[1][0]", "不允許的語法：Subscript"),
])
def test_whitelist_rejects_with_node_type(text, msg):
    with pytest.raises(ExprError) as ei:
        compile_expr(text, resolve)
    assert msg in str(ei.value)


@pytest.mark.parametrize("text", [
    "; DROP TABLE x", "amt; DROP TABLE x", "amt -- x\n; select 1", "1 +", "(", "SELECT 1 FROM x",
    "(SELECT 1)", "amt UNION SELECT 1", "1; SELECT pg_sleep(10)",
])
def test_sql_like_strings_rejected_as_exprerror(text):
    # 語法錯誤或白名單拒絕，兩者都是 ExprError；絕不會走到資料庫
    with pytest.raises(ExprError):
        compile_expr(text, resolve)


def test_syntax_error_message():
    with pytest.raises(ExprError, match="語法錯誤"):
        compile_expr("; DROP TABLE x", resolve)


def test_nul_char():
    with pytest.raises(ExprError):
        compile_expr("amt\x00", resolve)


def test_empty_and_nonstring():
    with pytest.raises(ExprError):
        compile_expr("  ", resolve)
    with pytest.raises(ExprError):
        compile_expr(None, resolve)  # type: ignore[arg-type]


def test_too_long_checked_before_parse():
    with pytest.raises(ExprError, match="過長"):
        compile_expr("(" * 1001 + "1" + ")" * 1001, resolve)


def test_deep_nesting():
    with pytest.raises(ExprError, match="過深"):
        compile_expr("ABS(" * 30 + "1" + ")" * 30, resolve)


def test_deep_unary_within_length_limit_is_exprerror():
    # ≤1000 字但極深：不論 parser 先丟 RecursionError 或結構檢查先擋，都必須是 ExprError
    with pytest.raises(ExprError):
        compile_expr("-" * 990 + "1", resolve)
    with pytest.raises(ExprError):
        compile_expr("(" * 400 + "1" + ")" * 400, resolve)


def test_too_many_nodes_shallow():
    # 4 層樹、每層 4 個：深度小但節點多
    leaf = "(" + "+".join(["n"] * 3) + ")"
    mid = "(" + "+".join([leaf] * 3) + ")"
    top = "+".join([mid] * 3)   # 約 27 葉 + 運算子與內部節點，< 200
    compile_expr(top, resolve)
    big = "(" + "+".join([top] * 4) + ")"   # 超過 200 節點，深度仍 < 20
    with pytest.raises(ExprError, match="節點數"):
        compile_expr(big, resolve)


def test_unknown_function_and_forms():
    with pytest.raises(ExprError, match="不允許的函式"):
        compile_expr("SUM(amt)", resolve)
    with pytest.raises(ExprError, match="不允許的函式"):
        compile_expr("Round(amt)", resolve)   # 混合大小寫
    with pytest.raises(ExprError, match="keyword"):
        compile_expr("ROUND(amt, ndigits=2)", resolve)
    with pytest.raises(ExprError, match="Starred"):
        compile_expr("COALESCE(*amt)", resolve)
    with pytest.raises(ExprError, match="整數常數"):
        compile_expr("ROUND(amt, 11)", resolve)
    with pytest.raises(ExprError, match="整數常數"):
        compile_expr("ROUND(amt, n)", resolve)
    with pytest.raises(ExprError, match="整數常數"):
        compile_expr("ROUND(amt, 1.5)", resolve)
    with pytest.raises(ExprError, match="整數常數"):
        compile_expr("ROUND(amt, True)", resolve)
    with pytest.raises(ExprError, match="2～5"):
        compile_expr("COALESCE(amt)", resolve)
    with pytest.raises(ExprError, match="2～5"):
        compile_expr("COALESCE(1,2,3,4,5,6)", resolve)
    with pytest.raises(ExprError, match="1 個參數"):
        compile_expr("ABS(amt, 1)", resolve)


def test_long_constant():
    with pytest.raises(ExprError, match="過長"):
        compile_expr("1" * 31, resolve)
    compile_expr("1" * 30, resolve)


def test_unknown_column():
    with pytest.raises(ExprError, match="找不到欄位"):
        compile_expr("nope + 1", resolve)
    with pytest.raises(ExprError, match="找不到欄位"):
        compile_expr("other.amt", resolve)


def test_arithmetic_and_attribute_ref():
    s, p = sql("claim.amt - claim.deduct * 2")
    assert "claim.amt - claim.deduct * %(param_1)s" in s
    assert list(p.values()) == [Decimal("2")]   # Decimal 常數是綁定參數，不是字面拼接


def test_unary():
    s, _ = sql("-amt + +n")
    assert "-claim.amt" in s


def test_division_nullif_cast():
    s, _ = sql("amt / deduct")
    assert "CAST(claim.amt AS NUMERIC)" in s
    assert "nullif(claim.deduct, 0)" in s.lower() and "::" not in s


def test_functions():
    s, _ = sql("ROUND(amt / deduct, 2)")
    assert "round(CAST(" in s and ", 2)" in s and "nullif(" in s
    s, _ = sql("round(amt)")
    assert "round(CAST(claim.amt AS NUMERIC))" in s
    s, _ = sql("ABS(amt)")
    assert "abs(claim.amt)" in s
    s, _ = sql("coalesce(amt, deduct, 0)")
    assert "coalesce(claim.amt, claim.deduct, %(" in s


def test_decimal_constant_not_float():
    _, p = sql("amt * 0.1")
    v = list(p.values())[0]
    assert isinstance(v, Decimal) and v == Decimal("0.1") and str(v) == "0.1"


def test_constant_is_bound_not_inlined():
    s, p = sql("amt + 1e3")
    assert "1e3" not in s and "1000" not in s
    assert list(p.values()) == [Decimal("1e3")]
