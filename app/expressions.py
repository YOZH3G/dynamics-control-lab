"""Safe arithmetic formulas for objects described in files.

A formula is parsed with ``ast`` and evaluated by walking the tree; it is never
passed to ``eval``. Allowed: numbers, names given by the caller, + − × / and
powers, unary signs, parentheses and the functions listed in FUNCTIONS.
"""

import ast
import math

FUNCTIONS = {"exp": math.exp, "log": math.log, "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
             "tan": math.tan, "abs": abs, "min": min, "max": max}
_BINARY = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
           ast.Div: lambda a, b: a / b, ast.Pow: lambda a, b: a ** b}
_UNARY = {ast.USub: lambda a: -a, ast.UAdd: lambda a: a}
MAX_LENGTH = 500


class Formula:
    def __init__(self, text, names):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_LENGTH:
            raise ValueError(f"Формула должна быть непустым текстом до {MAX_LENGTH} символов.")
        try:
            tree = ast.parse(text.strip(), mode="eval")
        except SyntaxError as error:
            raise ValueError(f"Формула «{text}»: синтаксическая ошибка.") from error
        self.text = text
        self.names = set()
        self._check(tree.body, set(names))
        self._tree = tree.body

    def _check(self, node, names):
        if isinstance(node, ast.Constant):
            if type(node.value) not in (int, float):
                raise ValueError(f"Формула «{self.text}»: допускаются только числа.")
        elif isinstance(node, ast.Name):
            if node.id not in names:
                raise ValueError(f"Формула «{self.text}»: неизвестное имя «{node.id}».")
            self.names.add(node.id)
        elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            self._check(node.left, names); self._check(node.right, names)
        elif isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            self._check(node.operand, names)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS
              and not node.keywords and node.args):
            for argument in node.args:
                self._check(argument, names)
        else:
            raise ValueError(f"Формула «{self.text}»: недопустимая конструкция.")

    def __call__(self, values):
        try:
            value = self._evaluate(self._tree, values)
        except (ArithmeticError, ValueError, TypeError) as error:
            raise ValueError(f"Формула «{self.text}» не вычисляется: {error}.") from error
        if isinstance(value, complex) or not math.isfinite(value):
            raise ValueError(f"Формула «{self.text}» дала неконечное значение.")
        return float(value)

    def _evaluate(self, node, values):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BinOp):
            left, right = self._evaluate(node.left, values), self._evaluate(node.right, values)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("показатель степени больше 100")
            return _BINARY[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp):
            return _UNARY[type(node.op)](self._evaluate(node.operand, values))
        return FUNCTIONS[node.func.id](*(self._evaluate(a, values) for a in node.args))
