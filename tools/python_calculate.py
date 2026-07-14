"""The first executable tool: a restricted mathematical calculator.

`python_calculate` evaluates a *tightly restricted* subset of Python expression
syntax by walking an AST allowlist. It is deliberately **not** a Python REPL:
there is no `eval`/`exec`, no imports, no attribute access, no names beyond a few
math constants, and only an explicit allowlist of numeric functions. Execution
happens in-process, so the allowlist — not an OS sandbox — is the safety boundary.

The handler always returns a stable, JSON-compatible envelope:

    success:  {"ok": True,  "result": <number | list>}
    failure:  {"ok": False, "error": {"type": <category>, "message": <str>}}

No traceback, file path, or evaluator internal ever reaches the caller.
"""

import ast
import json
import math
from typing import Any

from tools.registry import ToolSpec


PYTHON_CALCULATE_SPEC = ToolSpec(
    name="python_calculate",
    description=(
        "Evaluate a safe mathematical expression using the local Python runtime. "
        "Use it for arithmetic and supported numeric functions such as sqrt, "
        "round, min, max, sum and len."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": (
                    "Mathematical expression, for example: (12 + 18 + 27) / 3"
                ),
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "result": {"description": "Calculated JSON-compatible result."}
        },
        "required": ["result"],
        "additionalProperties": False,
    },
)


# --- Deterministic resource limits (see SPEC-007 §Resource limits) ------------

MAX_EXPRESSION_LENGTH = 500
MAX_NODE_COUNT = 100
MAX_DEPTH = 20
MAX_EXPONENT = 1000  # magnitude of an integer exponent
MAX_SEQUENCE_LENGTH = 1000
MAX_FACTORIAL = 1000


# --- Allowlists ---------------------------------------------------------------

# Only these functions may be called, and only as bare `name(...)` calls.
_ALLOWED_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "len": len,
    "sqrt": math.sqrt,
    "ceil": math.ceil,
    "floor": math.floor,
    "factorial": math.factorial,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
}

# The only names that may appear in an expression.
_ALLOWED_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
}

# Binary and unary operators and how to apply them.
_BINARY_OPERATORS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}

_UNARY_OPERATORS = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}


# --- Internal control-flow exceptions (mapped to error categories) ------------


class _EvaluationError(Exception):
    """Base for errors that map to a stable envelope category."""

    category = "internal_error"


class _UnsafeExpression(_EvaluationError):
    category = "unsafe_expression"


class _ResourceLimit(_EvaluationError):
    category = "resource_limit"


class _CalculationError(_EvaluationError):
    category = "calculation_error"


class _InvalidExpression(_EvaluationError):
    category = "invalid_expression"


# --- Public handler -----------------------------------------------------------


