"""Implements: FA47 (Rasterrechner: raster_calc, blockweise, precision).

Berechnet aus den Bändern eines Rasters ein neues Ein-Band-Raster nach einem
Ausdruck, z. B. NDVI aus Sentinel-2:

    expression: "(nir - red) / (nir + red)"        output_band: ndvi
    expression: "ndvi > 0.3"                       output_band: gruen

Der Ausdruck ist ein Python-Ausdruck, der nicht mit ``eval`` ausgeführt, sondern
mit ``ast`` geparst und von einem eigenen Auswerter berechnet wird; erlaubt ist
nur eine Positivliste:

- Zahlen, Bandnamen (die Namen des Eingaberasters; ein unbenanntes Raster hat
  ``band1`` ... ``bandN``)
- ``+ - * / **``, Vorzeichen ``-``
- Vergleiche ``< <= > >= == !=`` (keine Ketten) und ``& | ~`` auf Vergleichen
  (Klammern setzen: ``(a > 1) & (b < 2)``; ``and``/``or``/``not`` gibt es nicht)
- Funktionen ``where(bedingung, a, b)``, ``abs``, ``sqrt``, ``log``,
  ``clip(x, unten, oben)``, ``minimum``, ``maximum``

Alles andere wird schon bei der Konfigurationsprüfung abgelehnt. Die Bänder des
Rasters stehen erst zur Laufzeit fest; ein unbekannter Bandname ist ein
Laufzeitfehler vor der Rechnung, der die vorhandenen Bänder nennt.

Nodata: eine Zelle ist Nodata, sobald eines der verwendeten Bänder dort Nodata
ist. Nicht endliche Ergebnisse (Division durch 0, ``sqrt`` einer negativen Zahl,
``log(0)``) werden ebenfalls Nodata, nie ``inf``; ein Vergleich mit einem nicht
endlichen Operanden ist Nodata, nicht ``falsch``. Ein Wahrheitswert-Ausdruck ergibt
``uint8`` 0/1 mit Nodata 255, sonst ``float32`` mit Nodata NaN. Gerechnet wird in
``float64`` (``precision: float32`` halbiert den Arbeitsspeicher, rundet aber jeden
Zwischenwert).

Blockweise: der Ausdruck wird in Zeilenblöcken von ``BLOCK_ROWS`` Zeilen
ausgewertet, nur ein Block liegt gleichzeitig im Speicher. Alle Operatoren wirken
zellweise, das Ergebnis ist bitgleich zu einer Auswertung in einem Stück."""

from __future__ import annotations

import ast
from typing import Any, ClassVar, Literal

import numpy as np
from pydantic import Field, field_validator

from geofact.plugin_api import (
    DataType,
    ParamsBase,
    RasterLayer,
    register_operation,
    Step,
)

MAX_EXPRESSION_LENGTH = 1000
UINT8_NODATA = 255
BLOCK_ROWS = 512
"""Zeilen je Auswertungsblock (zur Laufzeit gelesen)."""
PRECISIONS = {"float64": np.float64, "float32": np.float32}

_FUNCTION_ARITY = {
    "where": 3,
    "abs": 1,
    "sqrt": 1,
    "log": 1,
    "clip": 3,
    "minimum": 2,
    "maximum": 2,
}
_ARITHMETIC = {
    ast.Add: np.add,
    ast.Sub: np.subtract,
    ast.Mult: np.multiply,
    ast.Div: np.divide,
    ast.Pow: np.power,
}
_LOGIC = {ast.BitAnd: np.logical_and, ast.BitOr: np.logical_or}
_COMPARE = {
    ast.Lt: np.less,
    ast.LtE: np.less_equal,
    ast.Gt: np.greater,
    ast.GtE: np.greater_equal,
    ast.Eq: np.equal,
    ast.NotEq: np.not_equal,
}
_OPERATOR_SYMBOL = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.Pow: "**",
    ast.BitAnd: "&",
    ast.BitOr: "|",
    ast.USub: "-",
    ast.UAdd: "+",
    ast.Invert: "~",
}


# --- Parsen und Positivliste (Konfigurationszeit) ---


class ExpressionError(ValueError):
    """Ungültiger Rasterausdruck (Meldung nennt Ausdruck und Position)."""


