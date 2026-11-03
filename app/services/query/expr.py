"""自訂欄位運算式編譯器（SPEC 9.3；AC11 安全關鍵）。

白名單 AST → SQLAlchemy 表達式物件；不拼字串。
"""
from __future__ import annotations

import ast
from decimal import Decimal, InvalidOperation
from typing import Callable

from sqlalchemy import Numeric, cast, func, literal_column
from sqlalchemy.sql.elements import ColumnElement, literal

MAX_LEN = 1000
MAX_NODES = 200
MAX_DEPTH = 20
MAX_CONST_LEN = 30


class ExprError(ValueError):
    """運算式不合法（訊息為中文，可直接回給使用者）。"""


Resolver = Callable[[str | None, str], ColumnElement]

_FUNCS = {"ROUND", "ABS", "COALESCE"}


def _check_structure(tree: ast.AST) -> None:
    """節點數與深度（迭代式，避免自身遞迴）。"""
    count = 0
    stack = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES:
            raise ExprError(f"運算式過於複雜：節點數超過 {MAX_NODES}")
        if depth > MAX_DEPTH:
            raise ExprError(f"運算式巢狀過深：超過 {MAX_DEPTH} 層")
        for ch in ast.iter_child_nodes(node):
            if not isinstance(ch, ast.expr_context):   # Load/Store 只是標記，不計入
                stack.append((ch, depth + 1))


def _reject(node: ast.AST) -> ExprError:
    return ExprError(f"不允許的語法：{type(node).__name__}")


def compile_expr(text: str, resolve: Resolver) -> ColumnElement:
    if not isinstance(text, str):
        raise ExprError("運算式必須是字串")
    if len(text) > MAX_LEN:   # 必須在 ast.parse 之前，避免深層巢狀造成 RecursionError
        raise ExprError(f"運算式過長：超過 {MAX_LEN} 字")
    if not text.strip():
        raise ExprError("運算式不可為空")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"運算式語法錯誤：{e.msg}") from None
    except (RecursionError, MemoryError):
        raise ExprError("運算式過於複雜，無法解析") from None
    except ValueError as e:   # 例如含 NUL 字元
        raise ExprError(f"運算式語法錯誤：{e}") from None
    _check_structure(tree)
    return _Compiler(text, resolve).node(tree.body)


class _Compiler:
    def __init__(self, text: str, resolve: Resolver):
        self.text = text
        self.resolve = resolve

    def node(self, node: ast.AST) -> ColumnElement:
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                raise _reject(node.op)
            a, b = self.node(node.left), self.node(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            return cast(a, Numeric) / func.nullif(b, literal_column("0"))
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.USub, ast.UAdd)):
                raise _reject(node.op)
            v = self.node(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.Constant):
            return self.const(node)
        if isinstance(node, ast.Name):
            return self.ref(None, node.id)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                return self.ref(node.value.id, node.attr)
            raise _reject(node)
        if isinstance(node, ast.Call):
            return self.call(node)
        raise _reject(node)

    def const(self, node: ast.Constant) -> ColumnElement:
        v = node.value
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ExprError(f"不允許的語法：Constant（{type(v).__name__} 常數；只接受數值）")
        lit = ast.get_source_segment(self.text, node) or repr(v)
        if len(lit) > MAX_CONST_LEN:
            raise ExprError(f"數值常數過長：超過 {MAX_CONST_LEN} 字")
        try:
            d = Decimal(lit)
        except InvalidOperation:
            raise ExprError(f"數值常數不合法：{lit}") from None
        if not d.is_finite():
            raise ExprError(f"數值常數不合法：{lit}")
        return literal(d, Numeric())

    def ref(self, t: str | None, c: str) -> ColumnElement:
        label = f"{t}.{c}" if t else c
        try:
            r = self.resolve(t, c)
        except ExprError:
            raise
        except Exception as e:   # resolve 丟別種例外也統一成 ExprError
            raise ExprError(f"找不到欄位：{label}") from e
        if r is None:
            raise ExprError(f"找不到欄位：{label}")
        return r

    def int_const(self, node: ast.AST, lo: int, hi: int, what: str) -> int:
        if (isinstance(node, ast.Constant) and isinstance(node.value, int)
                and not isinstance(node.value, bool) and lo <= node.value <= hi):
            return node.value
        raise ExprError(f"{what}必須是 {lo}～{hi} 的整數常數")

    def call(self, node: ast.Call) -> ColumnElement:
        if not isinstance(node.func, ast.Name):
            raise _reject(node.func)
        name = node.func.id
        if name != name.upper() and name != name.lower():
            raise ExprError(f"不允許的函式：{name}（只接受全大寫或全小寫寫法）")
        fn = name.upper()
        if fn not in _FUNCS:
            raise ExprError(f"不允許的函式：{name}（只支援 ROUND、ABS、COALESCE）")
        if node.keywords:
            raise ExprError(f"不允許的語法：keyword（{fn} 不接受具名參數）")
        if any(isinstance(a, ast.Starred) for a in node.args):
            raise ExprError("不允許的語法：Starred")
        args = node.args
        if fn == "ABS":
            if len(args) != 1:
                raise ExprError("ABS 需要 1 個參數")
            return func.abs(self.node(args[0]))
        if fn == "ROUND":
            if len(args) not in (1, 2):
                raise ExprError("ROUND 需要 1 或 2 個參數")
            x = cast(self.node(args[0]), Numeric)   # PostgreSQL 沒有 round(double, int)，先轉 NUMERIC
            if len(args) == 1:
                return func.round(x)
            n = self.int_const(args[1], 0, 10, "ROUND 的位數")
            return func.round(x, literal_column(str(n)))   # n 已驗證為 0～10 整數，內嵌避免參數型別推斷問題
        if not 2 <= len(args) <= 5:
            raise ExprError("COALESCE 需要 2～5 個參數")
        return func.coalesce(*[self.node(a) for a in args])
