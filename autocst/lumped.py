"""Linear RLC loads: explicit units, bounded expressions and CST readback checks."""
from __future__ import annotations

import ast
import math
import re


VALUE_FIELDS = ("resistance_ohm", "inductance_nh", "capacitance_pf")
POINT_FIELDS = ("point1_mm", "point2_mm")
UNITS = {"coordinates": "mm", "resistance": "ohm", "inductance": "nH",
         "capacitance": "pF", "native_inductance": "H", "native_capacitance": "F"}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _tree(value: str) -> ast.Expression:
    if not value.strip() or len(value) > 160:
        raise ValueError("Lumped expressions must contain 1 to 160 characters")
    try:
        tree = ast.parse(value, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError("Invalid lumped expression") from exc
    nodes = list(ast.walk(tree))
    allowed = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp,
               ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub)
    if len(nodes) > 64 or any(not isinstance(node, allowed) for node in nodes):
        raise ValueError("Lumped expressions allow only numbers, parameters and + - * /")
    for node in nodes:
        if isinstance(node, ast.Name) and not _IDENTIFIER.fullmatch(node.id):
            raise ValueError("Invalid lumped parameter name")
        if isinstance(node, ast.Constant):
            _finite(node.value)
    return tree


def _finite(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Lumped values must be finite numbers or arithmetic expressions")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Lumped value must be finite") from exc
    if not math.isfinite(result):
        raise ValueError("Lumped value must be finite")
    return result


def _resolve(value, parameters: dict) -> float:
    if not isinstance(value, str):
        return _finite(value)
    tree = _tree(value)

    def visit(node):
        if isinstance(node, ast.Constant):
            return _finite(node.value)
        if isinstance(node, ast.Name):
            if node.id not in parameters:
                raise ValueError(f"Unknown lumped parameter: {node.id}")
            return _finite(parameters[node.id])
        if isinstance(node, ast.UnaryOp):
            number = visit(node.operand)
            return number if isinstance(node.op, ast.UAdd) else -number
        left, right = visit(node.left), visit(node.right)
        if isinstance(node.op, ast.Add):
            number = left + right
        elif isinstance(node.op, ast.Sub):
            number = left - right
        elif isinstance(node.op, ast.Mult):
            number = left * right
        else:
            if right == 0:
                raise ValueError("Division by zero in lumped expression")
            number = left / right
        return _finite(number)

    return _finite(visit(tree.body))


def referenced_parameters(elements) -> set[str]:
    if not isinstance(elements, list) or len(elements) > 128:
        raise ValueError("lumped_elements must be a list of at most 128 elements")
    names = set()
    for element in elements:
        if not isinstance(element, dict):
            raise ValueError("Each lumped element must be an object")
        values = [element.get(key, 0) for key in VALUE_FIELDS]
        for key in POINT_FIELDS:
            point = element.get(key)
            if not isinstance(point, list) or len(point) != 3:
                raise ValueError(f"{key} must contain three coordinates in mm")
            values.extend(point)
        for value in values:
            if isinstance(value, str):
                names.update(node.id for node in ast.walk(_tree(value)) if isinstance(node, ast.Name))
            else:
                _finite(value)
    return names


def normalize_lumped_elements(elements, parameters: dict) -> list[dict]:
    referenced_parameters(elements)
    result, names = [], set()
    fields = {"name", "type", "monitor", *VALUE_FIELDS, *POINT_FIELDS}
    for element in elements:
        unknown = set(element) - fields
        if unknown:
            raise ValueError(f"Unsupported lumped fields: {sorted(unknown)}")
        name = element.get("name")
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
            raise ValueError("Lumped name must be an ASCII identifier of at most 64 characters")
        if name in names:
            raise ValueError(f"Duplicate lumped element name: {name}")
        names.add(name)
        kind = element.get("type", "rlcserial")
        if not isinstance(kind, str) or kind not in {"rlcserial", "rlcparallel"}:
            raise ValueError("Lumped type must be rlcserial or rlcparallel")
        monitor = element.get("monitor", True)
        if not isinstance(monitor, bool):
            raise ValueError("Lumped monitor must be boolean")
        canonical = {"name": name, "type": kind, "monitor": monitor}
        resolved = []
        for field in VALUE_FIELDS:
            raw = element.get(field, 0)
            value = _resolve(raw, parameters)
            if value < 0:
                raise ValueError(f"{name}.{field} must be nonnegative")
            resolved.append(value)
            canonical[field] = raw.strip() if isinstance(raw, str) else value
        if not any(resolved):
            raise ValueError("An RLC element needs at least one positive value; ideal open/short is not supported")
        points = []
        for field in POINT_FIELDS:
            raw = element[field]
            points.append([_resolve(value, parameters) for value in raw])
            canonical[field] = [value.strip() if isinstance(value, str) else _finite(value) for value in raw]
        if math.dist(*points) <= 1e-12:
            raise ValueError("Lumped endpoints must be distinct")
        result.append(canonical)
    return result


