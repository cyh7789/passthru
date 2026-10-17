"""Tiny evaluator for Criterion.expr, e.g. `pt_record.weeks >= 6 and not pt_record.improved`.
Only names, attribute lookups, constants, comparisons and and/or/not. No calls, no eval()."""
import ast, operator

CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
       ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


class Unknown(Exception):
    """evidence doesn't have the field the rule asks about"""


def _ev(n, ev):
    if isinstance(n, ast.Expression):
        return _ev(n.body, ev)
    if isinstance(n, ast.Constant):
        return n.value
    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
        doc = ev.get(n.value.id)
        if doc is None or n.attr not in doc:
            raise Unknown(f"{n.value.id}.{n.attr}")
        return doc[n.attr]
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        return not _ev(n.operand, ev)
    if isinstance(n, ast.BoolOp):
        vals = (_ev(v, ev) for v in n.values)
        return all(vals) if isinstance(n.op, ast.And) else any(vals)
    if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in CMP:
        return CMP[type(n.ops[0])](_ev(n.left, ev), _ev(n.comparators[0], ev))
    raise ValueError(f"not allowed in criterion: {ast.dump(n)}")


def evaluate(expr, evidence):
    """True / False, or None if the evidence can't answer it"""
    try:
        return bool(_ev(ast.parse(expr, mode="eval"), evidence))
    except Unknown:
        return None
