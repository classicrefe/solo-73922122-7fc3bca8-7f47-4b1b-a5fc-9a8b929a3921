"""WHERE 原子条件枚举（按书写顺序）、目标条件识别与 SQL 文本渲染。

原子条件 = 列与整数字面量的比较（Compare）或判空（IsNull）；
AND / OR / NOT 视为连接词，不作为独立条件。
"""
from __future__ import annotations

from collections.abc import Callable

from .ir import And, BoolNode, ColRef, Compare, IsNull, Not, Or

# 样本 API 关注的目标列：readings.value
TARGET_COLUMN = ColRef("readings", "value")

Atom = Compare | IsNull


def iter_atoms(node: BoolNode | None) -> list[Atom]:
    """按书写顺序（中序遍历）枚举 WHERE 中的全部原子条件。"""
    atoms: list[Atom] = []

    def walk(n: BoolNode) -> None:
        if isinstance(n, (Compare, IsNull)):
            atoms.append(n)
        elif isinstance(n, (And, Or)):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, Not):
            walk(n.child)
        else:  # pragma: no cover - 防御
            raise TypeError(f"未知布尔节点: {n!r}")

    if node is not None:
        walk(node)
    return atoms


def is_target_condition(atom: Atom) -> bool:
    """原子条件是否检查 readings.value（比较或判空）。"""
    return atom.col == TARGET_COLUMN


def render_condition(node: BoolNode) -> str:
    """渲染为表名限定的可读 SQL 片段（用于响应中标识条件）。"""
    return _render(node, lambda c: f"{c.table}.{c.column}")


def diagnostic_sql(atoms: list[Atom], where: BoolNode) -> str:
    """构造诊断查询：对每条连接行输出各原子条件与整体 WHERE 的三值结果。

    连接形态与样本 API 的固定限制一致（stations LEFT JOIN readings），
    因此诊断查询的行流与被测查询完全一致。bool 列在 PostgreSQL 中天然
    是三值的（TRUE / FALSE / NULL），psycopg 映射为 True / False / None。
    """
    exprs = ['"s"."id"', '"r"."station_id"', '"r"."value"']
    exprs += [_render(a, _alias_col) for a in atoms]
    exprs.append(_render(where, _alias_col))
    return (
        "SELECT "
        + ", ".join(exprs)
        + " FROM stations s LEFT JOIN readings r ON s.id = r.station_id"
    )


def _alias_col(c: ColRef) -> str:
    alias = {"stations": "s", "readings": "r"}[c.table]
    return f'"{alias}"."{c.column}"'


def _render(node: BoolNode, colfmt: Callable[[ColRef], str]) -> str:
    if isinstance(node, Compare):
        return f"({colfmt(node.col)} {node.op} {node.value})"
    if isinstance(node, IsNull):
        return f"({colfmt(node.col)} IS {'NOT ' if node.negated else ''}NULL)"
    if isinstance(node, And):
        return f"({_render(node.left, colfmt)} AND {_render(node.right, colfmt)})"
    if isinstance(node, Or):
        return f"({_render(node.left, colfmt)} OR {_render(node.right, colfmt)})"
    if isinstance(node, Not):
        return f"(NOT {_render(node.child, colfmt)})"
    raise TypeError(f"未知布尔节点: {node!r}")  # pragma: no cover - 防御
