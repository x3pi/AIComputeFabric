import ast, json, operator, sys

OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Pow: operator.pow, ast.USub: operator.neg, ast.Mod: operator.mod}


def ev(n):
    if isinstance(n, ast.Expression):
        return ev(n.body)
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
        return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in OPS:
        a, b = ev(n.left), ev(n.right)
        if isinstance(n.op, ast.Pow) and abs(b) > 100:
            raise ValueError("exponent too large")
        return OPS[type(n.op)](a, b)
    if isinstance(n, ast.UnaryOp) and type(n.op) in OPS:
        return OPS[type(n.op)](ev(n.operand))
    raise ValueError("only numbers and + - * / % ** are allowed")


req = json.load(sys.stdin)
try:
    print(json.dumps({"value": ev(ast.parse(req["expr"], mode="eval"))}))
except Exception as e:
    print(json.dumps({"error": str(e)[:100]}))
