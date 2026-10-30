"""Small, AST-whitelisted expression evaluator for workflow edge conditions.

It supports the expressions used by the bundled flows without exposing Python
attributes, imports, comprehensions or arbitrary function calls.
"""
from __future__ import annotations

import ast
from typing import Any, Dict


HELPERS = {
    "len": len,
    "any": any,
    "all": all,
    "bool": bool,
    "str": str,
    "int": int,
}


def evaluate_condition(expression: str, state: Dict[str, Any]) -> Any:
    tree = ast.parse(expression, mode="eval")
    return _eval(tree.body, {"state": state})


def _eval(node: ast.AST, env: Dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in HELPERS:
            return HELPERS[node.id]
        raise ValueError(f"未声明的名称: {node.id}")
    if isinstance(node, ast.Subscript):
        return _eval(node.value, env)[_eval(node.slice, env)]
    if isinstance(node, ast.List):
        return [_eval(item, env) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(item, env) for item in node.elts)
    if isinstance(node, ast.Dict):
        return {
            _eval(key, env): _eval(value, env)
            for key, value in zip(node.keys, node.values)
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
        value = _eval(node.operand, env)
        if isinstance(node.op, ast.Not):
            return not value
        return -value if isinstance(node.op, ast.USub) else +value
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        if isinstance(node.op, ast.And):
            result: Any = True
            for item in node.values:
                result = _eval(item, env)
                if not result:
                    return result
            return result
        result = False
        for item in node.values:
            result = _eval(item, env)
            if result:
                return result
        return result
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)
    ):
        left, right = _eval(node.left, env), _eval(node.right, env)
        operations = {
            ast.Add: lambda: left + right,
            ast.Sub: lambda: left - right,
            ast.Mult: lambda: left * right,
            ast.Div: lambda: left / right,
            ast.Mod: lambda: left % right,
        }
        return operations[type(node.op)]()
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for operator, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, env)
            checks = {
                ast.Eq: lambda: left == right,
                ast.NotEq: lambda: left != right,
                ast.Lt: lambda: left < right,
                ast.LtE: lambda: left <= right,
                ast.Gt: lambda: left > right,
                ast.GtE: lambda: left >= right,
                ast.In: lambda: left in right,
                ast.NotIn: lambda: left not in right,
                ast.Is: lambda: left is right,
                ast.IsNot: lambda: left is not right,
            }
            if type(operator) not in checks:
                raise ValueError(f"不支持的比较运算: {type(operator).__name__}")
            if not checks[type(operator)]():
                return False
            left = right
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id not in HELPERS or node.keywords:
            raise ValueError("只允许调用白名单函数，且不支持关键字参数")
        args = [_eval(arg, env) for arg in node.args]
        return HELPERS[node.func.id](*args)
    raise ValueError(f"条件表达式包含不允许的语法: {type(node).__name__}")
