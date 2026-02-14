#!/usr/bin/env python3
"""MCP server implementing the reduced-surface v5 contract in context-feature-spec.md."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import mcp.types as types
import z3
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.lowlevel.server import request_ctx

from .audit_log import append_tool_log, build_tool_call_payload
from .errors import LogicError
from .store import Store
from .supervisor import INTERCEPT_STAGE_CALL
from .supervisor import INTERCEPT_STAGE_REPLY
from .supervisor import SUPERVISOR
from .supervisor import new_event_payload_for_log

DEFAULT_TIMEOUT_MS = 2000
INFLUENCE_BUDGET = 20
MAX_AST_NODES = 5000
MAX_SYMBOLS = 2000

TYPE_INT = "Int"
TYPE_REAL = "Real"
TYPE_BOOL = "Bool"
SMT2_ALLOWED_COMMANDS = {
    "assert",
    "declare-const",
    "declare-fun",
    "define-fun",
    "define-fun-rec",
    "define-funs-rec",
}


@dataclass
class ConstraintBundle:
    item_id: str
    assertions: List[z3.BoolRef]
    full_expr: z3.BoolRef
    labels: List[str]
    symbols_used: List[str]


@dataclass
class ExpectationResult:
    status: str  # pass|fail|unknown
    counterexample: Optional[dict] = None
    reason: Optional[str] = None


@dataclass
class CachedBaseContext:
    key: str
    solver: z3.Solver
    symbol_table: Dict[str, str]
    z3_vars: Dict[str, z3.ExprRef]
    bundle_assumptions: Dict[str, List[z3.BoolRef]]
    rule_assumptions: Dict[str, List[z3.BoolRef]]
    assumption_to_item: Dict[str, str]
    active_bundles: Dict[str, dict]
    active_rules: Dict[str, dict]


class PyExprCompiler:
    def __init__(self, symbol_table: Dict[str, str]):
        self.symbol_table = symbol_table
        self.constraints: Dict[str, Dict[str, bool]] = {}
        self.vars_used: List[str] = []

    def _record_constraint(self, name: str, *, kind: str) -> None:
        if name not in self.constraints:
            self.constraints[name] = {
                "bool": False,
                "numeric": False,
                "int": False,
                "real": False,
            }
        self.constraints[name][kind] = True

    def _guess_type(self, node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return TYPE_BOOL
            if isinstance(node.value, int):
                return TYPE_INT
            if isinstance(node.value, float):
                return TYPE_REAL
        if isinstance(node, ast.Name):
            return self.symbol_table.get(node.id)
        if isinstance(node, ast.BoolOp):
            return TYPE_BOOL
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return TYPE_BOOL
        if isinstance(node, ast.Compare):
            return TYPE_BOOL
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Mod):
                return TYPE_INT
            if isinstance(node.op, ast.Div):
                return TYPE_REAL
            left = self._guess_type(node.left)
            right = self._guess_type(node.right)
            if left == TYPE_REAL or right == TYPE_REAL:
                return TYPE_REAL
            if left == TYPE_INT and right == TYPE_INT:
                return TYPE_INT
            return None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"abs", "min", "max"}:
                arg_types = [self._guess_type(arg) for arg in node.args]
                if any(t == TYPE_REAL for t in arg_types):
                    return TYPE_REAL
                if all(t == TYPE_INT for t in arg_types if t is not None) and arg_types:
                    return TYPE_INT
        if isinstance(node, ast.IfExp):
            t_body = self._guess_type(node.body)
            t_else = self._guess_type(node.orelse)
            if t_body == t_else:
                return t_body
        return None

    def _validate_node(self, node: ast.AST, node_count: List[int]) -> None:
        node_count[0] += 1
        if node_count[0] > MAX_AST_NODES:
            raise LogicError("E_UNSUPPORTED", "pyexpr too large")
        allowed = (
            ast.Expression,
            ast.BoolOp,
            ast.BinOp,
            ast.UnaryOp,
            ast.Compare,
            ast.IfExp,
            ast.Call,
            ast.Name,
            ast.Constant,
            ast.operator,
            ast.boolop,
            ast.unaryop,
            ast.cmpop,
            ast.expr_context,
        )
        if not isinstance(node, allowed):
            raise LogicError("E_UNSUPPORTED", f"Unsupported pyexpr construct: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise LogicError("E_UNSUPPORTED", "Only simple function calls are allowed")
            if node.func.id not in {"abs", "min", "max"}:
                raise LogicError("E_UNSUPPORTED", f"Function '{node.func.id}' is not allowed")
            if node.keywords:
                raise LogicError("E_UNSUPPORTED", "Keyword arguments are not allowed")
        for child in ast.iter_child_nodes(node):
            self._validate_node(child, node_count)

    def _analyze(self, node: ast.AST, expected: Optional[str] = None) -> Optional[str]:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return TYPE_BOOL
            if isinstance(node.value, int):
                return TYPE_INT
            if isinstance(node.value, float):
                return TYPE_REAL
            raise LogicError("E_UNSUPPORTED", "Unsupported literal in pyexpr")
        if isinstance(node, ast.Name):
            name = node.id
            if name not in self.vars_used:
                self.vars_used.append(name)
            declared = self.symbol_table.get(name)
            if expected == TYPE_BOOL:
                self._record_constraint(name, kind="bool")
            elif expected == TYPE_INT:
                self._record_constraint(name, kind="int")
                self._record_constraint(name, kind="numeric")
            elif expected == TYPE_REAL:
                self._record_constraint(name, kind="real")
                self._record_constraint(name, kind="numeric")
            elif expected == "numeric":
                self._record_constraint(name, kind="numeric")
            if declared:
                if expected == TYPE_BOOL and declared != TYPE_BOOL:
                    raise LogicError("E_PARSE_ERROR", f"Type mismatch for {name}")
                if expected in {TYPE_INT, TYPE_REAL, "numeric"} and declared == TYPE_BOOL:
                    raise LogicError("E_PARSE_ERROR", f"Type mismatch for {name}")
                return declared
            return expected
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                self._analyze(value, expected=TYPE_BOOL)
            return TYPE_BOOL
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                self._analyze(node.operand, expected=TYPE_BOOL)
                return TYPE_BOOL
            if isinstance(node.op, (ast.UAdd, ast.USub)):
                return self._analyze(node.operand, expected="numeric")
            raise LogicError("E_UNSUPPORTED", "Unsupported unary operator")
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Mod):
                self._analyze(node.left, expected=TYPE_INT)
                self._analyze(node.right, expected=TYPE_INT)
                return TYPE_INT
            if isinstance(node.op, ast.Div):
                self._analyze(node.left, expected=TYPE_REAL)
                self._analyze(node.right, expected=TYPE_REAL)
                return TYPE_REAL
            self._analyze(node.left, expected="numeric")
            self._analyze(node.right, expected="numeric")
            left_guess = self._guess_type(node.left)
            right_guess = self._guess_type(node.right)
            if left_guess == TYPE_REAL or right_guess == TYPE_REAL:
                self._analyze(node.left, expected=TYPE_REAL)
                self._analyze(node.right, expected=TYPE_REAL)
                return TYPE_REAL
            if left_guess == TYPE_INT and right_guess == TYPE_INT:
                return TYPE_INT
            return "numeric"
        if isinstance(node, ast.Compare):
            if len(node.ops) != len(node.comparators):
                raise LogicError("E_PARSE_ERROR", "Invalid comparison")
            left = node.left
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                    left_guess = self._guess_type(left)
                    right_guess = self._guess_type(right)
                    expected = TYPE_REAL if left_guess == TYPE_REAL or right_guess == TYPE_REAL else "numeric"
                    self._analyze(left, expected=expected)
                    self._analyze(right, expected=expected)
                elif isinstance(op, (ast.Eq, ast.NotEq)):
                    left_guess = self._guess_type(left)
                    right_guess = self._guess_type(right)
                    if left_guess == TYPE_BOOL or right_guess == TYPE_BOOL:
                        self._analyze(left, expected=TYPE_BOOL)
                        self._analyze(right, expected=TYPE_BOOL)
                    else:
                        expected = TYPE_REAL if left_guess == TYPE_REAL or right_guess == TYPE_REAL else "numeric"
                        self._analyze(left, expected=expected)
                        self._analyze(right, expected=expected)
                else:
                    raise LogicError("E_UNSUPPORTED", "Unsupported comparison operator")
                left = right
            return TYPE_BOOL
        if isinstance(node, ast.IfExp):
            self._analyze(node.test, expected=TYPE_BOOL)
            t_body = self._analyze(node.body)
            t_else = self._analyze(node.orelse)
            if t_body == TYPE_BOOL or t_else == TYPE_BOOL:
                if t_body != t_else:
                    raise LogicError("E_PARSE_ERROR", "Conditional branches must have same type")
                return TYPE_BOOL
            if t_body == TYPE_REAL or t_else == TYPE_REAL:
                return TYPE_REAL
            if t_body == TYPE_INT and t_else == TYPE_INT:
                return TYPE_INT
            return "numeric"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            func = node.func.id
            if func == "abs":
                self._analyze(node.args[0], expected="numeric")
                arg_guess = self._guess_type(node.args[0])
                return TYPE_REAL if arg_guess == TYPE_REAL else TYPE_INT
            if func in {"min", "max"}:
                for arg in node.args:
                    self._analyze(arg, expected="numeric")
                arg_types = [self._guess_type(arg) for arg in node.args]
                if any(t == TYPE_REAL for t in arg_types):
                    return TYPE_REAL
                return TYPE_INT
        raise LogicError("E_UNSUPPORTED", f"Unsupported pyexpr construct: {type(node).__name__}")

    def infer_types(self, expr: str) -> Dict[str, str]:
        try:
            parsed = ast.parse(expr, mode="eval")
        except SyntaxError as exc:
            raise LogicError("E_PARSE_ERROR", f"Invalid pyexpr: {exc.msg} at position {exc.offset}")
        self._validate_node(parsed, [0])
        self._analyze(parsed.body)
        resolved: Dict[str, str] = {}
        for name in self.vars_used:
            if name in resolved:
                continue
            declared = self.symbol_table.get(name)
            constraints = self.constraints.get(name, {})
            requires_bool = constraints.get("bool", False)
            requires_numeric = constraints.get("numeric", False)
            requires_int = constraints.get("int", False)
            requires_real = constraints.get("real", False)
            if declared:
                if declared == TYPE_BOOL and (requires_numeric or requires_int or requires_real):
                    raise LogicError("E_PARSE_ERROR", f"Type mismatch for {name}")
                if declared in {TYPE_INT, TYPE_REAL} and requires_bool:
                    raise LogicError("E_PARSE_ERROR", f"Type mismatch for {name}")
                resolved[name] = declared
                continue
            if requires_bool:
                if requires_numeric or requires_int or requires_real:
                    raise LogicError("E_PARSE_ERROR", f"Type mismatch for {name}")
                resolved[name] = TYPE_BOOL
            elif requires_real:
                resolved[name] = TYPE_REAL
            elif requires_int:
                resolved[name] = TYPE_INT
            elif requires_numeric:
                resolved[name] = TYPE_INT
            else:
                resolved[name] = TYPE_INT
        return resolved

    def compile(self, expr: str, z3_vars: Dict[str, z3.ExprRef]) -> Tuple[z3.BoolRef, List[str]]:
        parsed = ast.parse(expr, mode="eval")
        self._validate_node(parsed, [0])
        types_map = self.infer_types(expr)
        for name, typ in types_map.items():
            if name not in self.symbol_table:
                self.symbol_table[name] = typ
        if len(self.symbol_table) > MAX_SYMBOLS:
            raise LogicError("E_UNSUPPORTED", "Too many symbols")

        def get_var(name: str) -> Tuple[z3.ExprRef, str]:
            if name not in z3_vars:
                sort = self.symbol_table[name]
                if sort == TYPE_BOOL:
                    z3_vars[name] = z3.Bool(name)
                elif sort == TYPE_REAL:
                    z3_vars[name] = z3.Real(name)
                else:
                    z3_vars[name] = z3.Int(name)
            return z3_vars[name], self.symbol_table[name]

        def coerce_numeric(a: z3.ExprRef, a_t: str, b: z3.ExprRef, b_t: str) -> Tuple[z3.ExprRef, z3.ExprRef, str]:
            if a_t == TYPE_REAL or b_t == TYPE_REAL:
                if a_t == TYPE_INT:
                    a = z3.ToReal(a)
                if b_t == TYPE_INT:
                    b = z3.ToReal(b)
                return a, b, TYPE_REAL
            return a, b, TYPE_INT

        def build(node: ast.AST) -> Tuple[z3.ExprRef, str]:
            if isinstance(node, ast.Constant):
                if isinstance(node.value, bool):
                    return z3.BoolVal(node.value), TYPE_BOOL
                if isinstance(node.value, int):
                    return z3.IntVal(node.value), TYPE_INT
                if isinstance(node.value, float):
                    return z3.RealVal(str(node.value)), TYPE_REAL
                raise LogicError("E_UNSUPPORTED", "Unsupported literal")
            if isinstance(node, ast.Name):
                expr, t = get_var(node.id)
                return expr, t
            if isinstance(node, ast.BoolOp):
                values = [build(v)[0] for v in node.values]
                if isinstance(node.op, ast.And):
                    return z3.And(*values), TYPE_BOOL
                if isinstance(node.op, ast.Or):
                    return z3.Or(*values), TYPE_BOOL
                raise LogicError("E_UNSUPPORTED", "Unsupported boolean operator")
            if isinstance(node, ast.UnaryOp):
                if isinstance(node.op, ast.Not):
                    expr, t = build(node.operand)
                    if t != TYPE_BOOL:
                        raise LogicError("E_PARSE_ERROR", "not operand must be boolean")
                    return z3.Not(expr), TYPE_BOOL
                if isinstance(node.op, ast.USub):
                    expr, t = build(node.operand)
                    if t not in {TYPE_INT, TYPE_REAL}:
                        raise LogicError("E_PARSE_ERROR", "Unary minus requires numeric")
                    return -expr, t
                if isinstance(node.op, ast.UAdd):
                    expr, t = build(node.operand)
                    if t not in {TYPE_INT, TYPE_REAL}:
                        raise LogicError("E_PARSE_ERROR", "Unary plus requires numeric")
                    return expr, t
            if isinstance(node, ast.BinOp):
                if isinstance(node.op, ast.Mod):
                    left, lt = build(node.left)
                    right, rt = build(node.right)
                    if lt != TYPE_INT or rt != TYPE_INT:
                        raise LogicError("E_PARSE_ERROR", "Modulo requires integers")
                    return left % right, TYPE_INT
                if isinstance(node.op, ast.Div):
                    left, lt = build(node.left)
                    right, rt = build(node.right)
                    left, right, _ = coerce_numeric(left, lt, right, rt)
                    return left / right, TYPE_REAL
                left, lt = build(node.left)
                right, rt = build(node.right)
                left, right, out_t = coerce_numeric(left, lt, right, rt)
                if isinstance(node.op, ast.Add):
                    return left + right, out_t
                if isinstance(node.op, ast.Sub):
                    return left - right, out_t
                if isinstance(node.op, ast.Mult):
                    return left * right, out_t
                raise LogicError("E_UNSUPPORTED", "Unsupported binary operator")
            if isinstance(node, ast.Compare):
                exprs = []
                left_node = node.left
                left_expr, left_type = build(left_node)
                for op, right_node in zip(node.ops, node.comparators):
                    right_expr, right_type = build(right_node)
                    if isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                        left_expr, right_expr, _ = coerce_numeric(left_expr, left_type, right_expr, right_type)
                        if isinstance(op, ast.Lt):
                            exprs.append(left_expr < right_expr)
                        elif isinstance(op, ast.LtE):
                            exprs.append(left_expr <= right_expr)
                        elif isinstance(op, ast.Gt):
                            exprs.append(left_expr > right_expr)
                        elif isinstance(op, ast.GtE):
                            exprs.append(left_expr >= right_expr)
                    elif isinstance(op, (ast.Eq, ast.NotEq)):
                        if left_type == TYPE_BOOL or right_type == TYPE_BOOL:
                            if left_type != TYPE_BOOL or right_type != TYPE_BOOL:
                                raise LogicError("E_PARSE_ERROR", "Boolean comparison requires both sides bool")
                            comp = left_expr == right_expr
                        else:
                            left_expr, right_expr, _ = coerce_numeric(left_expr, left_type, right_expr, right_type)
                            comp = left_expr == right_expr
                        if isinstance(op, ast.NotEq):
                            comp = z3.Not(comp)
                        exprs.append(comp)
                    else:
                        raise LogicError("E_UNSUPPORTED", "Unsupported comparison operator")
                    left_expr, left_type = right_expr, right_type
                return z3.And(*exprs), TYPE_BOOL
            if isinstance(node, ast.IfExp):
                test_expr, test_type = build(node.test)
                if test_type != TYPE_BOOL:
                    raise LogicError("E_PARSE_ERROR", "Conditional test must be boolean")
                body_expr, body_type = build(node.body)
                else_expr, else_type = build(node.orelse)
                if body_type == TYPE_BOOL or else_type == TYPE_BOOL:
                    if body_type != else_type:
                        raise LogicError("E_PARSE_ERROR", "Conditional branches must have same type")
                    return z3.If(test_expr, body_expr, else_expr), TYPE_BOOL
                body_expr, else_expr, out_t = coerce_numeric(body_expr, body_type, else_expr, else_type)
                return z3.If(test_expr, body_expr, else_expr), out_t
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                func = node.func.id
                args = [build(arg) for arg in node.args]
                if func == "abs":
                    if len(args) != 1:
                        raise LogicError("E_PARSE_ERROR", "abs() takes one argument")
                    expr, t = args[0]
                    if t not in {TYPE_INT, TYPE_REAL}:
                        raise LogicError("E_PARSE_ERROR", "abs() requires numeric")
                    return z3.If(expr >= 0, expr, -expr), t
                if func in {"min", "max"}:
                    if len(args) < 1:
                        raise LogicError("E_PARSE_ERROR", "min/max require at least one argument")
                    expr, t = args[0]
                    for nxt_expr, nxt_t in args[1:]:
                        expr, nxt_expr, out_t = coerce_numeric(expr, t, nxt_expr, nxt_t)
                        if func == "min":
                            expr = z3.If(expr <= nxt_expr, expr, nxt_expr)
                        else:
                            expr = z3.If(expr >= nxt_expr, expr, nxt_expr)
                        t = out_t
                    return expr, t
            raise LogicError("E_UNSUPPORTED", "Unsupported pyexpr construct")

        expr, t = build(parsed.body)
        if t != TYPE_BOOL:
            raise LogicError("E_PARSE_ERROR", "pyexpr must evaluate to boolean")
        return expr, self.vars_used


class LogicEngine:
    def __init__(self, namespace_id: str):
        self.store = Store(namespace_id)
        self._ensure_context_root(self.store.data)
        self._cache_lock = threading.RLock()
        self._base_context: Optional[CachedBaseContext] = None

    def _invalidate_cache(self) -> None:
        with self._cache_lock:
            self._base_context = None

    def _context_key(self, bundles: Dict[str, dict], rules: Dict[str, dict]) -> str:
        payload = {
            "symbols": self.store.data.get("symbols", {}),
            "bundles": [
                {
                    "id": bundle_id,
                    "version": entry.get("version"),
                    "lang": entry.get("lang"),
                    "content": entry.get("content"),
                }
                for bundle_id, entry in bundles.items()
            ],
            "rules": [
                {
                    "id": rule_id,
                    "version": entry.get("version"),
                    "lang": entry.get("lang"),
                    "content": entry.get("content"),
                }
                for rule_id, entry in rules.items()
            ],
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _assumption_name(self, kind: str, item_id: str, idx: int) -> str:
        token = hashlib.sha1(f"{kind}:{item_id}:{idx}".encode("utf-8")).hexdigest()[:12]
        return f"a_{kind}_{idx}_{token}"

    def _extract_smt2_command(self, command: str) -> str:
        m = re.match(r"\(\s*([^\s\(\)]+)", command)
        if not m:
            raise LogicError("E_PARSE_ERROR", f"Invalid SMT2 command: {command!r}")
        return m.group(1)

    def _validate_smt2_commands(self, smt2: str) -> None:
        for command in self._split_commands(smt2):
            cmd = self._extract_smt2_command(command)
            if cmd not in SMT2_ALLOWED_COMMANDS:
                raise LogicError("E_UNSUPPORTED", f"Unsupported SMT2 command '{cmd}'")

    def _enforce_symbol_limit(self, symbol_table: Dict[str, str]) -> None:
        if len(symbol_table) > MAX_SYMBOLS:
            raise LogicError("E_UNSUPPORTED", f"Too many symbols (>{MAX_SYMBOLS})")

    def _normalize_smt2(self, content: Any) -> str:
        if isinstance(content, list):
            return "\n".join(str(line) for line in content)
        if isinstance(content, str):
            return content
        raise LogicError("E_INVALID_REQUEST", "smt2 content must be string or list")

    def _strip_comments(self, smt2: str) -> str:
        lines = []
        for line in smt2.splitlines():
            if ";" in line:
                line = line.split(";", 1)[0]
            lines.append(line)
        return "\n".join(lines)

    def _split_commands(self, smt2: str) -> List[str]:
        smt2 = self._strip_comments(smt2)
        cmds: List[str] = []
        depth = 0
        start = None
        in_string = False
        prev = ""
        for i, ch in enumerate(smt2):
            if ch == '"' and prev != "\\":
                in_string = not in_string
            if in_string:
                prev = ch
                continue
            if ch == "(":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and start is not None:
                    cmds.append(smt2[start : i + 1].strip())
                    start = None
            prev = ch
        return cmds

    def _declared_names_in_smt2(self, smt2: str) -> set[str]:
        names: set[str] = set()
        cmds = self._split_commands(smt2)
        for cmd in cmds:
            m = re.match(r"\(\s*declare-const\s+([^\s\)]+)\s+([^\s\)]+)\s*\)", cmd)
            if m:
                names.add(m.group(1))
                continue
            m = re.match(r"\(\s*declare-fun\s+([^\s\)]+)\s*\(([^)]*)\)\s+([^\s\)]+)\s*\)", cmd)
            if m:
                args = m.group(2).strip()
                if args == "":
                    names.add(m.group(1))
        return names

    def _update_symbols_from_smt2(self, smt2: str, symbol_table: Dict[str, str]) -> None:
        cmds = self._split_commands(smt2)
        symtab = symbol_table
        for cmd in cmds:
            m = re.match(r"\(\s*declare-const\s+([^\s\)]+)\s+([^\s\)]+)\s*\)", cmd)
            if m:
                name, sort = m.group(1), m.group(2)
                if sort in {TYPE_INT, TYPE_REAL, TYPE_BOOL}:
                    # SMT2 declarations take precedence over inferred types.
                    symtab[name] = sort
                continue
            m = re.match(r"\(\s*declare-fun\s+([^\s\)]+)\s*\(([^\)]*)\)\s+([^\s\)]+)\s*\)", cmd)
            if m:
                name, args, sort = m.group(1), m.group(2).strip(), m.group(3)
                if args == "" and sort in {TYPE_INT, TYPE_REAL, TYPE_BOOL}:
                    symtab[name] = sort
                continue
        self._enforce_symbol_limit(symtab)

    def _build_z3_vars(self, symbol_table: Optional[Dict[str, str]] = None) -> Dict[str, z3.ExprRef]:
        z3_vars: Dict[str, z3.ExprRef] = {}
        table = symbol_table if symbol_table is not None else self.store.data.get("symbols", {})
        for name, sort in table.items():
            if sort == TYPE_BOOL:
                z3_vars[name] = z3.Bool(name)
            elif sort == TYPE_REAL:
                z3_vars[name] = z3.Real(name)
            else:
                z3_vars[name] = z3.Int(name)
        return z3_vars

    def _ensure_var(self, name: str, z3_vars: Dict[str, z3.ExprRef], symbol_table: Dict[str, str]) -> z3.ExprRef:
        if name in z3_vars:
            return z3_vars[name]
        sort = symbol_table.get(name, TYPE_INT)
        if sort == TYPE_BOOL:
            z3_vars[name] = z3.Bool(name)
        elif sort == TYPE_REAL:
            z3_vars[name] = z3.Real(name)
        else:
            z3_vars[name] = z3.Int(name)
        return z3_vars[name]

    def _compile_pyexpr(
        self,
        expr: str,
        z3_vars: Dict[str, z3.ExprRef],
        symbol_table: Dict[str, str],
    ) -> Tuple[ConstraintBundle, List[str]]:
        compiler = PyExprCompiler(symbol_table)
        z3_expr, symbols_used = compiler.compile(expr, z3_vars)
        label = f"pyexpr_{hash(expr)}"
        bundle = ConstraintBundle(
            item_id=label,
            assertions=[z3_expr],
            full_expr=z3_expr,
            labels=[label],
            symbols_used=symbols_used,
        )
        return bundle, symbols_used

    def _compile_smt2(
        self,
        item_id: str,
        content: Any,
        z3_vars: Dict[str, z3.ExprRef],
        symbol_table: Dict[str, str],
    ) -> ConstraintBundle:
        smt2 = self._normalize_smt2(content)
        smt2 = self._strip_comments(smt2)
        self._validate_smt2_commands(smt2)
        self._update_symbols_from_smt2(smt2, symbol_table)
        self._enforce_symbol_limit(symbol_table)
        for name in symbol_table:
            self._ensure_var(name, z3_vars, symbol_table)
        declared_names = self._declared_names_in_smt2(smt2)
        decls = {name: var for name, var in z3_vars.items() if name not in declared_names}
        try:
            exprs = list(z3.parse_smt2_string(smt2, decls=decls))
        except z3.Z3Exception as exc:
            raise LogicError("E_PARSE_ERROR", f"Invalid smt2: {exc}")
        if not exprs:
            exprs = [z3.BoolVal(True)]
        cmds = self._split_commands(smt2)
        labels: List[Optional[str]] = []
        for cmd in cmds:
            if cmd.lstrip().startswith("(assert"):
                m = re.search(r":named\s+([^\s\)]+)", cmd)
                labels.append(m.group(1) if m else None)
        tracked_labels: List[str] = []
        for idx, expr in enumerate(exprs):
            label = labels[idx] if idx < len(labels) and labels[idx] else f"{item_id}__{idx}"
            tracked_labels.append(label)
        full_expr = z3.And(*exprs)
        return ConstraintBundle(item_id=item_id, assertions=exprs, full_expr=full_expr, labels=tracked_labels, symbols_used=[])

    def _summarize(self, content: Any) -> str:
        if isinstance(content, list):
            text = " ".join(str(line) for line in content)
        else:
            text = str(content)
        text = text.strip().replace("\n", " ")
        if len(text) > 120:
            return text[:117] + "..."
        return text

    def _ensure_context_root(self, data: dict) -> None:
        context = data.get("context")
        if not isinstance(context, dict):
            context = {}
            data["context"] = context
        concepts = context.get("concepts")
        if not isinstance(concepts, dict):
            context["concepts"] = {}
        code_bindings = context.get("code_bindings")
        if not isinstance(code_bindings, dict):
            context["code_bindings"] = {}

    def _active_items_from_data(self, data: dict, kind: str) -> Dict[str, dict]:
        items = data.get(kind, {})
        if not isinstance(items, dict):
            return {}
        active: Dict[str, dict] = {}
        for item_id, versions in items.items():
            if not isinstance(versions, list):
                continue
            for entry in reversed(versions):
                if isinstance(entry, dict) and entry.get("enabled"):
                    active[item_id] = entry
                    break
        return active

    def _all_id_owners(self, data: dict) -> Dict[str, str]:
        self._ensure_context_root(data)
        owners: Dict[str, str] = {}
        contexts = data["context"]

        def add_owner(item_id: str, owner: str) -> None:
            if item_id in owners and owners[item_id] != owner:
                raise LogicError(
                    "E_INVALID_REQUEST",
                    "Global id conflict across item types",
                    {"id": item_id, "owners": sorted({owners[item_id], owner})},
                )
            owners[item_id] = owner

        for kind in ("bundles", "rules", "expectations"):
            items = data.get(kind, {})
            if not isinstance(items, dict):
                continue
            for item_id in items:
                add_owner(item_id, kind[:-1] if kind.endswith("s") else kind)

        for item_id in contexts["concepts"]:
            add_owner(item_id, "concept")
        for item_id in contexts["code_bindings"]:
            add_owner(item_id, "code_binding")
        return owners

    def _assert_id_available(self, data: dict, item_id: str, owner: str) -> None:
        owners = self._all_id_owners(data)
        existing = owners.get(item_id)
        if existing is not None and existing != owner:
            raise LogicError(
                "E_INVALID_REQUEST",
                "Global id conflict across item types",
                {"id": item_id, "owner": existing},
            )

    def _next_version(self, versions: List[dict]) -> int:
        if not versions:
            return 1
        return max(int(v.get("version", 0)) for v in versions) + 1

    def _active_version_entry(self, data: dict, kind: str, item_id: str) -> dict:
        items = data.get(kind, {})
        if not isinstance(items, dict):
            raise LogicError("E_UNKNOWN_ID", f"{kind} id does not exist", {"id": item_id})
        versions = items.get(item_id)
        if not isinstance(versions, list) or not versions:
            raise LogicError("E_UNKNOWN_ID", f"{kind} id does not exist", {"id": item_id})
        for entry in reversed(versions):
            if isinstance(entry, dict) and entry.get("enabled"):
                return entry
        raise LogicError("E_UNKNOWN_ID", f"{kind} id is already removed", {"id": item_id})

    def _versioned_set(self, data: dict, kind: str, item_id: str, *, lang: str, content: Any) -> None:
        items = data.setdefault(kind, {})
        if not isinstance(items, dict):
            raise LogicError("E_INVALID_REQUEST", f"{kind} store is corrupted")
        versions = items.setdefault(item_id, [])
        if not isinstance(versions, list):
            raise LogicError("E_INVALID_REQUEST", f"{kind} id store is corrupted", {"id": item_id})
        for entry in versions:
            if isinstance(entry, dict):
                entry["enabled"] = False
        versions.append(
            {
                "id": item_id,
                "version": self._next_version(versions),
                "enabled": True,
                "lang": lang,
                "content": content,
                "created_at": time.time(),
            }
        )

    def _versioned_remove(self, data: dict, kind: str, item_id: str) -> None:
        items = data.get(kind, {})
        if not isinstance(items, dict):
            raise LogicError("E_UNKNOWN_ID", f"{kind} id does not exist", {"id": item_id})
        versions = items.get(item_id)
        if not isinstance(versions, list) or not versions:
            raise LogicError("E_UNKNOWN_ID", f"{kind} id does not exist", {"id": item_id})
        for entry in reversed(versions):
            if isinstance(entry, dict) and entry.get("enabled"):
                entry["enabled"] = False
                return
        raise LogicError("E_UNKNOWN_ID", f"{kind} id is already removed", {"id": item_id})

    def _normalize_string_list(self, value: Any, field: str, *, allow_empty: bool = True) -> List[str]:
        if not isinstance(value, list):
            raise LogicError("E_INVALID_REQUEST", f"{field} must be an array")
        out: List[str] = []
        for item in value:
            if not isinstance(item, str) or not item:
                raise LogicError("E_INVALID_REQUEST", f"{field} values must be non-empty strings")
            if item not in out:
                out.append(item)
        if not allow_empty and not out:
            raise LogicError("E_INVALID_REQUEST", f"{field} must not be empty")
        return out

    def _normalize_rel_path(self, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise LogicError("E_INVALID_REQUEST", "path must be a non-empty string")
        path = value.strip().replace("\\", "/")
        while "//" in path:
            path = path.replace("//", "/")
        if path.startswith("/"):
            raise LogicError("E_INVALID_REQUEST", "path must be relative")
        parts = [part for part in path.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise LogicError("E_INVALID_REQUEST", "path must not contain '..'")
        if not parts:
            raise LogicError("E_INVALID_REQUEST", "path must not resolve to empty")
        return "/".join(parts)

    def _normalize_anchor(self, value: Any) -> dict:
        if not isinstance(value, dict):
            raise LogicError("E_INVALID_REQUEST", "anchor must be an object")
        out: dict = {}
        if "line" in value:
            line = value["line"]
            if not isinstance(line, int) or line < 1:
                raise LogicError("E_INVALID_REQUEST", "anchor.line must be an integer >= 1")
            out["line"] = line
        if "byte_start" in value:
            start = value["byte_start"]
            if not isinstance(start, int) or start < 0:
                raise LogicError("E_INVALID_REQUEST", "anchor.byte_start must be an integer >= 0")
            out["byte_start"] = start
        if "byte_end" in value:
            end = value["byte_end"]
            if not isinstance(end, int) or end < 0:
                raise LogicError("E_INVALID_REQUEST", "anchor.byte_end must be an integer >= 0")
            out["byte_end"] = end
        if "byte_start" in out and "byte_end" in out and out["byte_end"] < out["byte_start"]:
            raise LogicError("E_INVALID_REQUEST", "anchor.byte_end must be >= anchor.byte_start")
        if "text" in value:
            text = value["text"]
            if not isinstance(text, str):
                raise LogicError("E_INVALID_REQUEST", "anchor.text must be a string")
            if text:
                out["text"] = text
        if out and "line" not in out:
            raise LogicError("E_INVALID_REQUEST", "anchor.line is required when anchor is provided")
        return out

    def _strip_empty(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            out = {}
            for key, value in payload.items():
                normalized = self._strip_empty(value)
                if normalized is None:
                    continue
                if normalized == "":
                    continue
                if isinstance(normalized, (list, dict)) and not normalized:
                    continue
                out[key] = normalized
            return out
        if isinstance(payload, list):
            return [self._strip_empty(item) for item in payload]
        return payload

    def _binding_has_anchor_path(
        self,
        binding_id: str,
        bindings: Dict[str, dict],
        concepts: Dict[str, dict],
    ) -> bool:
        pending: List[tuple[str, str]] = [("binding", binding_id)]
        seen: set[tuple[str, str]] = set()
        while pending:
            node_type, node_id = pending.pop()
            key = (node_type, node_id)
            if key in seen:
                continue
            seen.add(key)
            if node_type == "binding":
                binding = bindings[node_id]
                if binding.get("related_rule_ids") or binding.get("related_expectation_ids"):
                    return True
                for concept_id in binding.get("related_concept_ids", []):
                    if concept_id in concepts:
                        pending.append(("concept", concept_id))
            else:
                concept = concepts[node_id]
                if concept.get("related_rule_ids") or concept.get("related_expectation_ids"):
                    return True
                for other_binding_id in concept.get("related_code_binding_ids", []):
                    if other_binding_id in bindings:
                        pending.append(("binding", other_binding_id))
        return False

    def _validate_constraints_for_data(self, data: dict) -> None:
        self._ensure_context_root(data)
        self._all_id_owners(data)

        active_rules = self._active_items_from_data(data, "rules")
        active_expectations = self._active_items_from_data(data, "expectations")
        concepts = data["context"]["concepts"]
        bindings = data["context"]["code_bindings"]

        # Expectation integrity: must be rule-to-rule only in v5.
        for exp_id, entry in active_expectations.items():
            content = entry.get("content")
            if not isinstance(content, dict):
                raise LogicError("E_INVALID_REQUEST", "Expectation content must be an object", {"id": exp_id})
            kind = content.get("kind")
            a_ref = content.get("a_ref")
            b_ref = content.get("b_ref")
            if kind not in {"entails", "equivalent"}:
                raise LogicError("E_INVALID_REQUEST", "Unsupported expectation kind", {"id": exp_id, "kind": kind})
            if not isinstance(a_ref, str) or not a_ref or not isinstance(b_ref, str) or not b_ref:
                raise LogicError("E_INVALID_REQUEST", "Expectation must include a_ref and b_ref", {"id": exp_id})
            if a_ref not in active_rules or b_ref not in active_rules:
                raise LogicError(
                    "E_INVALID_REQUEST",
                    "Expectation references missing active rule",
                    {"id": exp_id, "a_ref": a_ref, "b_ref": b_ref},
                )

        # Concepts
        for concept_id, concept in concepts.items():
            if not isinstance(concept, dict):
                raise LogicError("E_INVALID_REQUEST", "Concept entry must be an object", {"id": concept_id})
            if concept.get("id") != concept_id:
                raise LogicError("E_INVALID_REQUEST", "Concept id mismatch", {"id": concept_id})
            for key in ("concept", "meaning"):
                if not isinstance(concept.get(key), str) or not concept.get(key):
                    raise LogicError("E_INVALID_REQUEST", f"Concept.{key} is required", {"id": concept_id})
            for key in ("primary_symbols", "related_rule_ids", "related_expectation_ids", "related_code_binding_ids"):
                if not isinstance(concept.get(key), list):
                    raise LogicError("E_INVALID_REQUEST", f"Concept.{key} must be an array", {"id": concept_id})

            if not (
                concept.get("related_rule_ids")
                or concept.get("related_expectation_ids")
                or concept.get("related_code_binding_ids")
            ):
                raise LogicError(
                    "E_INVALID_REQUEST",
                    "Concept must reference at least one rule, expectation, or code binding",
                    {"id": concept_id},
                )

            for rule_id in concept.get("related_rule_ids", []):
                if rule_id not in active_rules:
                    raise LogicError("E_INVALID_REQUEST", "Concept has dangling rule reference", {"id": concept_id, "ref": rule_id})
            for exp_id in concept.get("related_expectation_ids", []):
                if exp_id not in active_expectations:
                    raise LogicError(
                        "E_INVALID_REQUEST",
                        "Concept has dangling expectation reference",
                        {"id": concept_id, "ref": exp_id},
                    )
            for binding_id in concept.get("related_code_binding_ids", []):
                if binding_id not in bindings:
                    raise LogicError(
                        "E_INVALID_REQUEST",
                        "Concept has dangling code binding reference",
                        {"id": concept_id, "ref": binding_id},
                    )

        # Code bindings
        for binding_id, binding in bindings.items():
            if not isinstance(binding, dict):
                raise LogicError("E_INVALID_REQUEST", "Code binding entry must be an object", {"id": binding_id})
            if binding.get("id") != binding_id:
                raise LogicError("E_INVALID_REQUEST", "Code binding id mismatch", {"id": binding_id})
            path = binding.get("path")
            if not isinstance(path, str) or not path:
                raise LogicError("E_INVALID_REQUEST", "Code binding.path is required", {"id": binding_id})
            normalized_path = self._normalize_rel_path(path)
            if normalized_path != path:
                raise LogicError("E_INVALID_REQUEST", "Code binding.path must be normalized", {"id": binding_id})
            kind = binding.get("kind", "source")
            if kind not in {"source", "document"}:
                raise LogicError("E_INVALID_REQUEST", "Code binding.kind must be source|document", {"id": binding_id})
            for key in ("related_rule_ids", "related_expectation_ids", "related_concept_ids"):
                if not isinstance(binding.get(key), list):
                    raise LogicError("E_INVALID_REQUEST", f"Code binding.{key} must be an array", {"id": binding_id})
            if "symbols_used" in binding and not isinstance(binding.get("symbols_used"), list):
                raise LogicError("E_INVALID_REQUEST", "Code binding.symbols_used must be an array", {"id": binding_id})
            if "anchor" in binding and not isinstance(binding.get("anchor"), dict):
                raise LogicError("E_INVALID_REQUEST", "Code binding.anchor must be an object", {"id": binding_id})

            if not (
                binding.get("related_rule_ids")
                or binding.get("related_expectation_ids")
                or binding.get("related_concept_ids")
            ):
                raise LogicError(
                    "E_INVALID_REQUEST",
                    "Code binding must reference at least one rule, expectation, or concept",
                    {"id": binding_id},
                )

            for rule_id in binding.get("related_rule_ids", []):
                if rule_id not in active_rules:
                    raise LogicError(
                        "E_INVALID_REQUEST",
                        "Code binding has dangling rule reference",
                        {"id": binding_id, "ref": rule_id},
                    )
            for exp_id in binding.get("related_expectation_ids", []):
                if exp_id not in active_expectations:
                    raise LogicError(
                        "E_INVALID_REQUEST",
                        "Code binding has dangling expectation reference",
                        {"id": binding_id, "ref": exp_id},
                    )
            for concept_id in binding.get("related_concept_ids", []):
                if concept_id not in concepts:
                    raise LogicError(
                        "E_INVALID_REQUEST",
                        "Code binding has dangling concept reference",
                        {"id": binding_id, "ref": concept_id},
                    )

            if not self._binding_has_anchor_path(binding_id, bindings, concepts):
                raise LogicError(
                    "E_INVALID_REQUEST",
                    "Code binding must have a transitive path to a rule or expectation",
                    {"id": binding_id},
                )

        # No pure abstraction component without any logic anchor.
        graph: Dict[tuple[str, str], set[tuple[str, str]]] = {}
        for concept_id, concept in concepts.items():
            node = ("concept", concept_id)
            graph.setdefault(node, set())
            for binding_id in concept.get("related_code_binding_ids", []):
                bnode = ("binding", binding_id)
                graph[node].add(bnode)
                graph.setdefault(bnode, set()).add(node)
        for binding_id, binding in bindings.items():
            node = ("binding", binding_id)
            graph.setdefault(node, set())
            for concept_id in binding.get("related_concept_ids", []):
                cnode = ("concept", concept_id)
                graph[node].add(cnode)
                graph.setdefault(cnode, set()).add(node)

        visited: set[tuple[str, str]] = set()
        for start in graph:
            if start in visited:
                continue
            stack = [start]
            component: List[tuple[str, str]] = []
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                component.append(node)
                for nxt in graph.get(node, set()):
                    if nxt not in visited:
                        stack.append(nxt)
            anchored = False
            for node_type, node_id in component:
                if node_type == "concept":
                    concept = concepts[node_id]
                    if concept.get("related_rule_ids") or concept.get("related_expectation_ids"):
                        anchored = True
                        break
                else:
                    binding = bindings[node_id]
                    if binding.get("related_rule_ids") or binding.get("related_expectation_ids"):
                        anchored = True
                        break
            if not anchored and component:
                raise LogicError(
                    "E_INVALID_REQUEST",
                    "Pure abstraction component without rule/expectation anchor",
                    {"component_size": len(component)},
                )

    def _apply_persistent_mutation(self, mutator) -> None:
        working = copy.deepcopy(self.store.data)
        self._ensure_context_root(working)
        mutator(working)
        self._validate_constraints_for_data(working)
        self.store.data = working
        self.store.save()
        self._invalidate_cache()

    def set_rule(self, args: dict) -> dict:
        item_id = args.get("id")
        lang = args.get("lang")
        rule = args.get("rule")
        if not isinstance(item_id, str) or not item_id:
            raise LogicError("E_INVALID_REQUEST", "Missing required field id")
        if lang not in {"pyexpr", "smt2"}:
            raise LogicError("E_INVALID_REQUEST", "Unsupported lang", {"lang": lang})
        if rule is None:
            raise LogicError("E_INVALID_REQUEST", "Missing required field rule")

        def mutate(data: dict) -> None:
            self._assert_id_available(data, item_id, "rule")
            symbols = data.setdefault("symbols", {})
            if not isinstance(symbols, dict):
                raise LogicError("E_INVALID_REQUEST", "symbols store is corrupted")
            z3_vars = self._build_z3_vars(symbols)
            if lang == "pyexpr":
                self._compile_pyexpr(rule, z3_vars, symbols)
            else:
                self._compile_smt2(item_id, rule, z3_vars, symbols)
            self._versioned_set(data, "rules", item_id, lang=lang, content=rule)

        self._apply_persistent_mutation(mutate)
        return {"ok": True}

    def remove_rule(self, args: dict) -> dict:
        item_id = args.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise LogicError("E_INVALID_REQUEST", "Missing required field id")

        def mutate(data: dict) -> None:
            self._versioned_remove(data, "rules", item_id)

        self._apply_persistent_mutation(mutate)
        return {"ok": True}

    def set_bundle(self, args: dict) -> dict:
        item_id = args.get("id")
        bundle = args.get("bundle")
        if not isinstance(item_id, str) or not item_id:
            raise LogicError("E_INVALID_REQUEST", "Missing required field id")
        if bundle is None:
            raise LogicError("E_INVALID_REQUEST", "Missing required field bundle")

        def mutate(data: dict) -> None:
            self._assert_id_available(data, item_id, "bundle")
            symbols = data.setdefault("symbols", {})
            if not isinstance(symbols, dict):
                raise LogicError("E_INVALID_REQUEST", "symbols store is corrupted")
            z3_vars = self._build_z3_vars(symbols)
            self._compile_smt2(item_id, bundle, z3_vars, symbols)
            self._versioned_set(data, "bundles", item_id, lang="smt2", content=bundle)

        self._apply_persistent_mutation(mutate)
        return {"ok": True}

    def remove_bundle(self, args: dict) -> dict:
        item_id = args.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise LogicError("E_INVALID_REQUEST", "Missing required field id")

        def mutate(data: dict) -> None:
            self._versioned_remove(data, "bundles", item_id)

        self._apply_persistent_mutation(mutate)
        return {"ok": True}

    def set_expectation(self, args: dict) -> dict:
        item_id = args.get("id")
        kind = args.get("kind")
        a_ref = args.get("a_ref")
        b_ref = args.get("b_ref")
        if not isinstance(item_id, str) or not item_id:
            raise LogicError("E_INVALID_REQUEST", "Missing required field id")
        if kind not in {"entails", "equivalent"}:
            raise LogicError("E_INVALID_REQUEST", "Unsupported expectation kind", {"kind": kind})
        if not isinstance(a_ref, str) or not a_ref or not isinstance(b_ref, str) or not b_ref:
            raise LogicError("E_INVALID_REQUEST", "Expectation requires a_ref and b_ref")

        def mutate(data: dict) -> None:
            self._assert_id_available(data, item_id, "expectation")
            active_rules = self._active_items_from_data(data, "rules")
            if a_ref not in active_rules or b_ref not in active_rules:
                raise LogicError(
                    "E_INVALID_REQUEST",
                    "Expectation references missing active rule",
                    {"a_ref": a_ref, "b_ref": b_ref},
                )
            content = {"kind": kind, "a_ref": a_ref, "b_ref": b_ref}
            self._versioned_set(data, "expectations", item_id, lang="expect", content=content)

        self._apply_persistent_mutation(mutate)
        return {"ok": True}

    def remove_expectation(self, args: dict) -> dict:
        item_id = args.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise LogicError("E_INVALID_REQUEST", "Missing required field id")

        def mutate(data: dict) -> None:
            self._versioned_remove(data, "expectations", item_id)

        self._apply_persistent_mutation(mutate)
        return {"ok": True}

    def context_patch(self, args: dict) -> dict:
        ops = args.get("ops")
        if not isinstance(ops, list):
            raise LogicError("E_INVALID_REQUEST", "ops must be an array")

        def mutate(data: dict) -> None:
            self._ensure_context_root(data)
            concepts = data["context"]["concepts"]
            bindings = data["context"]["code_bindings"]
            if not isinstance(concepts, dict) or not isinstance(bindings, dict):
                raise LogicError("E_INVALID_REQUEST", "context store is corrupted")

            for op in ops:
                if not isinstance(op, dict):
                    raise LogicError("E_INVALID_REQUEST", "Each op must be an object")
                op_name = op.get("op")
                item_id = op.get("id")
                if not isinstance(op_name, str) or not isinstance(item_id, str) or not item_id:
                    raise LogicError("E_INVALID_REQUEST", "Each op requires non-empty op and id")

                if op_name == "set_concept":
                    self._assert_id_available(data, item_id, "concept")
                    set_payload = op.get("set")
                    if not isinstance(set_payload, dict):
                        raise LogicError("E_INVALID_REQUEST", "set_concept requires set object", {"id": item_id})
                    current = copy.deepcopy(concepts.get(item_id, {"id": item_id}))
                    current["id"] = item_id
                    for key, value in set_payload.items():
                        if value is None:
                            current.pop(key, None)
                        else:
                            current[key] = value
                    required = ("concept", "meaning", "primary_symbols", "related_rule_ids", "related_expectation_ids", "related_code_binding_ids")
                    for field in required:
                        if field not in current:
                            raise LogicError("E_INVALID_REQUEST", f"Concept missing required field {field}", {"id": item_id})
                    if not isinstance(current["concept"], str) or not current["concept"]:
                        raise LogicError("E_INVALID_REQUEST", "Concept.concept must be non-empty string", {"id": item_id})
                    if not isinstance(current["meaning"], str) or not current["meaning"]:
                        raise LogicError("E_INVALID_REQUEST", "Concept.meaning must be non-empty string", {"id": item_id})
                    normalized = {
                        "id": item_id,
                        "concept": current["concept"],
                        "meaning": current["meaning"],
                        "primary_symbols": self._normalize_string_list(current["primary_symbols"], "primary_symbols"),
                        "related_rule_ids": self._normalize_string_list(current["related_rule_ids"], "related_rule_ids"),
                        "related_expectation_ids": self._normalize_string_list(
                            current["related_expectation_ids"], "related_expectation_ids"
                        ),
                        "related_code_binding_ids": self._normalize_string_list(
                            current["related_code_binding_ids"], "related_code_binding_ids"
                        ),
                    }
                    if "source_ref" in current and current["source_ref"] is not None:
                        if not isinstance(current["source_ref"], str):
                            raise LogicError("E_INVALID_REQUEST", "Concept.source_ref must be a string", {"id": item_id})
                        if current["source_ref"]:
                            normalized["source_ref"] = current["source_ref"]
                    concepts[item_id] = normalized
                elif op_name == "remove_concept":
                    if item_id not in concepts:
                        raise LogicError("E_UNKNOWN_ID", "Concept id does not exist", {"id": item_id})
                    concepts.pop(item_id, None)
                elif op_name == "set_code_binding":
                    self._assert_id_available(data, item_id, "code_binding")
                    set_payload = op.get("set")
                    if not isinstance(set_payload, dict):
                        raise LogicError("E_INVALID_REQUEST", "set_code_binding requires set object", {"id": item_id})
                    current = copy.deepcopy(bindings.get(item_id, {"id": item_id}))
                    current["id"] = item_id
                    for key, value in set_payload.items():
                        if value is None:
                            current.pop(key, None)
                        else:
                            current[key] = value
                    required = ("path", "related_rule_ids", "related_expectation_ids", "related_concept_ids")
                    for field in required:
                        if field not in current:
                            raise LogicError(
                                "E_INVALID_REQUEST",
                                f"Code binding missing required field {field}",
                                {"id": item_id},
                            )
                    normalized = {
                        "id": item_id,
                        "path": self._normalize_rel_path(current["path"]),
                        "kind": current.get("kind", "source"),
                        "related_rule_ids": self._normalize_string_list(current["related_rule_ids"], "related_rule_ids"),
                        "related_expectation_ids": self._normalize_string_list(
                            current["related_expectation_ids"], "related_expectation_ids"
                        ),
                        "related_concept_ids": self._normalize_string_list(
                            current["related_concept_ids"], "related_concept_ids"
                        ),
                    }
                    if normalized["kind"] not in {"source", "document"}:
                        raise LogicError("E_INVALID_REQUEST", "Code binding.kind must be source|document", {"id": item_id})
                    if "function_or_behavior" in current and current["function_or_behavior"] is not None:
                        if not isinstance(current["function_or_behavior"], str):
                            raise LogicError("E_INVALID_REQUEST", "function_or_behavior must be a string", {"id": item_id})
                        if current["function_or_behavior"]:
                            normalized["function_or_behavior"] = current["function_or_behavior"]
                    if "symbols_used" in current and current["symbols_used"] is not None:
                        normalized["symbols_used"] = self._normalize_string_list(current["symbols_used"], "symbols_used")
                    if "anchor" in current and current["anchor"] is not None:
                        anchor = self._normalize_anchor(current["anchor"])
                        if anchor:
                            normalized["anchor"] = anchor
                    bindings[item_id] = normalized
                elif op_name == "remove_code_binding":
                    if item_id not in bindings:
                        raise LogicError("E_UNKNOWN_ID", "Code binding id does not exist", {"id": item_id})
                    bindings.pop(item_id, None)
                elif op_name == "set_rule_meta":
                    set_payload = op.get("set")
                    if not isinstance(set_payload, dict):
                        raise LogicError("E_INVALID_REQUEST", "set_rule_meta requires set object", {"id": item_id})
                    entry = self._active_version_entry(data, "rules", item_id)
                    meta = entry.get("meta")
                    if not isinstance(meta, dict):
                        meta = {}
                    for key, value in set_payload.items():
                        if value is None:
                            meta.pop(key, None)
                        else:
                            meta[key] = value
                    if meta:
                        entry["meta"] = meta
                    else:
                        entry.pop("meta", None)
                elif op_name == "set_expectation_meta":
                    set_payload = op.get("set")
                    if not isinstance(set_payload, dict):
                        raise LogicError("E_INVALID_REQUEST", "set_expectation_meta requires set object", {"id": item_id})
                    entry = self._active_version_entry(data, "expectations", item_id)
                    meta = entry.get("meta")
                    if not isinstance(meta, dict):
                        meta = {}
                    for key, value in set_payload.items():
                        if value is None:
                            meta.pop(key, None)
                        else:
                            meta[key] = value
                    if meta:
                        entry["meta"] = meta
                    else:
                        entry.pop("meta", None)
                else:
                    raise LogicError("E_INVALID_REQUEST", f"Unknown context op {op_name}")

        self._apply_persistent_mutation(mutate)
        return {"ok": True}

    def _render_list_item(self, item_type: str, item_id: str, payload: dict, detail_level: str) -> dict:
        if item_type in {"bundle", "rule"}:
            item = {"id": item_id, "type": item_type}
            if detail_level == "minimal":
                item["lang"] = payload.get("lang")
            elif detail_level in {"compact", "more"}:
                item["lang"] = payload.get("lang")
                item["summary"] = self._summarize(payload.get("content"))
            else:
                item["lang"] = payload.get("lang")
                item["version"] = payload.get("version")
                item["content"] = payload.get("content")
                if isinstance(payload.get("meta"), dict):
                    item["meta"] = payload["meta"]
            return self._strip_empty(item)

        if item_type == "expectation":
            content = payload.get("content", {})
            item = {"id": item_id, "type": "expectation", "kind": content.get("kind")}
            if detail_level in {"compact", "more", "full"}:
                item["a_ref"] = content.get("a_ref")
                item["b_ref"] = content.get("b_ref")
            if detail_level == "full":
                item["version"] = payload.get("version")
                if isinstance(payload.get("meta"), dict):
                    item["meta"] = payload["meta"]
            return self._strip_empty(item)

        if item_type == "concept":
            if detail_level == "minimal":
                return self._strip_empty({"id": item_id, "type": "concept", "concept": payload.get("concept")})
            if detail_level == "compact":
                return self._strip_empty(
                    {
                        "id": item_id,
                        "type": "concept",
                        "concept": payload.get("concept"),
                        "meaning": payload.get("meaning"),
                    }
                )
            if detail_level == "more":
                return self._strip_empty(
                    {
                        "id": item_id,
                        "type": "concept",
                        "concept": payload.get("concept"),
                        "meaning": payload.get("meaning"),
                        "related_rule_ids": payload.get("related_rule_ids"),
                        "related_expectation_ids": payload.get("related_expectation_ids"),
                        "related_code_binding_ids": payload.get("related_code_binding_ids"),
                    }
                )
            return self._strip_empty({"id": item_id, "type": "concept", **payload})

        # code_binding
        if detail_level == "minimal":
            return self._strip_empty({"id": item_id, "type": "code_binding", "path": payload.get("path")})
        if detail_level == "compact":
            return self._strip_empty(
                {
                    "id": item_id,
                    "type": "code_binding",
                    "path": payload.get("path"),
                    "kind": payload.get("kind", "source"),
                    "function_or_behavior": payload.get("function_or_behavior"),
                }
            )
        if detail_level == "more":
            return self._strip_empty(
                {
                    "id": item_id,
                    "type": "code_binding",
                    "path": payload.get("path"),
                    "kind": payload.get("kind", "source"),
                    "function_or_behavior": payload.get("function_or_behavior"),
                    "related_rule_ids": payload.get("related_rule_ids"),
                    "related_expectation_ids": payload.get("related_expectation_ids"),
                    "related_concept_ids": payload.get("related_concept_ids"),
                }
            )
        return self._strip_empty({"id": item_id, "type": "code_binding", **payload})

    def _item_universe(self) -> Dict[str, tuple[str, dict]]:
        self._ensure_context_root(self.store.data)
        active_bundles = self.store.get_active_items("bundles")
        active_rules = self.store.get_active_items("rules")
        active_expectations = self.store.get_active_items("expectations")
        concepts = self.store.data["context"]["concepts"]
        bindings = self.store.data["context"]["code_bindings"]

        universe: Dict[str, tuple[str, dict]] = {}
        for key, value in active_bundles.items():
            universe[key] = ("bundle", value)
        for key, value in active_rules.items():
            universe[key] = ("rule", value)
        for key, value in active_expectations.items():
            universe[key] = ("expectation", value)
        for key, value in concepts.items():
            universe[key] = ("concept", value)
        for key, value in bindings.items():
            universe[key] = ("code_binding", value)
        return universe

    def read_item(self, args: dict) -> dict:
        item_id = args.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise LogicError("E_INVALID_REQUEST", "Missing required field id")

        detail_level = args.get("detail_level", "full")
        if detail_level not in ITEM_DETAIL_LEVEL_VALUES:
            raise LogicError("E_INVALID_REQUEST", "detail_level must be one of minimal|compact|more|full")

        universe = self._item_universe()
        if item_id not in universe:
            raise LogicError("E_UNKNOWN_ID", "id does not exist", {"id": item_id})
        entry_type, payload = universe[item_id]
        return {"ok": True, "result": {"item": self._render_list_item(entry_type, item_id, payload, detail_level)}}

    def list_items(self, args: dict) -> dict:
        detail_level = args.get("detail_level", "compact")
        if detail_level not in LIST_DETAIL_LEVEL_VALUES:
            raise LogicError("E_INVALID_REQUEST", "detail_level must be one of minimal|compact|more")
        show = args.get("show")
        cursor = args.get("cursor")
        limit_raw = args.get("limit", 50)
        if not isinstance(limit_raw, int) or limit_raw < 1:
            raise LogicError("E_INVALID_REQUEST", "limit must be an integer >= 1")
        limit = limit_raw

        if "id" in args:
            raise LogicError("E_INVALID_REQUEST", "logic_list does not support id; use logic_read")

        valid_show = {"all", "bundles", "rules", "expectations", "concepts", "code_bindings"}
        if show is None:
            show = ["all"]
        if not isinstance(show, list):
            raise LogicError("E_INVALID_REQUEST", "show must be an array")
        normalized_show: List[str] = []
        for item in show:
            if item not in valid_show:
                raise LogicError("E_INVALID_REQUEST", "show contains unsupported item", {"value": item})
            if item not in normalized_show:
                normalized_show.append(item)
        if "all" in normalized_show:
            normalized_show = ["bundles", "rules", "expectations", "concepts", "code_bindings"]

        universe = self._item_universe()
        selected: List[tuple[str, str, dict]] = []
        if "bundles" in normalized_show:
            selected.extend((item_type, item_id, payload) for item_id, (item_type, payload) in universe.items() if item_type == "bundle")
        if "rules" in normalized_show:
            selected.extend((item_type, item_id, payload) for item_id, (item_type, payload) in universe.items() if item_type == "rule")
        if "expectations" in normalized_show:
            selected.extend(
                (item_type, item_id, payload)
                for item_id, (item_type, payload) in universe.items()
                if item_type == "expectation"
            )
        if "concepts" in normalized_show:
            selected.extend((item_type, item_id, payload) for item_id, (item_type, payload) in universe.items() if item_type == "concept")
        if "code_bindings" in normalized_show:
            selected.extend(
                (item_type, item_id, payload)
                for item_id, (item_type, payload) in universe.items()
                if item_type == "code_binding"
            )

        selected.sort(key=lambda item: item[1])
        start = 0
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.isdigit():
                raise LogicError("E_INVALID_REQUEST", "cursor must be a decimal offset string")
            start = int(cursor)
        page = selected[start : start + limit]
        items = [self._render_list_item(item_type, item_id, payload, detail_level) for item_type, item_id, payload in page]
        result = {"items": items}
        next_idx = start + len(page)
        if next_idx < len(selected):
            result["next_cursor"] = str(next_idx)
        return {"ok": True, "result": result}

    def _check_expectation_failures(self, section: dict) -> dict:
        baseline = []
        candidate = []
        for exp_id, result in section.get("baseline", {}).items():
            if isinstance(result, dict) and result.get("status") == "fail":
                baseline.append(exp_id)
        for exp_id, result in section.get("candidate", {}).items():
            if isinstance(result, dict) and result.get("status") == "fail":
                candidate.append(exp_id)
        return self._strip_empty({"baseline": sorted(baseline), "candidate": sorted(candidate)})

    def check_v5(self, args: dict) -> dict:
        detail_level = args.get("detail_level", "compact")
        if detail_level not in {"minimal", "compact", "more", "full"}:
            raise LogicError("E_INVALID_REQUEST", "detail_level must be one of minimal|compact|more|full")

        hypothesis = args.get("hypothesis", {}) or {}
        if not isinstance(hypothesis, dict):
            raise LogicError("E_INVALID_REQUEST", "hypothesis must be an object")
        patch = hypothesis.get("patch", {}) or {}
        if not isinstance(patch, dict):
            raise LogicError("E_INVALID_REQUEST", "hypothesis.patch must be an object")
        set_rules = patch.get("set_rules", {}) or {}
        remove_rules = patch.get("remove_rules", []) or []
        if not isinstance(set_rules, dict):
            raise LogicError("E_INVALID_REQUEST", "hypothesis.patch.set_rules must be an object")
        if not isinstance(remove_rules, list):
            raise LogicError("E_INVALID_REQUEST", "hypothesis.patch.remove_rules must be a list")

        active_rules = self.store.get_active_items("rules")
        patch_add: Dict[str, dict] = {}
        patch_replace: Dict[str, dict] = {}
        remove_set = set()
        for rid in remove_rules:
            if not isinstance(rid, str):
                raise LogicError("E_INVALID_REQUEST", "remove_rules values must be rule ids")
            remove_set.add(rid)
        for rid, rule_patch in set_rules.items():
            if rid in remove_set:
                raise LogicError("E_INVALID_REQUEST", "A rule cannot be set and removed in the same check", {"id": rid})
            if not isinstance(rule_patch, dict):
                raise LogicError("E_INVALID_REQUEST", "set_rules entries must be objects", {"id": rid})
            lang = rule_patch.get("lang")
            content = rule_patch.get("rule")
            if lang not in {"pyexpr", "smt2"} or content is None:
                raise LogicError("E_INVALID_REQUEST", "Invalid set_rules entry", {"id": rid})
            if rid in active_rules:
                patch_replace[rid] = {"lang": lang, "rule": content}
            else:
                patch_add[rid] = {"lang": lang, "rule": content}

        options = {
            "timeout_ms": DEFAULT_TIMEOUT_MS,
            "fail_on_timeout": False,
            "check_expectations": None,
        }
        if detail_level == "minimal":
            options.update({"analyse_influence": False, "return_models": False, "return_unsat_core": False})
        elif detail_level == "compact":
            options.update({"analyse_influence": False, "return_models": False, "return_unsat_core": False})
        elif detail_level == "more":
            options.update({"analyse_influence": True, "return_models": False, "return_unsat_core": True})
        else:
            options.update({"analyse_influence": True, "return_models": True, "return_unsat_core": True})

        legacy_args = {
            "hypothesis": {
                "facts": hypothesis.get("facts", {}) or {},
                "patch": {
                    "add": patch_add,
                    "replace": patch_replace,
                    "delete": list(remove_set),
                },
            },
            "options": options,
        }
        full_response = self.check(legacy_args)
        result = full_response.get("result", {})
        out = {
            "baseline": {"status": result.get("baseline", {}).get("status")},
            "candidate": {"status": result.get("candidate", {}).get("status")},
            "breaks": result.get("breaks"),
        }
        if detail_level != "minimal":
            out["delta"] = result.get("delta", {})
            out["expectation_failures"] = self._check_expectation_failures(result.get("expectations", {}))
        if detail_level in {"more", "full"}:
            if isinstance(result.get("baseline"), dict) and "unsat_core" in result["baseline"]:
                out.setdefault("baseline", {})["unsat_core"] = result["baseline"]["unsat_core"]
            if isinstance(result.get("candidate"), dict) and "unsat_core" in result["candidate"]:
                out.setdefault("candidate", {})["unsat_core"] = result["candidate"]["unsat_core"]
            out["expectations"] = result.get("expectations", {})
            if isinstance(result.get("influence"), dict):
                out["influence"] = {"patch_influence": result["influence"].get("patch_influence")}
        if detail_level == "full":
            if isinstance(result.get("baseline"), dict) and "model" in result["baseline"]:
                out.setdefault("baseline", {})["model"] = result["baseline"]["model"]
            if isinstance(result.get("candidate"), dict) and "model" in result["candidate"]:
                out.setdefault("candidate", {})["model"] = result["candidate"]["model"]
            if isinstance(result.get("influence"), dict):
                out["influence"] = result["influence"]
        return {"ok": True, "result": self._strip_empty(out)}

    def _parse_fact(self, name: str, value: Any, local_symbols: Dict[str, str]) -> Tuple[Optional[z3.ExprRef], Optional[str]]:
        if isinstance(value, str) and value.startswith("?"):
            m = re.match(r"^\?[^:]*(:([A-Za-z]+))?$", value)
            if not m:
                raise LogicError("E_INVALID_REQUEST", f"Invalid placeholder for {name}")
            typ = m.group(2)
            if typ:
                if typ not in {TYPE_INT, TYPE_REAL, TYPE_BOOL}:
                    raise LogicError("E_INVALID_REQUEST", f"Unsupported placeholder type {typ}")
                existing = local_symbols.get(name)
                if existing and existing != typ:
                    raise LogicError(
                        "E_INVALID_REQUEST",
                        f"Placeholder type mismatch for {name}: expected {existing}, got {typ}",
                    )
                local_symbols.setdefault(name, typ)
            elif name in local_symbols:
                typ = local_symbols[name]
            else:
                local_symbols.setdefault(name, TYPE_INT)
            return None, "placeholder"
        if isinstance(value, bool):
            existing = local_symbols.get(name)
            if existing and existing != TYPE_BOOL:
                raise LogicError("E_INVALID_REQUEST", f"Fact type mismatch for {name}: expected {existing}, got Bool")
            local_symbols.setdefault(name, TYPE_BOOL)
            return z3.BoolVal(value), None
        if isinstance(value, int):
            existing = local_symbols.get(name)
            if existing and existing not in {TYPE_INT, TYPE_REAL}:
                raise LogicError("E_INVALID_REQUEST", f"Fact type mismatch for {name}: expected {existing}, got Int")
            local_symbols.setdefault(name, TYPE_INT)
            return z3.IntVal(value), None
        if isinstance(value, float):
            existing = local_symbols.get(name)
            if existing and existing == TYPE_BOOL:
                raise LogicError("E_INVALID_REQUEST", f"Fact type mismatch for {name}: expected Bool, got Real")
            local_symbols.setdefault(name, TYPE_REAL)
            return z3.RealVal(str(value)), None
        raise LogicError("E_INVALID_REQUEST", f"Unsupported fact value for {name}")

    def _model_to_json(self, model: z3.ModelRef, symbols: Iterable[str], z3_vars: Dict[str, z3.ExprRef]) -> dict:
        result: Dict[str, Any] = {}
        for name in symbols:
            if name not in z3_vars:
                continue
            val = model.eval(z3_vars[name], model_completion=True)
            if z3.is_bool(val):
                result[name] = bool(z3.is_true(val))
            elif z3.is_int_value(val):
                result[name] = int(val.as_long())
            elif z3.is_rational_value(val):
                if val.denominator_as_long() == 1:
                    result[name] = int(val.numerator_as_long())
                else:
                    result[name] = str(val)
            else:
                result[name] = str(val)
        return result

    def _compile_rule_bundle(
        self,
        rule_id: str,
        rule_entry: dict,
        z3_vars: Dict[str, z3.ExprRef],
        symbol_table: Dict[str, str],
    ) -> ConstraintBundle:
        if rule_entry["lang"] == "pyexpr":
            compiler = PyExprCompiler(symbol_table)
            expr, symbols_used = compiler.compile(rule_entry["content"], z3_vars)
            return ConstraintBundle(rule_id, [expr], expr, [rule_id], symbols_used)
        return self._compile_smt2(rule_id, rule_entry["content"], z3_vars, symbol_table)

    def _build_base_context(self, key: str, bundles: Dict[str, dict], rules: Dict[str, dict]) -> CachedBaseContext:
        symbol_table = dict(self.store.data.get("symbols", {}))
        self._enforce_symbol_limit(symbol_table)
        z3_vars = self._build_z3_vars(symbol_table)

        solver = z3.Solver()
        solver.set(unsat_core=True)

        bundle_assumptions: Dict[str, List[z3.BoolRef]] = {}
        rule_assumptions: Dict[str, List[z3.BoolRef]] = {}
        assumption_to_item: Dict[str, str] = {}

        for bundle_id, entry in bundles.items():
            if entry["lang"] != "smt2":
                continue
            bundle = self._compile_smt2(bundle_id, entry["content"], z3_vars, symbol_table)
            lits: List[z3.BoolRef] = []
            for idx, expr in enumerate(bundle.assertions):
                lit_name = self._assumption_name("bundle", bundle_id, idx)
                lit = z3.Bool(lit_name)
                solver.add(z3.Implies(lit, expr))
                assumption_to_item[lit_name] = bundle_id
                lits.append(lit)
            bundle_assumptions[bundle_id] = lits

        for rule_id, entry in rules.items():
            bundle = self._compile_rule_bundle(rule_id, entry, z3_vars, symbol_table)
            lits = []
            for idx, expr in enumerate(bundle.assertions):
                lit_name = self._assumption_name("rule", rule_id, idx)
                lit = z3.Bool(lit_name)
                solver.add(z3.Implies(lit, expr))
                assumption_to_item[lit_name] = rule_id
                lits.append(lit)
            rule_assumptions[rule_id] = lits

        return CachedBaseContext(
            key=key,
            solver=solver,
            symbol_table=symbol_table,
            z3_vars=z3_vars,
            bundle_assumptions=bundle_assumptions,
            rule_assumptions=rule_assumptions,
            assumption_to_item=assumption_to_item,
            active_bundles=dict(bundles),
            active_rules=dict(rules),
        )

    def _get_base_context(self, bundles: Dict[str, dict], rules: Dict[str, dict]) -> CachedBaseContext:
        key = self._context_key(bundles, rules)
        with self._cache_lock:
            if self._base_context is not None and self._base_context.key == key:
                return self._base_context
            self._base_context = self._build_base_context(key, bundles, rules)
            return self._base_context

    def _active_assumptions(self, context: CachedBaseContext, enabled_rule_ids: set[str]) -> List[z3.BoolRef]:
        assumptions: List[z3.BoolRef] = []
        for bundle_id in context.active_bundles:
            assumptions.extend(context.bundle_assumptions.get(bundle_id, []))
        for rule_id in context.active_rules:
            if rule_id in enabled_rule_ids:
                assumptions.extend(context.rule_assumptions.get(rule_id, []))
        return assumptions

    def _apply_facts(
        self,
        solver: z3.Solver,
        facts: Dict[str, Any],
        z3_vars: Dict[str, z3.ExprRef],
        symbol_table: Dict[str, str],
    ) -> List[str]:
        symbols_from_facts: List[str] = []
        for name, value in facts.items():
            expr, kind = self._parse_fact(name, value, symbol_table)
            self._ensure_var(name, z3_vars, symbol_table)
            symbols_from_facts.append(name)
            if kind == "placeholder" or expr is None:
                continue
            solver.add(z3_vars[name] == expr)
        self._enforce_symbol_limit(symbol_table)
        return symbols_from_facts

    def _check_solver(
        self,
        solver: z3.Solver,
        assumptions: Optional[List[z3.BoolRef]] = None,
    ) -> Tuple[str, Optional[str]]:
        try:
            if assumptions:
                res = solver.check(*assumptions)
            else:
                res = solver.check()
        except z3.Z3Exception as exc:
            raise LogicError("E_SOLVER_ERROR", str(exc))
        if res == z3.sat:
            return "sat", None
        if res == z3.unsat:
            return "unsat", None
        reason = solver.reason_unknown()
        return "unknown", reason or None

    def _is_timeout_reason(self, reason: Optional[str]) -> bool:
        if not reason:
            return False
        return reason in {"timeout", "canceled"}

    def _extract_unsat_core(self, solver: z3.Solver, tracked_to_item: Dict[str, str]) -> List[str]:
        try:
            core = solver.unsat_core()
        except Exception:
            return []
        ids = set()
        for entry in core:
            name = str(entry)
            item_id = tracked_to_item.get(name)
            if item_id:
                ids.add(item_id)
        return sorted(ids)

    def _compile_rule_expr(
        self,
        rule_entry: dict,
        z3_vars: Dict[str, z3.ExprRef],
        symbol_table: Dict[str, str],
    ) -> z3.BoolRef:
        return self._compile_rule_bundle(rule_entry["id"], rule_entry, z3_vars, symbol_table).full_expr

    def _get_default_facts(self) -> Dict[str, Any]:
        defaults = self.store.data.get("defaults", {})
        if not isinstance(defaults, dict):
            return {}
        facts: Dict[str, Any] = {}
        default_facts = defaults.get("facts")
        if isinstance(default_facts, dict):
            facts.update(default_facts)
        for key, value in defaults.items():
            if key in {"facts", "domains", "types", "preferred_types"}:
                continue
            if isinstance(value, (bool, int, float, str)):
                facts.setdefault(key, value)
        return facts

    def _check_expectations(
        self,
        expectations: Dict[str, dict],
        base_solver: z3.Solver,
        assumptions: List[z3.BoolRef],
        z3_vars: Dict[str, z3.ExprRef],
        active_rules: Dict[str, dict],
        symbol_table: Dict[str, str],
        *,
        fail_on_timeout: bool,
    ) -> Dict[str, ExpectationResult]:
        results: Dict[str, ExpectationResult] = {}
        local_symbols = dict(symbol_table)
        for exp_id, entry in expectations.items():
            expect = entry["content"]
            kind = expect.get("kind")
            if kind not in {"entails", "equivalent"}:
                results[exp_id] = ExpectationResult(status="unknown", reason="unsupported")
                continue
            try:
                # v5 shape: {"kind","a_ref","b_ref"}; keep legacy support for stored v4/v3 forms.
                a_ref = expect.get("a_ref")
                b_ref = expect.get("b_ref")
                if not a_ref:
                    a = expect.get("a", {})
                    a_ref = a.get("ref") if isinstance(a, dict) else None
                if not a_ref:
                    raise LogicError("E_INVALID_REQUEST", "Expectation missing a_ref")
                if a_ref in active_rules:
                    a_entry = active_rules[a_ref]
                else:
                    a_entry = self.store.get_latest_item("rules", a_ref)
                a_expr = self._compile_rule_expr({**a_entry, "id": a_ref}, z3_vars, local_symbols)

                if b_ref:
                    if b_ref in active_rules:
                        b_entry = active_rules[b_ref]
                    else:
                        b_entry = self.store.get_latest_item("rules", b_ref)
                    b_expr = self._compile_rule_expr({**b_entry, "id": b_ref}, z3_vars, local_symbols)
                else:
                    b = expect.get("b", {})
                    if not isinstance(b, dict):
                        raise LogicError("E_INVALID_REQUEST", "Expectation missing b_ref")
                    if "ref" in b:
                        b_ref = b.get("ref")
                        if b_ref in active_rules:
                            b_entry = active_rules[b_ref]
                        else:
                            b_entry = self.store.get_latest_item("rules", b_ref)
                        b_expr = self._compile_rule_expr({**b_entry, "id": b_ref}, z3_vars, local_symbols)
                    else:
                        b_expr_str = b.get("expr")
                        compiler = PyExprCompiler(local_symbols)
                        b_expr, _ = compiler.compile(b_expr_str, z3_vars)

                # helper to check entailment
                def entails(left_expr: z3.BoolRef, right_expr: z3.BoolRef) -> ExpectationResult:
                    base_solver.push()
                    try:
                        base_solver.add(left_expr)
                        base_solver.add(z3.Not(right_expr))
                        status, reason = self._check_solver(base_solver, assumptions)
                        if status == "sat":
                            model = base_solver.model()
                            symbols = list(z3_vars.keys())
                            counter = self._model_to_json(model, symbols, z3_vars)
                            return ExpectationResult(status="fail", counterexample=counter)
                        if status == "unknown":
                            if fail_on_timeout and self._is_timeout_reason(reason):
                                raise LogicError("E_TIMEOUT", "Expectation solver timed out", {"id": exp_id})
                            return ExpectationResult(status="unknown", reason=reason or "solver_unknown")
                        return ExpectationResult(status="pass")
                    finally:
                        base_solver.pop()

                if kind == "entails":
                    results[exp_id] = entails(a_expr, b_expr)
                else:
                    res1 = entails(a_expr, b_expr)
                    if res1.status != "pass":
                        results[exp_id] = res1
                        continue
                    res2 = entails(b_expr, a_expr)
                    results[exp_id] = res2
            except LogicError as exc:
                results[exp_id] = ExpectationResult(status="unknown", reason=exc.message)
            except Exception as exc:
                results[exp_id] = ExpectationResult(status="unknown", reason=str(exc))
        return results

    def check(self, args: dict) -> dict:
        hypothesis = args.get("hypothesis", {}) or {}
        facts = hypothesis.get("facts", {}) or {}
        patch = hypothesis.get("patch", {}) or {}
        if not isinstance(facts, dict):
            raise LogicError("E_INVALID_REQUEST", "hypothesis.facts must be an object")
        if not isinstance(patch, dict):
            raise LogicError("E_INVALID_REQUEST", "hypothesis.patch must be an object")
        options = args.get("options", {}) or {}
        analyse_influence = options.get("analyse_influence", True)
        return_models = options.get("return_models", True)
        return_unsat_core = options.get("return_unsat_core", True)
        check_expectations = options.get("check_expectations")
        timeout_raw = options.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        if not isinstance(timeout_raw, int) or timeout_raw < 1:
            raise LogicError("E_INVALID_REQUEST", "options.timeout_ms must be a positive integer")
        timeout_ms = timeout_raw
        fail_on_timeout = bool(options.get("fail_on_timeout", False))

        active_bundles = self.store.get_active_items("bundles")
        active_rules = self.store.get_active_items("rules")
        active_expectations = self.store.get_active_items("expectations")

        if check_expectations is None:
            check_expectations = bool(active_expectations)

        effective_facts = self._get_default_facts()
        effective_facts.update(facts)

        # Prepare candidate rules based on patch
        candidate_rules = dict(active_rules)
        patch_add = patch.get("add", {}) or {}
        patch_replace = patch.get("replace", {}) or {}
        patch_delete = patch.get("delete", []) or []
        if not isinstance(patch_add, dict):
            raise LogicError("E_INVALID_REQUEST", "hypothesis.patch.add must be an object")
        if not isinstance(patch_replace, dict):
            raise LogicError("E_INVALID_REQUEST", "hypothesis.patch.replace must be an object")
        if not isinstance(patch_delete, list):
            raise LogicError("E_INVALID_REQUEST", "hypothesis.patch.delete must be a list")

        # Apply delete
        for rid in patch_delete:
            if not isinstance(rid, str):
                raise LogicError("E_INVALID_REQUEST", "patch.delete values must be rule ids")
            if rid not in active_rules:
                raise LogicError("E_UNKNOWN_ID", "Rule id does not exist", {"id": rid})
            candidate_rules.pop(rid, None)

        # Apply replace
        for rid, replacement in patch_replace.items():
            if rid not in active_rules:
                raise LogicError("E_UNKNOWN_ID", "Rule id does not exist", {"id": rid})
            if not isinstance(replacement, dict):
                raise LogicError("E_INVALID_REQUEST", "Invalid replacement rule", {"id": rid})
            lang = replacement.get("lang")
            rule_content = replacement.get("rule")
            if lang not in {"pyexpr", "smt2"} or rule_content is None:
                raise LogicError("E_INVALID_REQUEST", "Invalid replacement rule", {"id": rid})
            candidate_rules[rid] = {"id": rid, "lang": lang, "content": rule_content}

        # Apply add
        for rid, addition in patch_add.items():
            if not isinstance(addition, dict):
                raise LogicError("E_INVALID_REQUEST", "Invalid added rule", {"id": rid})
            lang = addition.get("lang")
            rule_content = addition.get("rule")
            if lang not in {"pyexpr", "smt2"} or rule_content is None:
                raise LogicError("E_INVALID_REQUEST", "Invalid added rule", {"id": rid})
            if rid in active_rules and rid not in patch_delete and rid not in patch_replace:
                raise LogicError("E_INVALID_REQUEST", "Added rule id collides with existing rule", {"id": rid})
            candidate_rules[rid] = {"id": rid, "lang": lang, "content": rule_content}

        with self._cache_lock:
            base_context = self._get_base_context(active_bundles, active_rules)
            solver = base_context.solver

            # Baseline check
            solver.push()
            try:
                baseline_symbols = dict(base_context.symbol_table)
                baseline_z3_vars = dict(base_context.z3_vars)
                solver.set(timeout=timeout_ms)
                baseline_fact_symbols = self._apply_facts(solver, effective_facts, baseline_z3_vars, baseline_symbols)
                baseline_assumptions = self._active_assumptions(base_context, set(active_rules.keys()))
                baseline_track = dict(base_context.assumption_to_item)
                baseline_status, baseline_reason = self._check_solver(solver, baseline_assumptions)
                if fail_on_timeout and baseline_status == "unknown" and self._is_timeout_reason(baseline_reason):
                    raise LogicError("E_TIMEOUT", "Baseline check timed out")
                baseline_result = {"status": baseline_status}
                if baseline_status == "unknown" and baseline_reason:
                    baseline_result["reason"] = baseline_reason
                baseline_unsat_core: List[str] = []
                if baseline_status == "unsat" and return_unsat_core:
                    baseline_unsat_core = self._extract_unsat_core(solver, baseline_track)
                    baseline_result["unsat_core"] = baseline_unsat_core
                baseline_model_symbols = set(baseline_fact_symbols)
                if baseline_status == "sat" and return_models:
                    model = solver.model()
                    baseline_result["model"] = self._model_to_json(model, baseline_model_symbols, baseline_z3_vars)
            finally:
                solver.pop()

            # Candidate check
            solver.push()
            try:
                candidate_symbols = dict(base_context.symbol_table)
                candidate_z3_vars = dict(base_context.z3_vars)
                solver.set(timeout=timeout_ms)
                candidate_fact_symbols = self._apply_facts(solver, effective_facts, candidate_z3_vars, candidate_symbols)

                candidate_track = dict(base_context.assumption_to_item)
                disabled_base_rules = set(patch_delete) | set(patch_replace.keys())
                enabled_base_rules = set(active_rules.keys()) - disabled_base_rules
                candidate_assumptions = self._active_assumptions(base_context, enabled_base_rules)

                for rid, replacement in patch_replace.items():
                    patch_entry = {"id": rid, "lang": replacement["lang"], "content": replacement["rule"]}
                    patch_bundle = self._compile_rule_bundle(rid, patch_entry, candidate_z3_vars, candidate_symbols)
                    for idx, expr in enumerate(patch_bundle.assertions):
                        lit_name = self._assumption_name("patch_replace", rid, idx)
                        lit = z3.Bool(lit_name)
                        solver.add(z3.Implies(lit, expr))
                        candidate_assumptions.append(lit)
                        candidate_track[lit_name] = rid

                for rid, addition in patch_add.items():
                    patch_entry = {"id": rid, "lang": addition["lang"], "content": addition["rule"]}
                    patch_bundle = self._compile_rule_bundle(rid, patch_entry, candidate_z3_vars, candidate_symbols)
                    for idx, expr in enumerate(patch_bundle.assertions):
                        lit_name = self._assumption_name("patch_add", rid, idx)
                        lit = z3.Bool(lit_name)
                        solver.add(z3.Implies(lit, expr))
                        candidate_assumptions.append(lit)
                        candidate_track[lit_name] = rid

                candidate_status, candidate_reason = self._check_solver(solver, candidate_assumptions)
                if fail_on_timeout and candidate_status == "unknown" and self._is_timeout_reason(candidate_reason):
                    raise LogicError("E_TIMEOUT", "Candidate check timed out")
                candidate_result = {"status": candidate_status}
                if candidate_status == "unknown" and candidate_reason:
                    candidate_result["reason"] = candidate_reason
                candidate_unsat_core: List[str] = []
                if candidate_status == "unsat" and return_unsat_core:
                    candidate_unsat_core = self._extract_unsat_core(solver, candidate_track)
                    candidate_result["unsat_core"] = candidate_unsat_core
                candidate_model_symbols = set(candidate_fact_symbols)
                if candidate_status == "sat" and return_models:
                    model = solver.model()
                    candidate_result["model"] = self._model_to_json(model, candidate_model_symbols, candidate_z3_vars)
            finally:
                solver.pop()

        breaks = baseline_status == "sat" and candidate_status != "sat"

        # Expectations
        expectation_section = {"baseline": {}, "candidate": {}}
        baseline_expect_fail = set()
        candidate_expect_fail = set()
        if check_expectations and active_expectations:
            with self._cache_lock:
                base_context = self._get_base_context(active_bundles, active_rules)
                solver = base_context.solver

                solver.push()
                try:
                    baseline_symbols = dict(base_context.symbol_table)
                    baseline_z3_vars = dict(base_context.z3_vars)
                    solver.set(timeout=timeout_ms)
                    self._apply_facts(solver, effective_facts, baseline_z3_vars, baseline_symbols)
                    baseline_assumptions = self._active_assumptions(base_context, set(active_rules.keys()))
                    baseline_expect = self._check_expectations(
                        active_expectations,
                        solver,
                        baseline_assumptions,
                        baseline_z3_vars,
                        active_rules,
                        baseline_symbols,
                        fail_on_timeout=fail_on_timeout,
                    )
                finally:
                    solver.pop()

                solver.push()
                try:
                    candidate_symbols = dict(base_context.symbol_table)
                    candidate_z3_vars = dict(base_context.z3_vars)
                    solver.set(timeout=timeout_ms)
                    self._apply_facts(solver, effective_facts, candidate_z3_vars, candidate_symbols)
                    disabled_base_rules = set(patch_delete) | set(patch_replace.keys())
                    enabled_base_rules = set(active_rules.keys()) - disabled_base_rules
                    candidate_assumptions = self._active_assumptions(base_context, enabled_base_rules)
                    for rid, replacement in patch_replace.items():
                        patch_entry = {"id": rid, "lang": replacement["lang"], "content": replacement["rule"]}
                        patch_bundle = self._compile_rule_bundle(rid, patch_entry, candidate_z3_vars, candidate_symbols)
                        for idx, expr in enumerate(patch_bundle.assertions):
                            lit = z3.Bool(self._assumption_name("exp_patch_replace", rid, idx))
                            solver.add(z3.Implies(lit, expr))
                            candidate_assumptions.append(lit)
                    for rid, addition in patch_add.items():
                        patch_entry = {"id": rid, "lang": addition["lang"], "content": addition["rule"]}
                        patch_bundle = self._compile_rule_bundle(rid, patch_entry, candidate_z3_vars, candidate_symbols)
                        for idx, expr in enumerate(patch_bundle.assertions):
                            lit = z3.Bool(self._assumption_name("exp_patch_add", rid, idx))
                            solver.add(z3.Implies(lit, expr))
                            candidate_assumptions.append(lit)
                    candidate_expect = self._check_expectations(
                        active_expectations,
                        solver,
                        candidate_assumptions,
                        candidate_z3_vars,
                        candidate_rules,
                        candidate_symbols,
                        fail_on_timeout=fail_on_timeout,
                    )
                finally:
                    solver.pop()

            expectation_section["baseline"] = {
                exp_id: {
                    "status": res.status,
                    "counterexample": res.counterexample,
                    "reason": res.reason,
                }
                for exp_id, res in baseline_expect.items()
            }
            expectation_section["candidate"] = {
                exp_id: {
                    "status": res.status,
                    "counterexample": res.counterexample,
                    "reason": res.reason,
                }
                for exp_id, res in candidate_expect.items()
            }
            for exp_id, res in baseline_expect.items():
                if res.status == "fail":
                    baseline_expect_fail.add(exp_id)
            for exp_id, res in candidate_expect.items():
                if res.status == "fail":
                    candidate_expect_fail.add(exp_id)

        # Influence analysis
        influence_info: Dict[str, Any] = {"patch_influence": None, "details": {}, "skipped": []}
        if analyse_influence:
            influence_budget = INFLUENCE_BUDGET
            influence_details: Dict[str, Any] = {"add": {}, "delete": {}, "replace": {}}
            skipped: List[str] = []
            patch_influence = False
            unknown = False

            def budget_check(tag: str) -> bool:
                nonlocal influence_budget
                if influence_budget <= 0:
                    skipped.append(tag)
                    return False
                influence_budget -= 1
                return True

            def entailment_check(enabled_rule_ids: set[str], expr: z3.BoolRef, z3_vars: Dict[str, z3.ExprRef], symbols: Dict[str, str]) -> str:
                with self._cache_lock:
                    base_context = self._get_base_context(active_bundles, active_rules)
                    solver = base_context.solver
                    solver.push()
                    try:
                        solver.set(timeout=timeout_ms)
                        self._apply_facts(solver, effective_facts, z3_vars, symbols)
                        solver.add(z3.Not(expr))
                        assumptions = self._active_assumptions(base_context, enabled_rule_ids)
                        status, _ = self._check_solver(solver, assumptions)
                        return status
                    finally:
                        solver.pop()

            if baseline_status == "sat" and candidate_status == "unsat":
                patch_influence = True
            elif baseline_status == "unsat" and candidate_status == "sat":
                patch_influence = True
            elif baseline_status == "sat" and candidate_status == "sat":
                active_rule_ids = set(active_rules.keys())

                for rid, addition in patch_add.items():
                    tag = f"add:{rid}"
                    if not budget_check(tag):
                        influence_details["add"][rid] = "unknown"
                        unknown = True
                        continue
                    symbols = dict(self.store.data.get("symbols", {}))
                    z3_vars = self._build_z3_vars(symbols)
                    expr = self._compile_rule_expr(
                        {"id": rid, "lang": addition["lang"], "content": addition["rule"]},
                        z3_vars,
                        symbols,
                    )
                    status = entailment_check(active_rule_ids, expr, z3_vars, symbols)
                    if status == "unknown":
                        influence_details["add"][rid] = "unknown"
                        unknown = True
                        continue
                    influential = status == "sat"
                    influence_details["add"][rid] = influential
                    if influential:
                        patch_influence = True

                for rid in patch_delete:
                    tag = f"delete:{rid}"
                    if not budget_check(tag):
                        influence_details["delete"][rid] = "unknown"
                        unknown = True
                        continue
                    symbols = dict(self.store.data.get("symbols", {}))
                    z3_vars = self._build_z3_vars(symbols)
                    expr_old = self._compile_rule_expr({**active_rules[rid], "id": rid}, z3_vars, symbols)
                    status = entailment_check(active_rule_ids - {rid}, expr_old, z3_vars, symbols)
                    if status == "unknown":
                        influence_details["delete"][rid] = "unknown"
                        unknown = True
                        continue
                    influential = status == "sat"
                    influence_details["delete"][rid] = influential
                    if influential:
                        patch_influence = True

                for rid, replacement in patch_replace.items():
                    replace_result: Dict[str, Any] = {"delete": "unknown", "add": "unknown"}

                    del_tag = f"replace:{rid}:delete"
                    if budget_check(del_tag):
                        symbols_del = dict(self.store.data.get("symbols", {}))
                        z3_vars_del = self._build_z3_vars(symbols_del)
                        expr_old = self._compile_rule_expr({**active_rules[rid], "id": rid}, z3_vars_del, symbols_del)
                        del_status = entailment_check(set(active_rules.keys()) - {rid}, expr_old, z3_vars_del, symbols_del)
                        if del_status == "unknown":
                            unknown = True
                        else:
                            replace_result["delete"] = del_status == "sat"
                            if del_status == "sat":
                                patch_influence = True
                    else:
                        unknown = True

                    add_tag = f"replace:{rid}:add"
                    if budget_check(add_tag):
                        symbols_add = dict(self.store.data.get("symbols", {}))
                        z3_vars_add = self._build_z3_vars(symbols_add)
                        expr_new = self._compile_rule_expr(
                            {"id": rid, "lang": replacement["lang"], "content": replacement["rule"]},
                            z3_vars_add,
                            symbols_add,
                        )
                        add_status = entailment_check(set(active_rules.keys()) - {rid}, expr_new, z3_vars_add, symbols_add)
                        if add_status == "unknown":
                            unknown = True
                        else:
                            replace_result["add"] = add_status == "sat"
                            if add_status == "sat":
                                patch_influence = True
                    else:
                        unknown = True

                    influence_details["replace"][rid] = replace_result

            if baseline_status == "unknown" or candidate_status == "unknown" or unknown:
                influence_info["patch_influence"] = "unknown"
            else:
                influence_info["patch_influence"] = patch_influence
            influence_info["details"] = influence_details
            influence_info["skipped"] = skipped

        # Delta
        delta = {"newly_failed": [], "no_longer_failed": [], "still_failed": [], "unknown": []}
        baseline_failed = set(baseline_unsat_core) | baseline_expect_fail
        candidate_failed = set(candidate_unsat_core) | candidate_expect_fail
        delta_unknown = False
        unknown_candidates = set()
        if baseline_status == "unsat" and (not return_unsat_core or not baseline_unsat_core):
            delta_unknown = True
            unknown_candidates.update(active_bundles.keys())
            unknown_candidates.update(active_rules.keys())
        if candidate_status == "unsat" and (not return_unsat_core or not candidate_unsat_core):
            delta_unknown = True
            unknown_candidates.update(active_bundles.keys())
            unknown_candidates.update(candidate_rules.keys())
        if baseline_status == "unknown" or candidate_status == "unknown" or delta_unknown:
            delta["unknown"] = sorted(baseline_failed | candidate_failed | unknown_candidates)
        else:
            delta["newly_failed"] = sorted(candidate_failed - baseline_failed)
            delta["no_longer_failed"] = sorted(baseline_failed - candidate_failed)
            delta["still_failed"] = sorted(baseline_failed & candidate_failed)

        return {
            "ok": True,
            "result": {
                "baseline": baseline_result,
                "candidate": candidate_result,
                "breaks": breaks,
                "influence": influence_info,
                "delta": delta,
                "expectations": expectation_section,
            },
        }


ENGINES: Dict[str, LogicEngine] = {}

STRICT_RAW_CALL_LOGGING = os.getenv("LOGIC_STRICT_RAW_CALL_LOGGING", "1").lower() not in {"0", "false", "no"}


def _namespace_from_request(request: Any) -> Optional[str]:
    if request is None:
        return None
    try:
        path_params = getattr(request, "path_params", None)
        if isinstance(path_params, dict):
            session_id = path_params.get("session_id")
            if isinstance(session_id, str) and session_id:
                return str(session_id)
    except Exception:
        pass
    return None


def get_namespace_id() -> str:
    try:
        ctx = request_ctx.get()
    except LookupError:
        return "default"
    namespace_id = _namespace_from_request(getattr(ctx, "request", None))
    return namespace_id or "default"


def get_engine(namespace_id: Optional[str] = None) -> LogicEngine:
    namespace_id = namespace_id or get_namespace_id()
    if namespace_id not in ENGINES:
        ENGINES[namespace_id] = LogicEngine(namespace_id)
    return ENGINES[namespace_id]


server = Server("logic-tool-server", version="1.0")


DETAIL_LEVEL_VALUES = ["minimal", "compact", "more", "full"]
LIST_DETAIL_LEVEL_VALUES = ["minimal", "compact", "more"]
ITEM_DETAIL_LEVEL_VALUES = ["minimal", "compact", "more", "full"]
LIST_SHOW_VALUES = ["all", "bundles", "rules", "expectations", "concepts", "code_bindings"]
RULE_LANG_VALUES = ["pyexpr", "smt2"]
EXPECTATION_KIND_VALUES = ["entails", "equivalent"]
CONTEXT_PATCH_OPS = [
    "set_concept",
    "remove_concept",
    "set_code_binding",
    "remove_code_binding",
    "set_rule_meta",
    "set_expectation_meta",
]
PLAYBOOK_FOCUS_VALUES = ["onboarding", "discovery", "experiment", "hygiene", "handoff"]

MANIFEST_DIR = Path(__file__).resolve().parent.parent / ".logic_mcp_manifest"


def _tool_response_schema(result_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "description": "Common success/error envelope for all logic tools.",
        "properties": {
            "ok": {"type": "boolean"},
            "result": result_schema
            or {
                "type": "object",
                "description": "Tool-specific result object when ok=true.",
                "additionalProperties": True,
            },
            "error": {
                "type": "object",
                "description": "Error payload when ok=false.",
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "details": {"type": "object", "additionalProperties": True},
                },
                "required": ["code", "message"],
                "additionalProperties": True,
            },
        },
        "required": ["ok"],
        "additionalProperties": True,
    }


def _tool_annotations(
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def _manifest_text(filename: str, fallback: str) -> str:
    path = MANIFEST_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return fallback


def _prefixed(values: list[str], prefix: str) -> list[str]:
    if not prefix:
        return values
    pfx = prefix.lower()
    return [v for v in values if v.lower().startswith(pfx)]


def _resolve_session_token(token: str, active_session: str) -> str:
    if token in {"current", "active", "this"}:
        return active_session
    if token == active_session:
        return active_session
    raise ValueError(f"session_id '{token}' does not match active session '{active_session}'")


def _summarize_ids(items: list[dict], item_type: str, limit: int = 6) -> list[str]:
    ids = sorted(item.get("id") for item in items if item.get("type") == item_type and isinstance(item.get("id"), str))
    return ids[:limit]


def _session_snapshot_markdown(session_id: str) -> str:
    engine = get_engine(session_id)
    listing = engine.list_items({"show": ["all"], "detail_level": "minimal", "limit": 500})
    items = listing.get("result", {}).get("items", [])
    counts = {"bundle": 0, "rule": 0, "expectation": 0, "concept": 0, "code_binding": 0}
    for item in items:
        item_type = item.get("type")
        if item_type in counts:
            counts[item_type] += 1

    lines = [
        f"# Session Snapshot ({session_id})",
        "",
        "Use this as a low-cost orientation before mutations.",
        "",
        "## Counts",
        f"- bundles: {counts['bundle']}",
        f"- rules: {counts['rule']}",
        f"- expectations: {counts['expectation']}",
        f"- concepts: {counts['concept']}",
        f"- code_bindings: {counts['code_binding']}",
        "",
        "## Sample IDs",
        f"- rules: {', '.join(_summarize_ids(items, 'rule')) or '(none)'}",
        f"- expectations: {', '.join(_summarize_ids(items, 'expectation')) or '(none)'}",
        f"- concepts: {', '.join(_summarize_ids(items, 'concept')) or '(none)'}",
        f"- code_bindings: {', '.join(_summarize_ids(items, 'code_binding')) or '(none)'}",
        "",
        "## Suggested Next Calls",
        "1. `logic_list` with `detail_level:\"minimal\"` and narrow `show` filters.",
        "2. Inspect one item deeply with `logic_read` when needed.",
        "3. Add or update one rule/expectation/context item at a time.",
        "4. Run `logic_check` with a temporary `hypothesis.patch` for experiments.",
        "5. Escalate list detail to `more` and use `logic_read` for full item detail.",
    ]
    return "\n".join(lines)


def _inventory_resource_json(session_id: str, detail_level: str, query: dict[str, list[str]]) -> str:
    args: dict[str, Any] = {"detail_level": detail_level}

    show_values: list[str] = []
    for raw in query.get("show", []):
        for part in raw.split(","):
            item = part.strip()
            if item:
                show_values.append(item)
    if show_values:
        args["show"] = show_values

    limit_values = query.get("limit")
    if limit_values:
        args["limit"] = int(limit_values[0])

    cursor_values = query.get("cursor")
    if cursor_values:
        args["cursor"] = cursor_values[0]

    result = get_engine(session_id).list_items(args)
    return json.dumps(result, indent=2, ensure_ascii=False)


def _item_resource_json(session_id: str, item_id: str, query: dict[str, list[str]]) -> str:
    detail_level = "full"
    detail_values = query.get("detail_level")
    if detail_values and detail_values[0]:
        detail_level = detail_values[0]
    result = get_engine(session_id).read_item({"id": item_id, "detail_level": detail_level})
    return json.dumps(result, indent=2, ensure_ascii=False)


def _playbook_markdown(session_id: str, focus: str) -> str:
    focus_key = focus if focus in PLAYBOOK_FOCUS_VALUES else "discovery"
    opening = [
        f"# Reasoning Playbook ({session_id})",
        "",
        f"Focus: `{focus_key}`",
        "",
    ]
    sections: dict[str, list[str]] = {
        "onboarding": [
            "1. Call `logic_list` with `show:[\"all\"]`, `detail_level:\"minimal\"`.",
            "2. Fetch specific IDs with `logic_read {\"id\":\"...\"}` before modifying.",
            "3. Capture discovered invariants with `logic_set_rule` in small independent units.",
            "4. Attach context links incrementally with `logic_context_patch`.",
        ],
        "discovery": [
            "1. Convert each newly discovered requirement into one small `logic_set_rule` call.",
            "2. When omission risk appears, add one `logic_set_expectation` immediately.",
            "3. Add code/concept anchors as soon as they are known via `logic_context_patch`.",
            "4. Re-run `logic_check` after each modest change instead of batching large edits.",
        ],
        "experiment": [
            "1. Keep persistent state stable; use `logic_check.hypothesis.patch` for trial ideas.",
            "2. Try one patch at a time and inspect `breaks`, `delta`, `expectation_failures`.",
            "3. Start with `detail_level:\"compact\"`; escalate to `more` and use `logic_read` for full item detail.",
            "4. Persist only the experiments that survived checks.",
        ],
        "hygiene": [
            "1. Before remove/replace, inspect dependencies with `logic_list`.",
            "2. Remove fragile links in safe order: expectations/context first, then rule/bundle.",
            "3. Keep context graph coherent by updating concepts and code bindings together.",
            "4. Prefer many reversible edits over one large destructive migration.",
        ],
        "handoff": [
            "1. Export focused inventories (`rules`,`expectations`,`concepts`,`code_bindings`).",
            "2. Provide key ID lookups (`logic_read {\"id\":\"...\"}`) for critical nodes.",
            "3. Attach one recent `logic_check` result that explains current breakage risk.",
            "4. Include what-if candidates as temporary patch snippets, not persisted edits.",
        ],
    }
    body = sections[focus_key]
    trailer = [
        "",
        "Core heuristic: prefer many modestly sized calls and checks to avoid hidden coupling and reduce reasoning cost.",
    ]
    return "\n".join(opening + body + trailer)


def _base_tool_result_meta() -> dict[str, Any]:
    return {
        "result_contract": "ok_or_error_envelope",
        "error_codes": [
            "E_INVALID_REQUEST",
            "E_UNKNOWN_ID",
            "E_PARSE_ERROR",
            "E_UNSUPPORTED",
            "E_SOLVER_ERROR",
            "E_TIMEOUT",
        ],
    }


def _logic_tools() -> list[types.Tool]:
    rule_content_schema = {
        "description": "Rule/bundle content. For pyexpr use a single expression string. For smt2 use one string or a command array.",
        "oneOf": [
            {
                "type": "string",
                "description": "Single expression (pyexpr) or newline-delimited SMT2 text.",
                "examples": ["retry_limit >= 0", "(declare-const x Int)\n(assert (> x 0))"],
            },
            {
                "type": "array",
                "description": "SMT2 commands as ordered lines.",
                "items": {"type": "string"},
                "minItems": 1,
                "examples": [["(declare-const x Int)", "(assert (> x 0))"]],
            },
        ],
    }
    rule_patch_schema = {
        "type": "object",
        "description": "Temporary rule override/addition used only inside logic_check.hypothesis.patch.",
        "properties": {
            "lang": {
                "type": "string",
                "enum": RULE_LANG_VALUES,
                "description": "Rule language for the temporary patch rule.",
            },
            "rule": rule_content_schema,
        },
        "required": ["lang", "rule"],
        "additionalProperties": False,
    }
    list_result_schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "Page of list entries. Shape varies by item type and detail_level.",
                "items": {"type": "object", "additionalProperties": True},
            },
            "next_cursor": {
                "type": "string",
                "description": "Present when additional items remain; pass this to the next logic_list call.",
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    read_result_schema = {
        "type": "object",
        "properties": {
            "item": {
                "type": "object",
                "description": "Single inventory item resolved by global ID.",
                "additionalProperties": True,
            }
        },
        "required": ["item"],
        "additionalProperties": False,
    }
    check_result_schema = {
        "type": "object",
        "description": "Baseline/candidate outcome and diagnostics shaped by detail_level.",
        "properties": {
            "baseline": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["sat", "unsat", "unknown"]},
                    "unsat_core": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "object", "additionalProperties": True},
                    "reason": {"type": "string"},
                },
                "required": ["status"],
                "additionalProperties": True,
            },
            "candidate": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["sat", "unsat", "unknown"]},
                    "unsat_core": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "object", "additionalProperties": True},
                    "reason": {"type": "string"},
                },
                "required": ["status"],
                "additionalProperties": True,
            },
            "breaks": {"type": "boolean"},
            "delta": {"type": "object", "additionalProperties": True},
            "expectation_failures": {"type": "object", "additionalProperties": True},
            "expectations": {"type": "object", "additionalProperties": True},
            "influence": {"type": "object", "additionalProperties": True},
        },
        "required": ["baseline", "candidate", "breaks"],
        "additionalProperties": False,
    }

    base_meta = _base_tool_result_meta()
    tools: list[types.Tool] = []

    def add_tool(
        *,
        name: str,
        title: str,
        description: str,
        properties: dict[str, Any],
        required: list[str],
        read_only: bool,
        destructive: bool,
        idempotent: bool,
        open_world: bool,
        output_schema: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        tool_meta = dict(base_meta)
        if meta:
            tool_meta.update(meta)
        tools.append(
            types.Tool(
                name=name,
                title=title,
                description=description,
                inputSchema={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                outputSchema=output_schema or _tool_response_schema(),
                annotations=_tool_annotations(
                    read_only=read_only,
                    destructive=destructive,
                    idempotent=idempotent,
                    open_world=open_world,
                ),
                execution=types.ToolExecution(taskSupport="forbidden"),
                meta=tool_meta,
            )
        )

    add_tool(
        name="logic_set_rule",
        title="Set Rule",
        description=(
            "Create/replace one persistent rule. Prefer small, composable rules captured as discoveries appear, "
            "then validate with logic_check."
        ),
        properties={
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "Stable global ID for the rule.",
                "examples": ["r_retry_nonneg"],
            },
            "lang": {
                "type": "string",
                "enum": RULE_LANG_VALUES,
                "description": "Rule language.",
                "examples": ["pyexpr"],
            },
            "rule": rule_content_schema,
        },
        required=["id", "lang", "rule"],
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=False,
        meta={
            "use_cases": ["capture_new_invariant", "progressive_specification"],
            "look_before_act": "Prefer logic_list before edits unless you are certain the ID is new.",
        },
    )
    add_tool(
        name="logic_remove_rule",
        title="Remove Rule",
        description=(
            "Disable a persistent rule. Use only after reviewing dependencies (expectations, concepts, code bindings)."
        ),
        properties={
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "Rule ID to disable.",
            }
        },
        required=["id"],
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=False,
        meta={"use_cases": ["cleanup", "decommission_rule"], "requires_review": ["logic_list"]},
    )
    add_tool(
        name="logic_set_bundle",
        title="Set Bundle",
        description=(
            "Create/replace one persistent SMT2 bundle, usually declarations shared by multiple rules."
        ),
        properties={
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "Stable global ID for the bundle.",
                "examples": ["b_symbols"],
            },
            "bundle": {
                **rule_content_schema,
                "description": "SMT2 bundle content (declarations/defines/asserts).",
            },
        },
        required=["id", "bundle"],
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=False,
        meta={"use_cases": ["shared_symbol_declarations", "smt2_fragment_reuse"]},
    )
    add_tool(
        name="logic_remove_bundle",
        title="Remove Bundle",
        description="Disable a persistent bundle after dependency review.",
        properties={
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "Bundle ID to disable.",
            }
        },
        required=["id"],
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=False,
        meta={"use_cases": ["bundle_cleanup"]},
    )
    add_tool(
        name="logic_set_expectation",
        title="Set Expectation",
        description=(
            "Create/replace one expectation relationship to guard against omission bugs and refactor drift."
        ),
        properties={
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "Stable global ID for the expectation.",
                "examples": ["e_total_formula_equiv"],
            },
            "kind": {
                "type": "string",
                "enum": EXPECTATION_KIND_VALUES,
                "description": "Expectation mode: entails (A => B) or equivalent (A <=> B).",
            },
            "a_ref": {"type": "string", "minLength": 1, "description": "Left-side rule ID (A)."},
            "b_ref": {"type": "string", "minLength": 1, "description": "Right-side rule ID (B)."},
        },
        required=["id", "kind", "a_ref", "b_ref"],
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=False,
        meta={"use_cases": ["omission_guard", "semantic_equivalence_guard"]},
    )
    add_tool(
        name="logic_remove_expectation",
        title="Remove Expectation",
        description="Disable one expectation.",
        properties={
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "Expectation ID to disable.",
            }
        },
        required=["id"],
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=False,
        meta={"use_cases": ["expectation_cleanup"]},
    )
    add_tool(
        name="logic_check",
        title="Check Hypothesis",
        description=(
            "Run baseline vs candidate what-if analysis. Prefer many small experiments using hypothesis.patch "
            "instead of one large speculative rewrite."
        ),
        properties={
            "hypothesis": {
                "type": "object",
                "description": "Temporary request-scoped overlay for facts and patch rules.",
                "properties": {
                    "facts": {
                        "type": "object",
                        "description": (
                            "Fact overlay. Values can be bool/int/float or symbolic placeholders like '?x:Int'."
                        ),
                        "additionalProperties": {
                            "oneOf": [
                                {"type": "boolean"},
                                {"type": "integer"},
                                {"type": "number"},
                                {"type": "string"},
                            ]
                        },
                    },
                    "patch": {
                        "type": "object",
                        "description": "Temporary rule edits for candidate evaluation only (not persisted).",
                        "properties": {
                            "set_rules": {
                                "type": "object",
                                "description": "Rule additions/replacements by rule ID.",
                                "additionalProperties": rule_patch_schema,
                            },
                            "remove_rules": {
                                "type": "array",
                                "description": "Rule IDs temporarily disabled in candidate evaluation.",
                                "items": {"type": "string"},
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
            "detail_level": {
                "type": "string",
                "enum": DETAIL_LEVEL_VALUES,
                "default": "compact",
                "description": "Result verbosity/cost. Start compact, escalate only when needed.",
            },
        },
        required=[],
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        output_schema=_tool_response_schema(check_result_schema),
        meta={
            "use_cases": ["what_if_analysis", "counterexample_search", "regression_risk_scan"],
            "workflow_hint": "Use patch for experiments; persist only successful ideas.",
        },
    )
    add_tool(
        name="logic_context_patch",
        title="Patch Context Graph",
        description=(
            "Apply atomic updates to concepts, code bindings, and metadata. Keep logic-to-code links current as "
            "new understanding appears."
        ),
        properties={
            "ops": {
                "type": "array",
                "description": "Atomic operation list applied in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": CONTEXT_PATCH_OPS,
                            "description": "Patch operation type.",
                        },
                        "id": {"type": "string", "minLength": 1, "description": "Target ID for this operation."},
                        "set": {
                            "type": "object",
                            "description": "Partial payload for set_* operations. Ignored for remove_*.",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["op", "id"],
                    "additionalProperties": False,
                },
                "minItems": 1,
            }
        },
        required=["ops"],
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=False,
        meta={
            "use_cases": ["traceability_graph", "ownership_metadata", "incremental_knowledge_capture"],
            "workflow_hint": "Add concept and code-binding links as soon as entities are discovered.",
        },
    )
    add_tool(
        name="logic_list",
        title="List Inventory",
        description=(
            "List inventory across bundles, rules, expectations, concepts, and code bindings. "
            "Use this first to orient before mutation unless certainty is high."
        ),
        properties={
            "show": {
                "type": "array",
                "description": "Item categories to include. Defaults to ['all'].",
                "items": {"type": "string", "enum": LIST_SHOW_VALUES},
            },
            "detail_level": {
                "type": "string",
                "enum": LIST_DETAIL_LEVEL_VALUES,
                "default": "compact",
                "description": "Response detail/cost level.",
            },
            "cursor": {
                "type": "string",
                "description": "Opaque decimal offset returned as next_cursor by a previous page.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "default": 50,
                "description": "Maximum number of items in one page.",
            },
        },
        required=[],
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        output_schema=_tool_response_schema(list_result_schema),
        meta={
            "use_cases": ["orientation", "pre_edit_scan", "dependency_review", "handoff"],
            "workflow_hint": "Start minimal, escalate list detail to more, then use logic_read for full item detail.",
        },
    )
    add_tool(
        name="logic_read",
        title="Read Item",
        description=(
            "Read one inventory item by global ID with optional full detail."
        ),
        properties={
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "Global item ID to read.",
            },
            "detail_level": {
                "type": "string",
                "enum": ITEM_DETAIL_LEVEL_VALUES,
                "default": "full",
                "description": "Item detail level.",
            },
        },
        required=["id"],
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        output_schema=_tool_response_schema(read_result_schema),
        meta={
            "use_cases": ["single_item_lookup", "deep_inspection", "handoff_anchor_review"],
            "workflow_hint": "Use after logic_list identifies the target ID.",
        },
    )

    return tools


_TOOL_OUTPUT_SCHEMAS: dict[str, dict[str, Any] | None] | None = None


def get_tool_output_schema(tool_name: str) -> dict[str, Any] | None:
    global _TOOL_OUTPUT_SCHEMAS
    if _TOOL_OUTPUT_SCHEMAS is None:
        schemas: dict[str, dict[str, Any] | None] = {}
        for tool in _logic_tools():
            output_schema = getattr(tool, "outputSchema", None)
            schemas[tool.name] = output_schema if isinstance(output_schema, dict) else None
        _TOOL_OUTPUT_SCHEMAS = schemas
    return _TOOL_OUTPUT_SCHEMAS.get(tool_name)


@server.list_tools()
async def list_tools(_: types.ListToolsRequest | None = None) -> types.ListToolsResult:
    return types.ListToolsResult(tools=_logic_tools())


@server.list_resources()
async def list_resources(_: types.ListResourcesRequest | None = None) -> types.ListResourcesResult:
    resources = [
        types.Resource(
            name="logic_guide_overview",
            title="Logic Guide Overview",
            uri="logic://guide/overview",
            description=(
                "High-level strategy for using Logic MCP effectively: orient first, model incrementally, "
                "and run many modest experiments."
            ),
            mimeType="text/markdown",
            annotations=types.Annotations(audience=["assistant"], priority=1.0),
        ),
        types.Resource(
            name="logic_guide_incremental_strategy",
            title="Logic Incremental Strategy",
            uri="logic://guide/incremental-strategy",
            description="Detailed loop emphasizing smaller calls, progressive modeling, and low-cost validation.",
            mimeType="text/markdown",
            annotations=types.Annotations(audience=["assistant"], priority=1.0),
        ),
        types.Resource(
            name="logic_manifest",
            title="Manifest",
            uri="logic://guide/manifest",
            description="Manifest guidance shipped with this server installation.",
            mimeType="text/markdown",
            annotations=types.Annotations(audience=["assistant"], priority=0.95),
        ),
        types.Resource(
            name="logic_examples",
            title="Examples",
            uri="logic://guide/examples",
            description="Compact request patterns for common reasoning flows.",
            mimeType="text/markdown",
            annotations=types.Annotations(audience=["assistant"], priority=0.95),
        ),
        types.Resource(
            name="logic_use_cases",
            title="Use-Case Catalog",
            uri="logic://guide/use-cases",
            description="Extended use-case demonstrations spanning discovery, experimentation, and handoff.",
            mimeType="text/markdown",
            annotations=types.Annotations(audience=["assistant"], priority=0.9),
        ),
        types.Resource(
            name="logic_use_case_index",
            title="Use-Case Index",
            uri="logic://guide/use-case-index",
            description="Low-token index of available use-case scenarios.",
            mimeType="text/markdown",
            annotations=types.Annotations(audience=["assistant"], priority=0.9),
        ),
        types.Resource(
            name="logic_session_snapshot",
            title="Session Snapshot",
            uri="logic://session/current/snapshot",
            description="Current session inventory counts, sample IDs, and suggested next calls.",
            mimeType="text/markdown",
            annotations=types.Annotations(audience=["assistant"], priority=0.98),
        ),
        types.Resource(
            name="logic_session_playbook",
            title="Session Playbook",
            uri="logic://session/current/playbook",
            description="Actionable checklist for incremental reasoning loops in the current session.",
            mimeType="text/markdown",
            annotations=types.Annotations(audience=["assistant"], priority=0.96),
        ),
    ]
    return types.ListResourcesResult(resources=resources)


@server.list_resource_templates()
async def list_resource_templates() -> list[types.ResourceTemplate]:
    return [
        types.ResourceTemplate(
            name="logic_session_inventory",
            title="Session Inventory",
            uriTemplate="logic://session/{session_id}/inventory/{detail_level}",
            description=(
                "Machine-readable inventory snapshot (detail_level=minimal|compact|more). Optional query params: show=rules,expectations "
                "& limit=50 & cursor=0."
            ),
            mimeType="application/json",
            annotations=types.Annotations(audience=["assistant"], priority=1.0),
        ),
        types.ResourceTemplate(
            name="logic_session_item",
            title="Session Item Lookup",
            uriTemplate="logic://session/{session_id}/item/{item_id}",
            description="Lookup one item by global ID. Optional query param: detail_level=minimal|compact|more|full.",
            mimeType="application/json",
            annotations=types.Annotations(audience=["assistant"], priority=0.96),
        ),
        types.ResourceTemplate(
            name="logic_session_playbook_focus",
            title="Session Playbook by Focus",
            uriTemplate="logic://session/{session_id}/playbook/{focus}",
            description="Focused workflow guidance. focus=onboarding|discovery|experiment|hygiene|handoff.",
            mimeType="text/markdown",
            annotations=types.Annotations(audience=["assistant"], priority=0.95),
        ),
    ]


@server.read_resource()
async def read_resource(uri: Any) -> Iterable[ReadResourceContents]:
    uri_text = str(uri)
    parsed = urlparse(uri_text)
    scheme = parsed.scheme
    host = parsed.netloc
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query, keep_blank_values=False)
    active_session = get_namespace_id()

    if scheme != "logic":
        raise ValueError("Only logic:// URIs are supported.")

    if host == "guide":
        if parts == ["overview"]:
            text = "\n".join(
                [
                    "# Logic MCP Overview",
                    "",
                    "Use Logic MCP for solver-backed reasoning whenever constraints can be expressed formally.",
                    "",
                    "## Core Operating Pattern",
                    "1. Orient with `logic_list` before mutations unless certainty is high.",
                    "2. Use `logic_read` for deep inspection of specific IDs.",
                    "3. Add structure incrementally: bundles/rules/expectations/context as discoveries appear.",
                    "4. Use temporary `logic_check.hypothesis.patch` for experiments.",
                    "5. Prefer many modest calls over one large speculative construction.",
                    "6. Persist only the pieces that survive checks.",
                    "",
                    "## Why This Pattern",
                    "- Faster: each call has lower cognitive and token cost.",
                    "- Safer: smaller changes isolate regressions and reveal dependencies early.",
                    "- More exact: solver feedback guides each next move with concrete evidence.",
                ]
            )
            return [ReadResourceContents(content=text, mime_type="text/markdown")]
        if parts == ["incremental-strategy"]:
            text = "\n".join(
                [
                    "# Incremental Strategy",
                    "",
                    "## Look Before Act",
                    "- Run `logic_list` with minimal detail first.",
                    "- Pull full detail only for IDs you plan to touch using `logic_read`.",
                    "",
                    "## Capture As You Discover",
                    "- New invariant: `logic_set_rule`.",
                    "- Shared declarations: `logic_set_bundle`.",
                    "- Omission/refactor risk: `logic_set_expectation`.",
                    "- Traceability to code/spec: `logic_context_patch` concept/binding ops.",
                    "",
                    "## Experiment Freely",
                    "- Keep persistent graph stable.",
                    "- Use `logic_check` with hypothesis facts/patch for trial ideas.",
                    "- Compare compact outputs quickly; escalate list detail to `more` and inspect IDs with `logic_read` when needed.",
                    "",
                    "## Suggested Loop",
                    "1. list -> 2. read target IDs -> 3. one focused change -> 4. check -> 5. keep/revert idea -> 6. repeat",
                ]
            )
            return [ReadResourceContents(content=text, mime_type="text/markdown")]
        if parts == ["manifest"]:
            text = _manifest_text(
                "manifest.md",
                "# Manifest\n\nManifest file not found on disk. Use `logic://guide/overview` for default guidance.",
            )
            return [ReadResourceContents(content=text, mime_type="text/markdown")]
        if parts == ["examples"]:
            text = _manifest_text(
                "examples.md",
                "# Examples\n\nExamples file not found on disk. Use tool schemas and prompts for request patterns.",
            )
            return [ReadResourceContents(content=text, mime_type="text/markdown")]
        if parts == ["use-cases"]:
            text = _manifest_text(
                "use-case-examples.md",
                "# Use Cases\n\nUse-case file not found on disk.",
            )
            return [ReadResourceContents(content=text, mime_type="text/markdown")]
        if parts == ["use-case-index"]:
            src = _manifest_text("use-case-examples.md", "")
            headings = []
            for line in src.splitlines():
                if line.startswith("## "):
                    headings.append(line[3:].strip())
            if not headings:
                headings = [
                    "Spec-time invariant capture",
                    "Counterexample search",
                    "Temporary patch experimentation",
                    "Traceability graph maintenance",
                    "Focused handoff review",
                ]
            text = "# Use-Case Index\n\n" + "\n".join(f"- {entry}" for entry in headings)
            return [ReadResourceContents(content=text, mime_type="text/markdown")]
        raise ValueError(f"Unknown guide resource: {uri_text}")

    if host == "session":
        if len(parts) == 2 and parts[0] == "current" and parts[1] == "snapshot":
            text = _session_snapshot_markdown(active_session)
            return [ReadResourceContents(content=text, mime_type="text/markdown")]
        if len(parts) == 2 and parts[0] == "current" and parts[1] == "playbook":
            text = _playbook_markdown(active_session, "discovery")
            return [ReadResourceContents(content=text, mime_type="text/markdown")]
        if len(parts) >= 3 and parts[1] == "inventory":
            session_id = _resolve_session_token(parts[0], active_session)
            detail_level = parts[2]
            text = _inventory_resource_json(session_id, detail_level, query)
            return [ReadResourceContents(content=text, mime_type="application/json")]
        if len(parts) >= 3 and parts[1] == "item":
            session_id = _resolve_session_token(parts[0], active_session)
            item_id = parts[2]
            text = _item_resource_json(session_id, item_id, query)
            return [ReadResourceContents(content=text, mime_type="application/json")]
        if len(parts) >= 3 and parts[1] == "playbook":
            session_id = _resolve_session_token(parts[0], active_session)
            focus = parts[2]
            text = _playbook_markdown(session_id, focus)
            return [ReadResourceContents(content=text, mime_type="text/markdown")]
        raise ValueError(f"Unknown session resource: {uri_text}")

    raise ValueError(f"Unknown resource host: {host}")


PROMPTS: dict[str, types.Prompt] = {
    "logic_orient": types.Prompt(
        name="logic_orient",
        title="Orient Before Mutation",
        description=(
            "Build a quick understanding of current logic state and choose safe incremental next calls."
        ),
        arguments=[
            types.PromptArgument(name="goal", description="Current problem or objective.", required=False),
            types.PromptArgument(
                name="certainty",
                description="How sure you are about existing model state: low|medium|high.",
                required=False,
            ),
        ],
    ),
    "logic_capture_discovery": types.Prompt(
        name="logic_capture_discovery",
        title="Capture Discovery",
        description=(
            "Translate newly discovered requirements into incremental rule/expectation/context updates."
        ),
        arguments=[
            types.PromptArgument(name="discovery", description="Natural-language discovery to encode.", required=True),
            types.PromptArgument(name="code_path", description="Optional related source/doc path.", required=False),
            types.PromptArgument(name="symbols", description="Optional comma-separated symbols.", required=False),
        ],
    ),
    "logic_experiment_loop": types.Prompt(
        name="logic_experiment_loop",
        title="What-If Experiment Loop",
        description="Plan low-risk experiments with hypothesis.patch and gradual detail escalation.",
        arguments=[
            types.PromptArgument(name="hypothesis", description="Experiment intent.", required=False),
            types.PromptArgument(
                name="detail_level",
                description="minimal|compact|more|full (defaults to compact).",
                required=False,
            ),
            types.PromptArgument(
                name="max_iterations",
                description="Suggested number of small iterations (e.g., 3, 5, 8).",
                required=False,
            ),
        ],
    ),
    "logic_graph_handoff": types.Prompt(
        name="logic_graph_handoff",
        title="Graph Handoff Review",
        description="Prepare a high-signal handoff from rules/expectations to concepts/code bindings.",
        arguments=[
            types.PromptArgument(name="focus", description="rules|expectations|concepts|code-bindings|all", required=False),
            types.PromptArgument(name="risk_area", description="Optional subsystem/risk summary.", required=False),
        ],
    ),
}


def _prompt_text(name: str, arguments: dict[str, str]) -> tuple[str, str]:
    if name == "logic_orient":
        goal = arguments.get("goal", "current task")
        certainty = arguments.get("certainty", "low")
        text = "\n".join(
            [
                f"Goal: {goal}",
                f"Certainty about existing model: {certainty}",
                "",
                "Use this sequence:",
                "1. `logic_list` with `{show:[\"all\"], detail_level:\"minimal\"}`.",
                "2. Retrieve exact items with `logic_read {\"id\":\"...\"}` before edits.",
                "3. Make one focused mutation at a time.",
                "4. Validate each mutation via `logic_check` (compact first).",
                "",
                "Default bias: look before act unless you are certain state already matches your intent.",
            ]
        )
        return ("Orientation workflow before edits.", text)
    if name == "logic_capture_discovery":
        discovery = arguments.get("discovery", "(no discovery text provided)")
        code_path = arguments.get("code_path", "")
        symbols = arguments.get("symbols", "")
        lines = [
            f"Discovery: {discovery}",
            "",
            "Encode incrementally:",
            "1. Add one rule/bundle reflecting the newly discovered invariant/declaration.",
            "2. Add expectation only when omission or equivalence risk appears.",
            "3. Attach concept/code binding links now rather than deferring context work.",
            "4. Run `logic_check` after each modest step.",
        ]
        if symbols:
            lines.append(f"- Candidate symbols: {symbols}")
        if code_path:
            lines.append(f"- Add/patch code binding path: {code_path}")
        lines.extend(
            [
                "",
                "Prefer multiple small calls over one large payload to keep reasoning and diagnostics sharp.",
            ]
        )
        return ("Convert discoveries into executable structure.", "\n".join(lines))
    if name == "logic_experiment_loop":
        hypothesis = arguments.get("hypothesis", "evaluate a candidate change")
        detail = arguments.get("detail_level", "compact")
        max_iterations = arguments.get("max_iterations", "5")
        text = "\n".join(
            [
                f"Hypothesis: {hypothesis}",
                f"Starting detail_level: {detail}",
                f"Suggested small iterations: {max_iterations}",
                "",
                "Experiment loop:",
                "1. Keep persistent state stable.",
                "2. Use `logic_check.hypothesis.patch` with one temporary change.",
                "3. Inspect `breaks`, `delta`, and `expectation_failures`.",
                "4. Keep, refine, or discard the idea.",
                "5. Repeat with next small variation.",
                "",
                "Escalate to `more` first; use `logic_read` for full item detail when compact output is insufficient.",
            ]
        )
        return ("What-if loop for safe, cheap, exact experimentation.", text)
    if name == "logic_graph_handoff":
        focus = arguments.get("focus", "all")
        risk_area = arguments.get("risk_area", "")
        lines = [
            f"Handoff focus: {focus}",
            "Collect these views:",
            "1. `logic_list` for `rules` and `expectations` (more).",
            "2. `logic_list` for `concepts` and `code_bindings` (more).",
            "3. Specific `logic_read {\"id\":\"...\", \"detail_level\":\"full\"}` lookups for critical anchors.",
            "4. One representative `logic_check` result for active risk context.",
        ]
        if risk_area:
            lines.append(f"Risk area to emphasize: {risk_area}")
        lines.append("Keep the handoff graph-first and evidence-backed.")
        return ("Prepare an auditable logic-to-code handoff package.", "\n".join(lines))
    raise ValueError(f"Unknown prompt '{name}'")


@server.list_prompts()
async def list_prompts(_: types.ListPromptsRequest | None = None) -> types.ListPromptsResult:
    return types.ListPromptsResult(prompts=list(PROMPTS.values()))


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    args = arguments or {}
    description, text = _prompt_text(name, args)
    return types.GetPromptResult(
        description=description,
        messages=[
            types.PromptMessage(
                role="assistant",
                content=types.TextContent(type="text", text=text),
            )
        ],
    )


@server.completion()
async def completion(
    ref: types.PromptReference | types.ResourceTemplateReference,
    argument: types.CompletionArgument,
    context: types.CompletionContext | None,
) -> types.Completion | None:
    del context
    values: list[str] = []
    name = argument.name
    current = argument.value

    if isinstance(ref, types.PromptReference):
        prompt_suggestions: dict[tuple[str, str], list[str]] = {
            ("logic_orient", "certainty"): ["low", "medium", "high"],
            ("logic_experiment_loop", "detail_level"): DETAIL_LEVEL_VALUES,
            ("logic_experiment_loop", "max_iterations"): ["3", "5", "8", "12"],
            ("logic_graph_handoff", "focus"): ["all", "rules", "expectations", "concepts", "code-bindings"],
        }
        values = prompt_suggestions.get((ref.name, name), [])
    elif isinstance(ref, types.ResourceTemplateReference):
        ref_name = getattr(ref, "name", "")
        ref_uri = str(getattr(ref, "uriTemplate", "") or getattr(ref, "uri", ""))
        if name == "detail_level":
            if ref_name == "logic_session_item" or "/item/" in ref_uri:
                values = ITEM_DETAIL_LEVEL_VALUES
            else:
                values = LIST_DETAIL_LEVEL_VALUES
        elif name == "show":
            values = LIST_SHOW_VALUES
        elif name == "focus":
            values = PLAYBOOK_FOCUS_VALUES
        elif name == "session_id":
            session_id = get_namespace_id()
            values = ["current"] if session_id == "default" else ["current", session_id]
        elif name == "item_id":
            session_id = get_namespace_id()
            try:
                items = get_engine(session_id).list_items({"show": ["all"], "detail_level": "minimal", "limit": 200})
                values = sorted(
                    {
                        item.get("id")
                        for item in items.get("result", {}).get("items", [])
                        if isinstance(item.get("id"), str) and item.get("id")
                    }
                )
            except Exception:
                values = []

    filtered = _prefixed(values, current)[:30]
    if not filtered:
        return None
    return types.Completion(values=filtered, total=len(filtered), hasMore=False)


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> dict:
    args = arguments or {}
    session_id = get_namespace_id()
    request = None
    ctx = None
    try:
        ctx = request_ctx.get()
        request = getattr(ctx, "request", None)
    except LookupError:
        request = None
        ctx = None
    if ctx is not None:
        session_obj = getattr(ctx, "session", None)
        if session_obj is not None:
            await SUPERVISOR.register_active_session(session_id, session_obj)
    call_payload = await build_tool_call_payload(
        request,
        name,
        arguments,
        strict_raw_call_logging=STRICT_RAW_CALL_LOGGING,
    )
    output_schema = get_tool_output_schema(name)
    mode = await SUPERVISOR.get_mode(session_id)
    if SUPERVISOR.mode_matches_stage(mode, INTERCEPT_STAGE_CALL):
        pending = await SUPERVISOR.create_pending(
            session_id=session_id,
            stage=INTERCEPT_STAGE_CALL,
            tool_name=name,
            call_payload=call_payload,
            tool_arguments=args if isinstance(args, dict) else {},
            output_schema=output_schema,
            tool_response=None,
        )
        action, payload = await SUPERVISOR.wait_for_decision(pending.intercept_id)
        if action == "override" and isinstance(payload, dict) and isinstance(payload.get("response"), dict):
            response = payload["response"]
            entry = append_tool_log(session_id, call_payload, response)
            if entry is not None:
                await SUPERVISOR.publish("session_log", new_event_payload_for_log(session_id, entry))
            return response
        if isinstance(payload, dict) and isinstance(payload.get("arguments"), dict):
            args = payload["arguments"]
    engine = get_engine(session_id)
    handlers = {
        "logic_set_rule": engine.set_rule,
        "logic_remove_rule": engine.remove_rule,
        "logic_set_bundle": engine.set_bundle,
        "logic_remove_bundle": engine.remove_bundle,
        "logic_set_expectation": engine.set_expectation,
        "logic_remove_expectation": engine.remove_expectation,
        "logic_context_patch": engine.context_patch,
        "logic_list": engine.list_items,
        "logic_read": engine.read_item,
        "logic_check": engine.check_v5,
    }
    content_mutation_tools = {
        "logic_set_rule",
        "logic_remove_rule",
        "logic_set_bundle",
        "logic_remove_bundle",
        "logic_set_expectation",
        "logic_remove_expectation",
        "logic_context_patch",
    }
    response: dict
    try:
        handler = handlers.get(name)
        if handler is None:
            response = {"ok": False, "error": {"code": "E_INVALID_REQUEST", "message": f"Unknown tool {name}"}}
        else:
            response = handler(args)
            if (
                name in content_mutation_tools
                and isinstance(response, dict)
                and response.get("ok") is True
            ):
                try:
                    await SUPERVISOR.publish_session_graph_updated(session_id)
                except Exception:
                    # Graph updates must not block tool responses.
                    pass
    except LogicError as exc:
        response = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        if exc.details:
            response["error"]["details"] = exc.details
    except Exception as exc:
        response = {"ok": False, "error": {"code": "E_SOLVER_ERROR", "message": str(exc)}}
    if SUPERVISOR.mode_matches_stage(mode, INTERCEPT_STAGE_REPLY):
        pending = await SUPERVISOR.create_pending(
            session_id=session_id,
            stage=INTERCEPT_STAGE_REPLY,
            tool_name=name,
            call_payload=call_payload,
            tool_arguments=args if isinstance(args, dict) else {},
            output_schema=output_schema,
            tool_response=response,
        )
        action, payload = await SUPERVISOR.wait_for_decision(pending.intercept_id)
        if action == "send" and isinstance(payload, dict) and isinstance(payload.get("response"), dict):
            response = payload["response"]
    entry = append_tool_log(session_id, call_payload, response)
    if entry is not None:
        await SUPERVISOR.publish("session_log", new_event_payload_for_log(session_id, entry))
    return response