def _fail(expression: str, node: ast.AST | None, problem: str) -> ExpressionError:
    where = (
        f" (Zeichen {node.col_offset + 1})"
        if node is not None and hasattr(node, "col_offset")
        else ""
    )
    return ExpressionError(f"Ausdruck '{expression}'{where}: {problem}")


def _check_node(expression: str, node: ast.AST) -> None:
    """Prüft rekursiv, dass nur Positivlisten-Knoten vorkommen."""
    if isinstance(node, ast.Expression):
        _check_node(expression, node.body)
    elif isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _fail(
                expression,
                node,
                f"nur Zahlen sind als Konstante erlaubt, nicht {node.value!r}",
            )
    elif isinstance(node, ast.Name):
        pass
    elif isinstance(node, ast.UnaryOp):
        if type(node.op) not in (ast.USub, ast.UAdd, ast.Invert):
            raise _fail(
                expression,
                node,
                "Operator 'not' gibt es nicht - '~' auf Vergleichen verwenden",
            )
        _check_node(expression, node.operand)
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _ARITHMETIC and type(node.op) not in _LOGIC:
            raise _fail(
                expression,
                node,
                f"Operator '{type(node.op).__name__}' nicht erlaubt (erlaubt: + - * / ** & |)",
            )
        _check_node(expression, node.left)
        _check_node(expression, node.right)
    elif isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise _fail(
                expression,
                node,
                "Vergleichsketten (a < b < c) sind nicht erlaubt - zwei Vergleiche mit & verbinden",
            )
        if type(node.ops[0]) not in _COMPARE:
            raise _fail(expression, node, "erlaubte Vergleiche: < <= > >= == !=")
        _check_node(expression, node.left)
        _check_node(expression, node.comparators[0])
    elif isinstance(node, ast.BoolOp):
        raise _fail(
            expression,
            node,
            "'and'/'or' gibt es nicht - '&' bzw. '|' auf Vergleichen verwenden",
        )
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTION_ARITY:
            raise _fail(
                expression, node, f"erlaubte Funktionen: {sorted(_FUNCTION_ARITY)}"
            )
        if node.keywords:
            raise _fail(expression, node, "Schlüsselwort-Argumente sind nicht erlaubt")
        expected = _FUNCTION_ARITY[node.func.id]
        if len(node.args) != expected:
            raise _fail(
                expression,
                node,
                f"{node.func.id}() erwartet {expected} Argument(e), nicht {len(node.args)}",
            )
        for argument in node.args:
            _check_node(expression, argument)
    else:
        raise _fail(
            expression,
            node,
            f"'{type(node).__name__}' ist in einem Rasterausdruck nicht erlaubt",
        )


def parse_expression(expression: str) -> ast.Expression:
    """Parst und prüft einen Rasterausdruck gegen die Positivliste;
    ExpressionError nennt Ausdruck und Zeichenposition."""
    if not isinstance(expression, str) or not expression.strip():
        raise ExpressionError("Ausdruck darf nicht leer sein")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(
            f"Ausdruck zu lang ({len(expression)} Zeichen, höchstens {MAX_EXPRESSION_LENGTH})"
        )
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(
            f"Ausdruck '{expression}': Syntaxfehler (Zeichen {exc.offset or '?'}): {exc.msg}"
        ) from None
    except (RecursionError, MemoryError, ValueError) as exc:
        raise ExpressionError(
            f"Ausdruck '{expression}': nicht auswertbar ({type(exc).__name__})"
        ) from None
    try:
        _check_node(expression, tree)
    except RecursionError:
        raise ExpressionError(
            f"Ausdruck '{expression}': zu tief verschachtelt"
        ) from None
    if not referenced_names(tree):
        raise ExpressionError(
            f"Ausdruck '{expression}' verwendet kein Band - er müsste ein Band-Name sein "
            "(z. B. nir) und ergäbe sonst nur eine Zahl"
        )
    return tree


def referenced_names(tree: ast.AST) -> list[str]:
    """Bandnamen im Ausdruck (ohne Funktionsnamen), in Reihenfolge des ersten Auftretens."""
    function_nodes = {
        id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    names: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and id(node) not in function_nodes
            and node.id not in names
        ):
            names.append(node.id)
    return names


# --- Auswertung (Laufzeit) ---


