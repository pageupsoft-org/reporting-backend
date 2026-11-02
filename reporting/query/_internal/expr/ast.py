"""Typed AST for custom expressions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Literal:
    value: int | Decimal | str | bool | None


@dataclass(frozen=True)
class FieldRef:
    alias: str
    column: str

    @property
    def ref(self) -> str:
        return f"{self.alias}.{self.column}"


@dataclass(frozen=True)
class CalcRef:
    name: str


@dataclass(frozen=True)
class FuncCall:
    name: str  # upper-cased
    args: tuple[Node, ...]


@dataclass(frozen=True)
class BinaryOp:
    op: str  # + - * / = <> < <= > >= AND OR
    left: Node
    right: Node


@dataclass(frozen=True)
class UnaryOp:
    op: str  # NEG | NOT
    operand: Node


@dataclass(frozen=True)
class Case:
    whens: tuple[tuple[Node, Node], ...]
    else_: Node | None


Node = Literal | FieldRef | CalcRef | FuncCall | BinaryOp | UnaryOp | Case


def children(node: Node) -> tuple[Node, ...]:
    if isinstance(node, FuncCall):
        return node.args
    if isinstance(node, BinaryOp):
        return (node.left, node.right)
    if isinstance(node, UnaryOp):
        return (node.operand,)
    if isinstance(node, Case):
        out: list[Node] = [n for pair in node.whens for n in pair]
        if node.else_ is not None:
            out.append(node.else_)
        return tuple(out)
    return ()


def walk(node: Node) -> list[Node]:
    out = [node]
    for child in children(node):
        out.extend(walk(child))
    return out


def depth(node: Node) -> int:
    kids = children(node)
    return 1 + (max(depth(k) for k in kids) if kids else 0)
