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
from typing import Any, Dict, Iterable, List, Optional, Tuple

import mcp.types as types
import z3
from mcp.server import Server
from mcp.server.lowlevel.server import request_ctx

from .audit_log import append_tool_log, build_tool_call_payload
from .errors import LogicError
from .store import Store

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

    def list_items(self, args: dict) -> dict:
        detail_level = args.get("detail_level", "compact")
        if detail_level not in {"minimal", "compact", "more", "full"}:
            raise LogicError("E_INVALID_REQUEST", "detail_level must be one of minimal|compact|more|full")
        show = args.get("show")
        item_id = args.get("id")
        cursor = args.get("cursor")
        limit_raw = args.get("limit", 50)
        if not isinstance(limit_raw, int) or limit_raw < 1:
            raise LogicError("E_INVALID_REQUEST", "limit must be an integer >= 1")
        limit = limit_raw

        if show is not None and item_id is not None:
            raise LogicError("E_INVALID_REQUEST", "id and show are mutually exclusive")
        if item_id is not None and not isinstance(item_id, str):
            raise LogicError("E_INVALID_REQUEST", "id must be a string")

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

        if item_id is not None:
            if item_id not in universe:
                raise LogicError("E_UNKNOWN_ID", "id does not exist", {"id": item_id})
            entry_type, payload = universe[item_id]
            level = args.get("detail_level", "full")
            if level not in {"minimal", "compact", "more", "full"}:
                raise LogicError("E_INVALID_REQUEST", "detail_level must be one of minimal|compact|more|full")
            return {"ok": True, "result": {"items": [self._render_list_item(entry_type, item_id, payload, level)]}}

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

        selected: List[tuple[str, str, dict]] = []
        if "bundles" in normalized_show:
            selected.extend(("bundle", item_id, payload) for item_id, payload in active_bundles.items())
        if "rules" in normalized_show:
            selected.extend(("rule", item_id, payload) for item_id, payload in active_rules.items())
        if "expectations" in normalized_show:
            selected.extend(("expectation", item_id, payload) for item_id, payload in active_expectations.items())
        if "concepts" in normalized_show:
            selected.extend(("concept", item_id, payload) for item_id, payload in concepts.items())
        if "code_bindings" in normalized_show:
            selected.extend(("code_binding", item_id, payload) for item_id, payload in bindings.items())

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


@server.list_tools()
async def list_tools(_: types.ListToolsRequest | None = None) -> types.ListToolsResult:
    tools = []

    def tool_schema(name: str, description: str, props: dict, required: list[str]) -> types.Tool:
        return types.Tool(
            name=name,
            description=description,
            inputSchema={
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            },
        )

    rule_content_schema = {
        "oneOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ]
    }
    rule_patch_schema = {
        "type": "object",
        "properties": {
            "lang": {"type": "string", "enum": ["pyexpr", "smt2"]},
            "rule": rule_content_schema,
        },
        "required": ["lang", "rule"],
        "additionalProperties": False,
    }

    tools.append(
        tool_schema(
            "logic_set_rule",
            "Set (create/replace) a persistent rule.",
            {
                "id": {"type": "string", "minLength": 1},
                "lang": {"type": "string", "enum": ["pyexpr", "smt2"]},
                "rule": rule_content_schema,
            },
            ["id", "lang", "rule"],
        )
    )
    tools.append(
        tool_schema(
            "logic_remove_rule",
            "Remove (disable active) a persistent rule.",
            {"id": {"type": "string", "minLength": 1}},
            ["id"],
        )
    )
    tools.append(
        tool_schema(
            "logic_set_bundle",
            "Set (create/replace) a persistent SMT2 bundle.",
            {
                "id": {"type": "string", "minLength": 1},
                "bundle": rule_content_schema,
            },
            ["id", "bundle"],
        )
    )
    tools.append(
        tool_schema(
            "logic_remove_bundle",
            "Remove (disable active) a persistent bundle.",
            {"id": {"type": "string", "minLength": 1}},
            ["id"],
        )
    )
    tools.append(
        tool_schema(
            "logic_set_expectation",
            "Set (create/replace) an expectation between two rules.",
            {
                "id": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "enum": ["entails", "equivalent"]},
                "a_ref": {"type": "string", "minLength": 1},
                "b_ref": {"type": "string", "minLength": 1},
            },
            ["id", "kind", "a_ref", "b_ref"],
        )
    )
    tools.append(
        tool_schema(
            "logic_remove_expectation",
            "Remove (disable active) an expectation.",
            {"id": {"type": "string", "minLength": 1}},
            ["id"],
        )
    )
    tools.append(
        tool_schema(
            "logic_check",
            "Run baseline/candidate what-if evaluation over the full active model.",
            {
                "hypothesis": {
                    "type": "object",
                    "properties": {
                        "facts": {"type": "object"},
                        "patch": {
                            "type": "object",
                            "properties": {
                                "set_rules": {"type": "object", "additionalProperties": rule_patch_schema},
                                "remove_rules": {"type": "array", "items": {"type": "string"}},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "additionalProperties": False,
                },
                "detail_level": {"type": "string", "enum": ["minimal", "compact", "more", "full"]},
            },
            [],
        )
    )
    tools.append(
        tool_schema(
            "logic_context_patch",
            "Apply an atomic context/meta patch.",
            {
                "ops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": [
                                    "set_concept",
                                    "remove_concept",
                                    "set_code_binding",
                                    "remove_code_binding",
                                    "set_rule_meta",
                                    "set_expectation_meta",
                                ],
                            },
                            "id": {"type": "string", "minLength": 1},
                            "set": {"type": "object"},
                        },
                        "required": ["op", "id"],
                        "additionalProperties": False,
                    },
                }
            },
            ["ops"],
        )
    )
    tools.append(
        tool_schema(
            "logic_list",
            "Unified list/lookup for logic and context inventories.",
            {
                "show": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["all", "bundles", "rules", "expectations", "concepts", "code_bindings"]},
                },
                "id": {"type": "string"},
                "detail_level": {"type": "string", "enum": ["minimal", "compact", "more", "full"]},
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1},
            },
            [],
        )
    )

    return types.ListToolsResult(tools=tools)


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> dict:
    args = arguments or {}
    session_id = get_namespace_id()
    request = None
    try:
        ctx = request_ctx.get()
        request = getattr(ctx, "request", None)
    except LookupError:
        request = None
    call_payload = await build_tool_call_payload(
        request,
        name,
        arguments,
        strict_raw_call_logging=STRICT_RAW_CALL_LOGGING,
    )
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
        "logic_check": engine.check_v5,
    }
    response: dict
    try:
        handler = handlers.get(name)
        if handler is None:
            response = {"ok": False, "error": {"code": "E_INVALID_REQUEST", "message": f"Unknown tool {name}"}}
        else:
            response = handler(args)
    except LogicError as exc:
        response = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        if exc.details:
            response["error"]["details"] = exc.details
    except Exception as exc:
        response = {"ok": False, "error": {"code": "E_SOLVER_ERROR", "message": str(exc)}}
    append_tool_log(session_id, call_payload, response)
    return response