class _Evaluator:
    """Berechnet den Syntaxbaum über NumPy-Arrays. ``invalid`` sammelt Zellen, an
    denen ein Vergleich einen nicht endlichen Operanden hatte (dort ist das
    Ergebnis Nodata, nicht ``falsch``)."""

    def __init__(
        self, expression: str, bands: dict[str, np.ndarray], shape: tuple[int, int]
    ) -> None:
        self.expression = expression
        self.bands = bands
        self.invalid = np.zeros(shape, dtype=bool)

    def evaluate(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return self.evaluate(node.body)
        if isinstance(node, ast.Constant):
            try:
                return float(node.value)
            except OverflowError:
                raise _fail(
                    self.expression, node, f"Zahl {str(node.value)[:20]}... ist zu groß"
                ) from None
        if isinstance(node, ast.Name):
            return self.bands[node.id]
        if isinstance(node, ast.UnaryOp):
            operand = self.evaluate(node.operand)
            if isinstance(node.op, ast.Invert):
                return np.logical_not(self._boolean(operand, node, "~"))
            operand = self._numeric(operand)
            return -operand if isinstance(node.op, ast.USub) else operand
        if isinstance(node, ast.BinOp):
            left, right = self.evaluate(node.left), self.evaluate(node.right)
            if type(node.op) in _LOGIC:
                symbol = _OPERATOR_SYMBOL[type(node.op)]
                return _LOGIC[type(node.op)](
                    self._boolean(left, node, symbol),
                    self._boolean(right, node, symbol),
                )
            return _ARITHMETIC[type(node.op)](self._numeric(left), self._numeric(right))
        if isinstance(node, ast.Compare):
            left = self._numeric(self.evaluate(node.left))
            right = self._numeric(self.evaluate(node.comparators[0]))
            for operand in (left, right):
                if isinstance(operand, np.ndarray):
                    self.invalid |= ~np.isfinite(operand)
            return _COMPARE[type(node.ops[0])](left, right)
        if isinstance(node, ast.Call):
            return self._call(node)
        raise _fail(
            self.expression, node, f"'{type(node).__name__}' nicht auswertbar"
        )  # pragma: no cover

    def _call(self, node: ast.Call) -> Any:
        name = node.func.id
        arguments = [self.evaluate(argument) for argument in node.args]
        if name == "where":
            condition = self._boolean(arguments[0], node, "where(bedingung, ...)")
            return np.where(
                condition, self._numeric(arguments[1]), self._numeric(arguments[2])
            )
        numbers = [self._numeric(argument) for argument in arguments]
        return {
            "abs": np.abs,
            "sqrt": np.sqrt,
            "log": np.log,
            "clip": np.clip,
            "minimum": np.minimum,
            "maximum": np.maximum,
        }[name](*numbers)

    def _numeric(self, value: Any) -> Any:
        """Wahrheitswerte rechnen als 0/1 (sonst würde True + True ein ODER)."""
        if isinstance(value, np.ndarray) and value.dtype == bool:
            return value.astype(np.float64)
        return value

    def _boolean(self, value: Any, node: ast.AST, where: str) -> np.ndarray:
        if not (isinstance(value, np.ndarray) and value.dtype == bool):
            raise _fail(
                self.expression,
                node,
                f"'{where}' erwartet Wahrheitswerte (Vergleiche wie 'ndvi > 0.3'), "
                "nicht Zahlen - Klammern um Vergleiche setzen",
            )
        return value


def _block_valid(block: np.ndarray, nodata: float | None) -> np.ndarray:
    """Gültige Zellen eines Bandblocks (wie ``RasterLayer.valid_mask``)."""
    if nodata is None:
        return np.ones(block.shape, dtype=bool)
    if isinstance(nodata, float) and np.isnan(nodata):
        return ~np.isnan(block)
    return block != nodata


def _evaluate_block(
    tree: ast.Expression,
    expression: str,
    raster: RasterLayer,
    names: list[str],
    rows: slice,
    dtype: type,
) -> tuple[np.ndarray, bool]:
    """Wertet den Ausdruck auf den Zeilen ``rows`` aus: (Ergebnisblock, ist
    Wahrheitswert). Nodata ist im Block schon gesetzt (255 bzw. NaN)."""
    raw = {name: raster.band_data(name)[rows] for name in names}
    shape = next(iter(raw.values())).shape
    valid = np.ones(shape, dtype=bool)
    for block in raw.values():
        valid &= _block_valid(block, raster.nodata)
    bands = {name: block.astype(dtype) for name, block in raw.items()}
    evaluator = _Evaluator(expression, bands, shape)
    with np.errstate(all="ignore"):
        result = evaluator.evaluate(tree)
        if not isinstance(result, np.ndarray) or result.shape != shape:
            raise ValueError(
                f"raster_calc: Ausdruck '{expression}' ergibt keinen Wert je Zelle"
            )
        nodata_cells = ~valid | evaluator.invalid
        if result.dtype == bool:
            data = result.astype(np.uint8)
            data[nodata_cells] = UINT8_NODATA
            return data, True
        data = result.astype(np.float32)
        data[nodata_cells | ~np.isfinite(data)] = np.nan
        return data, False


def calculate(
    raster: RasterLayer, expression: str, output_band: str, precision: str = "float64"
) -> RasterLayer:
    """Wertet ``expression`` auf den Bändern von ``raster`` blockweise aus."""
    tree = parse_expression(expression)
    names = referenced_names(tree)
    available = raster.names
    unknown = [name for name in names if name not in available]
    if unknown:
        origin = f" '{raster.path}'" if raster.path else ""
        raise ValueError(
            f"raster_calc: Ausdruck '{expression}' nennt unbekannte Bänder {unknown}; "
            f"das Eingaberaster{origin} hat die Bänder {available}"
        )
    if precision not in PRECISIONS:
        raise ValueError(
            f"raster_calc: precision '{precision}', erlaubt sind {sorted(PRECISIONS)}"
        )
    dtype = PRECISIONS[precision]

    total_rows = raster.shape[0]
    step = max(1, int(BLOCK_ROWS))
    data: np.ndarray | None = None
    is_bool = False
    # mindestens ein Block, auch bei 0 Zeilen (leeres Raster -> leeres Ergebnis)
    for start in range(0, max(total_rows, 1), step):
        rows = slice(start, min(start + step, total_rows))
        block, block_bool = _evaluate_block(
            tree, expression, raster, names, rows, dtype
        )
        if data is None:
            is_bool = block_bool
            data = np.empty(raster.shape, dtype=block.dtype)
        elif block_bool != is_bool:  # pragma: no cover - der Ausdruck bestimmt den Typ
            raise ValueError(
                f"raster_calc: Ausdruck '{expression}' wechselt den Ergebnistyp"
            )
        data[rows] = block
    nodata: float = UINT8_NODATA if is_bool else float("nan")
    return RasterLayer(
        data=data,
        transform=raster.transform,
        crs=raster.crs,
        path=raster.path,
        nodata=nodata,
        band_names=[output_band],
    )


class RasterCalcStep(Step):
    op: Literal["raster_calc"] = "raster_calc"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"raster": DataType.RASTER}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.RASTER

    class Params(ParamsBase):
        expression: str = Field(
            ...,
            description="Ausdruck über die Bandnamen des Rasters, z. B. "
            "'(nir - red) / (nir + red)' oder 'ndvi > 0.3'. Erlaubt: Zahlen, "
            "Bandnamen, + - * / **, Vergleiche, & | ~ auf Vergleichen und die "
            "Funktionen where, abs, sqrt, log, clip, minimum, maximum.",
        )
        output_band: str = Field(
            "result",
            description="Name des Ergebnisbandes (in Folgeschritten als Bandname "
            "verwendbar).",
        )
        precision: Literal["float64", "float32"] = Field(
            "float64",
            description="Rechengenauigkeit der Zwischenwerte: float64 (Default) oder "
            "float32 (halber Arbeitsspeicher, Rundung auf float32). Das "
            "Ergebnis ist in beiden Fällen float32 bzw. uint8.",
        )

        @field_validator("expression")
        @classmethod
        def expression_is_valid(cls, value: str) -> str:
            parse_expression(value)
            return value

        @field_validator("output_band")
        @classmethod
        def output_band_is_identifier(cls, value: str) -> str:
            if not value.isidentifier():
                raise ValueError(
                    f"output_band '{value}' muss ein Name aus Buchstaben, Ziffern und Unterstrich sein "
                    "(er wird in Ausdrücken als Bandname verwendet)"
                )
            return value


@register_operation(RasterCalcStep)
def run_raster_calc(inputs: dict, params: dict) -> RasterLayer:
    return calculate(
        inputs["raster"],
        params["expression"],
        params.get("output_band", "result"),
        params.get("precision", "float64"),
    )