def python_calculate(arguments: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one restricted mathematical expression.

    Returns the stable success/failure envelope described in the module docstring.
    """

    expression = _validate_arguments(arguments)
    if isinstance(expression, dict):  # validation already produced an envelope
        return expression

    try:
        if len(expression) > MAX_EXPRESSION_LENGTH:
            raise _ResourceLimit(
                f"Expression exceeds {MAX_EXPRESSION_LENGTH} characters."
            )

        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError:
            raise _InvalidExpression("The expression is not valid syntax.")

        _check_size_limits(tree)
        result = _evaluate(tree.body)
        result = _finalize_result(result)
    except _EvaluationError as error:
        return _error(error.category, str(error))
    except Exception:
        # Never leak an unexpected traceback to the model or the CLI.
        return _error("internal_error", "The calculation could not be completed.")

    return {"ok": True, "result": result}


# --- Argument validation ------------------------------------------------------


def _validate_arguments(arguments: dict[str, Any]) -> "str | dict[str, Any]":
    """Return the expression string, or an invalid_arguments envelope."""

    if not isinstance(arguments, dict):
        return _error("invalid_arguments", "Arguments must be an object.")
    if set(arguments) != {"expression"}:
        return _error(
            "invalid_arguments", "Arguments must contain exactly 'expression'."
        )

    expression = arguments["expression"]
    if not isinstance(expression, str):
        return _error("invalid_arguments", "'expression' must be a string.")
    if not expression.strip():
        return _error("invalid_arguments", "'expression' must not be empty.")

    return expression


# --- Size / complexity limits -------------------------------------------------


def _check_size_limits(tree: ast.Expression) -> None:
    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > MAX_NODE_COUNT:
        raise _ResourceLimit(f"Expression has more than {MAX_NODE_COUNT} nodes.")

    depth = _depth(tree)
    if depth > MAX_DEPTH:
        raise _ResourceLimit(f"Expression is nested deeper than {MAX_DEPTH} levels.")


def _depth(node: ast.AST) -> int:
    children = list(ast.iter_child_nodes(node))
    if not children:
        return 1
    return 1 + max(_depth(child) for child in children)


# --- Recursive AST evaluator (the allowlist) ----------------------------------


def _evaluate(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        # Only real integers and floats — not bool, str, bytes, complex, None.
        if type(node.value) is int or type(node.value) is float:
            return node.value
        raise _UnsafeExpression("Only numeric literals are allowed.")

    if isinstance(node, ast.BinOp):
        return _evaluate_binop(node)

    if isinstance(node, ast.UnaryOp):
        apply = _UNARY_OPERATORS.get(type(node.op))
        if apply is None:
            raise _UnsafeExpression("This unary operator is not allowed.")
        return apply(_evaluate(node.operand))

    if isinstance(node, (ast.List, ast.Tuple)):
        if len(node.elts) > MAX_SEQUENCE_LENGTH:
            raise _ResourceLimit(
                f"Sequences may hold at most {MAX_SEQUENCE_LENGTH} elements."
            )
        return [_evaluate(element) for element in node.elts]

    if isinstance(node, ast.Call):
        return _evaluate_call(node)

    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTANTS:
            return _ALLOWED_CONSTANTS[node.id]
        raise _UnsafeExpression(f"Name '{node.id}' is not allowed.")

    raise _UnsafeExpression("The expression contains unsupported syntax.")


def _evaluate_binop(node: ast.BinOp) -> Any:
    apply = _BINARY_OPERATORS.get(type(node.op))
    if apply is None:
        raise _UnsafeExpression("This operator is not allowed.")

    left = _evaluate(node.left)
    right = _evaluate(node.right)

    # Guard against building an astronomically large integer via `a ** b`.
    if isinstance(node.op, ast.Pow):
        if type(right) is int and abs(right) > MAX_EXPONENT:
            raise _ResourceLimit(
                f"Exponent magnitude may not exceed {MAX_EXPONENT}."
            )

    try:
        return apply(left, right)
    except ZeroDivisionError:
        raise _CalculationError("Division by zero.")
    except (ValueError, OverflowError) as error:
        raise _CalculationError(_clean_math_message(error))


def _evaluate_call(node: ast.Call) -> Any:
    if not isinstance(node.func, ast.Name):
        raise _UnsafeExpression("Only direct calls to allowed functions are permitted.")
    if node.keywords:
        raise _UnsafeExpression("Keyword arguments are not allowed.")
    if any(isinstance(arg, ast.Starred) for arg in node.args):
        raise _UnsafeExpression("Argument unpacking is not allowed.")

    name = node.func.id
    function = _ALLOWED_FUNCTIONS.get(name)
    if function is None:
        raise _UnsafeExpression(f"Function '{name}' is not allowed.")

    arguments = [_evaluate(arg) for arg in node.args]

    if name == "factorial" and arguments:
        value = arguments[0]
        if type(value) is int and value > MAX_FACTORIAL:
            raise _ResourceLimit(f"factorial argument may not exceed {MAX_FACTORIAL}.")

    try:
        return function(*arguments)
    except ZeroDivisionError:
        raise _CalculationError("Division by zero.")
    except (ValueError, OverflowError) as error:
        raise _CalculationError(_clean_math_message(error))
    except TypeError:
        raise _CalculationError(f"Invalid arguments for '{name}'.")


# --- Result finalization ------------------------------------------------------


def _finalize_result(result: Any) -> Any:
    if isinstance(result, bool) or not isinstance(result, (int, float, list)):
        raise _CalculationError("The result is not a supported numeric value.")

    if isinstance(result, float) and not math.isfinite(result):
        raise _CalculationError("The result is not a finite number.")

    try:
        json.dumps(result)
    except (TypeError, ValueError):
        raise _CalculationError("The result cannot be represented as JSON.")

    return result


def _clean_math_message(error: Exception) -> str:
    message = str(error).strip()
    if not message:
        return "The calculation is not defined for these values."
    # Keep it short and stable; never expose object reprs or paths.
    return message[:120].rstrip(".") + "."


def _error(category: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": category, "message": message}}