def lumped_metadata(elements, parameters: dict) -> dict:
    result = []
    for element in normalize_lumped_elements(elements, parameters):
        resolved = {**element, **{key: _resolve(element[key], parameters) for key in VALUE_FIELDS},
                    **{key: [_resolve(value, parameters) for value in element[key]] for key in POINT_FIELDS}}
        resolved["native_si"] = {"resistance_ohm": resolved["resistance_ohm"],
                                 "inductance_h": resolved["inductance_nh"] * 1e-9,
                                 "capacitance_f": resolved["capacitance_pf"] * 1e-12}
        for original, converted in zip((resolved[key] for key in VALUE_FIELDS), resolved["native_si"].values()):
            if not math.isfinite(converted) or (original > 0 and converted == 0):
                raise ValueError("Lumped SI conversion is outside representable numeric range")
        result.append(resolved)
    return {"elements": result, "dissipative": any(item["resistance_ohm"] > 0 for item in result),
            "units": dict(UNITS), "value_binding": "expressions resolved and frozen for each new run",
            "monitor_readback": "no documented getter; verify exported monitor curves separately",
            "scope": "Ideal passive linear RLC; no nonlinear device or parasitic-model validation"}


def render_lumped_history(elements, parameters: dict) -> str:
    resolved = lumped_metadata(elements, parameters)["elements"]
    if not resolved:
        return ""
    script = '''
' AutoCST linear lumped elements; numeric inputs are resolved per run.
If Units.GetUnit("Length") <> "mm" Then
    Err.Raise 513, "AutoCST", "Lumped coordinates require a project using mm"
End If
Dim autoCST_LEType As String
Dim autoCST_LER As Double
Dim autoCST_LEL As Double
Dim autoCST_LEC As Double
Dim autoCST_LEGs As Double
Dim autoCST_LEI0 As Double
Dim autoCST_LETemp As Double
Dim autoCST_LERadius As Double
Dim autoCST_LEX1 As Double
Dim autoCST_LEY1 As Double
Dim autoCST_LEZ1 As Double
Dim autoCST_LEX2 As Double
Dim autoCST_LEY2 As Double
Dim autoCST_LEZ2 As Double
'''
    properties = "autoCST_LEType, autoCST_LER, autoCST_LEL, autoCST_LEC, autoCST_LEGs, autoCST_LEI0, autoCST_LETemp, autoCST_LERadius"
    coordinates = "autoCST_LEX1, autoCST_LEY1, autoCST_LEZ1, autoCST_LEX2, autoCST_LEY2, autoCST_LEZ2"
    for element in resolved:
        name, kind = element["name"], element["type"]
        r, l, c = element["native_si"].values()
        p1, p2 = element["point1_mm"], element["point2_mm"]
        point = lambda values: ", ".join(f'"{value:.17g}"' for value in values)
        script += f'''If LumpedElement.GetProperties("{name}", {properties}) Then
    Err.Raise 513, "AutoCST", "Lumped name already exists: {name}"
End If
With LumpedElement
    .Reset
    .SetName "{name}"
    .Folder ""
    .SetType "{kind}"
    .SetR "{r:.17g}"
    .SetL "{l:.17g}"
    .SetC "{c:.17g}"
    .SetP1 "False", {point(p1)}
    .SetP2 "False", {point(p2)}
    .SetInvert "False"
    .SetMonitor "{str(element['monitor'])}"
    .SetRadius "0"
    .Create
End With
If Not LumpedElement.GetProperties("{name}", {properties}) Then
    Err.Raise 513, "AutoCST", "Cannot read created lumped properties: {name}"
End If
If LCase(autoCST_LEType) <> "{kind}" Then
    Err.Raise 513, "AutoCST", "Lumped circuit type differs from requested type"
End If
'''
        for variable, expected in zip(("autoCST_LER", "autoCST_LEL", "autoCST_LEC"), (r, l, c)):
            tolerance = max(abs(expected) * 1e-8, 1e-24)
            script += f'If Abs({variable} - ({expected:.17g})) > {tolerance:.17g} Then Err.Raise 513, "AutoCST", "Lumped RLC readback mismatch"\n'
        script += f'''If Not LumpedElement.GetCoordinates("{name}", {coordinates}) Then
    Err.Raise 513, "AutoCST", "Cannot read created lumped coordinates"
End If
'''
        for variable, expected in zip(coordinates.split(", "), p1 + p2):
            tolerance = max(abs(expected) * 1e-8, 1e-9)
            script += f'If Abs({variable} - ({expected:.17g})) > {tolerance:.17g} Then Err.Raise 513, "AutoCST", "Lumped endpoint readback mismatch"\n'
    return script
