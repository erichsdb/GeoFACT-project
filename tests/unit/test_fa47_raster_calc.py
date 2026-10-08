"""Implements: FA47 (Rasterrechner: raster_calc).

Contract tests of `raster_calc` (builtin/operations/raster_calc.py): arithmetic on
the bands of a raster with a safely evaluated expression. The evaluator is a
whitelist over the `ast` - the tests pin both halves: what it computes
(values, nodata, no `inf`) and what it refuses (everything else, at config time).
"""

from __future__ import annotations

import numpy as np
import pytest
from affine import Affine
from pyproj import CRS

from geofact import api
from geofact.builtin.operations.raster_calc import (
    RasterCalcStep,
    calculate,
    parse_expression,
    referenced_names,
    run_raster_calc,
)
from geofact.core.types import DataType, RasterLayer

TRANSFORM = Affine(10, 0, 350000, 0, -10, 5640000)
CRS33 = CRS.from_epsg(32633)


def _raster(
    bands: dict[str, list[list[float]]], nodata=None, dtype="float64"
) -> RasterLayer:
    data = np.stack([np.array(values, dtype=dtype) for values in bands.values()])
    return RasterLayer(
        data=data,
        transform=TRANSFORM,
        crs=CRS33,
        nodata=nodata,
        band_names=list(bands),
        path="szene",
    )


def _calc(raster: RasterLayer, expression: str, **params) -> RasterLayer:
    return run_raster_calc({"raster": raster}, {"expression": expression, **params})


# =====================================================================
# Values
# =====================================================================


def test_fa47_ndvi_from_two_bands():
    raster = _raster({"red": [[1, 2], [3, 4]], "nir": [[3, 2], [9, 12]]})
    result = _calc(raster, "(nir - red) / (nir + red)", output_band="ndvi")
    assert result.data.dtype == np.float32
    assert result.data.ravel().tolist() == pytest.approx([0.5, 0.0, 0.5, 0.5])
    assert result.band_names == ["ndvi"] and result.data.ndim == 2


def test_fa47_result_keeps_georeferencing_and_default_band_name():
    raster = _raster({"a": [[1, 2]], "b": [[3, 4]]})
    result = _calc(raster, "a + b")
    assert (
        result.transform == TRANSFORM and result.crs == CRS33 and result.path == "szene"
    )
    assert result.band_names == ["result"] and result.data.tolist() == [[4.0, 6.0]]


def test_fa47_unsigned_integer_bands_do_not_wrap_around():
    """Raw digital numbers are uint16: 3 - 5 must be -2, not 65534."""
    raster = _raster({"red": [[5]], "nir": [[3]]}, dtype="uint16")
    assert _calc(raster, "nir - red").data.tolist() == [[-2.0]]


def test_fa47_input_raster_is_not_modified():
    raster = _raster({"a": [[1.0, 2.0]], "b": [[3.0, 4.0]]}, nodata=2.0)
    before = raster.data.copy()
    _calc(raster, "a + b")
    assert (
        np.array_equal(raster.data, before)
        and raster.nodata == 2.0
        and raster.band_names == ["a", "b"]
    )


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("a * 2 + b", [[4.0, 7.0]]),
        ("a ** 2", [[1.0, 4.0]]),
        ("-a + 10", [[9.0, 8.0]]),
        ("a / b", [[0.5, 2.0 / 3.0]]),
        ("abs(a - b)", [[1.0, 1.0]]),
        ("sqrt(b + 6)", [[np.sqrt(8.0), 3.0]]),
        ("clip(a + b, 3, 4)", [[3.0, 4.0]]),
        ("minimum(a, b)", [[1.0, 2.0]]),
        ("maximum(a, b)", [[2.0, 3.0]]),
        ("log(a + 1) * 0 + 7", [[7.0, 7.0]]),
        ("where(a > 1, 100, a)", [[1.0, 100.0]]),
        ("(a > 1) + (b > 2)", [[0.0, 2.0]]),  # truth values count as 0/1 in arithmetic
    ],
)
def test_fa47_operators_and_functions(expression, expected):
    raster = _raster({"a": [[1.0, 2.0]], "b": [[2.0, 3.0]]})
    assert _calc(raster, expression).data.ravel().tolist() == pytest.approx(
        np.ravel(expected)
    )


def test_fa47_unnamed_raster_has_implicit_band_names():
    raster = RasterLayer(
        data=np.array([[[1.0, 2.0]], [[3.0, 4.0]]]), transform=TRANSFORM, crs=CRS33
    )
    assert _calc(raster, "band1 + band2").data.tolist() == [[4.0, 6.0]]
    single = RasterLayer(data=np.array([[1.0, 2.0]]), transform=TRANSFORM, crs=CRS33)
    assert _calc(single, "band1 * 3").data.tolist() == [[3.0, 6.0]]


# =====================================================================
# Truth values: uint8 0/1 with nodata 255
# =====================================================================


def test_fa47_comparison_gives_uint8_zero_one_with_nodata_255():
    raster = _raster({"ndvi": [[0.1, 0.3], [0.5, np.nan]]}, nodata=float("nan"))
    result = _calc(raster, "ndvi > 0.3", output_band="gruen")
    assert result.data.dtype == np.uint8 and result.nodata == 255
    assert result.data.tolist() == [
        [0, 0],
        [1, 255],
    ]  # NaN input stays nodata, never 'false'


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("(a > 1) & (b < 5)", [[0, 1, 0]]),
        ("(a > 1) | (b < 5)", [[0, 1, 1]]),
        ("~(a > 1)", [[1, 0, 0]]),
        ("a >= 2", [[0, 1, 1]]),
        ("a == 2", [[0, 1, 0]]),
        ("a != 2", [[1, 0, 1]]),
        ("a <= 2", [[1, 1, 0]]),
        ("a < b", [[1, 1, 1]]),
    ],
)
def test_fa47_truth_value_expressions(expression, expected):
    raster = _raster({"a": [[1.0, 2.0, 3.0]], "b": [[9.0, 4.0, 6.0]]})
    assert _calc(raster, expression).data.tolist() == expected


def test_fa47_chained_steps_keep_nodata():
    """NDVI (float, NaN nodata) -> mask: cells without NDVI stay nodata in the mask."""
    raster = _raster({"red": [[0, 2]], "nir": [[0, 8]]}, nodata=0.0)
    ndvi = _calc(raster, "(nir - red) / (nir + red)", output_band="ndvi")
    assert np.isnan(ndvi.data[0, 0]) and np.isnan(ndvi.nodata)
    mask = _calc(ndvi, "ndvi > 0.3", output_band="gruen")
    assert mask.data.tolist() == [[255, 1]]


# =====================================================================
# Nodata and non-finite results
# =====================================================================


def test_fa47_nodata_in_any_used_band_makes_the_cell_nodata():
    raster = _raster(
        {"red": [[0.0, 2.0]], "nir": [[5.0, 0.0]], "other": [[0.0, 0.0]]}, nodata=0.0
    )
    result = _calc(raster, "nir + red")
    assert np.isnan(result.data).tolist() == [[True, True]] and np.isnan(result.nodata)
    only_nir = _calc(raster, "nir * 2")  # 'red' is not used: only nir's nodata counts
    assert only_nir.data[0, 0] == 10.0 and np.isnan(only_nir.data[0, 1])


def test_fa47_declared_numeric_nodata_is_honoured():
    raster = _raster({"a": [[-200.0, 3.0]]}, nodata=-200.0)
    assert np.isnan(_calc(raster, "a * 2").data[0, 0])
    assert _calc(raster, "a * 2").data[0, 1] == 6.0


def test_fa47_division_by_zero_is_nodata_not_inf():
    raster = _raster({"a": [[1.0, 0.0, -1.0]], "b": [[0.0, 0.0, 0.0]]})
    result = _calc(raster, "a / b")
    assert np.isnan(result.data).all() and not np.isinf(result.data).any()


def test_fa47_sqrt_of_negative_and_log_of_zero_are_nodata():
    raster = _raster({"a": [[-4.0, 0.0, 4.0]]})
    assert np.isnan(_calc(raster, "sqrt(a)").data).tolist() == [[True, False, False]]
    assert np.isnan(_calc(raster, "log(a)").data).tolist() == [[True, True, False]]


def test_fa47_where_guard_keeps_cells_the_guard_covers():
    """`where(b == 0, 0, a / b)`: the explicit guard decides, the division by zero in the
    unchosen branch must not turn the cell into nodata."""
    raster = _raster({"a": [[1.0, 6.0]], "b": [[0.0, 3.0]]})
    result = _calc(raster, "where(b == 0, 0, a / b)")
    assert result.data.tolist() == [[0.0, 2.0]]


def test_fa47_comparison_with_a_non_finite_operand_is_nodata():
    raster = _raster({"a": [[1.0, 1.0]], "b": [[0.0, 2.0]]})
    result = _calc(raster, "(a / b) > 0.1")
    assert result.data.tolist() == [[255, 1]]


def test_fa47_huge_values_overflow_to_nodata_not_inf():
    raster = _raster({"a": [[1e300, 2.0]]})
    result = _calc(raster, "a * a * a")
    assert np.isnan(result.data[0, 0]) and result.data[0, 1] == 8.0


def test_fa47_empty_raster_gives_an_empty_result():
    """Edge case 'empty layer'."""
    raster = RasterLayer(
        data=np.zeros((2, 0, 0)), transform=TRANSFORM, crs=CRS33, band_names=["a", "b"]
    )
    result = _calc(raster, "a + b")
    assert result.data.shape == (0, 0) and result.band_names == ["result"]


# =====================================================================
# Errors at run time: unknown bands
# =====================================================================


def test_fa47_unknown_band_is_an_explicit_error_naming_the_bands():
    raster = _raster({"red": [[1.0]], "nir": [[2.0]]})
    with pytest.raises(
        ValueError, match=r"unbekannte Bänder \['nri'\].*\['red', 'nir'\]"
    ):
        _calc(raster, "(nri - red) / (nri + red)")


def test_fa47_multi_band_name_of_a_removed_band_is_unknown():
    result = _calc(_raster({"a": [[1.0]]}), "a + 1", output_band="x")
    with pytest.raises(ValueError, match="unbekannte Bänder"):
        _calc(result, "a + 1")  # only 'x' exists now


# =====================================================================
# Safety: the whitelist (config time)
# =====================================================================


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo x')",
        "a.real",
        "a[0]",
        "(lambda: 1)()",
        "[x for x in a]",
        "'text'",
        "a if b else c",
        "a and b",
        "a or b",
        "not a",
        "1 < a < 3",
        "a // b",
        "a % b",
        "a << 1",
        "a ^ b",
        "a @ b",
        "open('x')",
        "eval('1+1')",
        "exec('x=1')",
        "print(a)",
        "sqrt(x=a)",
        "where(a)",
        "abs(a, b)",
        "a in b",
        "a is b",
        "True",
        "None",
        "{1: a}",
        "a := 1",
        "(a, b)",
        "f'{a}'",
        "await a",
        "b'x' + a",
        "a.sqrt()",
        "np.sqrt(a)",
    ],
)
def test_fa47_whitelist_rejects_everything_else_at_config_time(expression):
    with pytest.raises(ValueError):
        parse_expression(expression)
    with pytest.raises(ValueError):
        RasterCalcStep.Params(expression=expression)


@pytest.mark.parametrize(
    "expression, fragment",
    [
        ("", "leer"),
        ("   ", "leer"),
        ("2 + 3", "kein Band"),
        ("sqrt(4)", "kein Band"),
        ("a +", "Syntaxfehler"),
        ("(a", "Syntaxfehler"),
        ("a and b", "'&'"),
        ("1 < a < 3", "Vergleichsketten"),
        ("a.real", "Attribute"),
        ("foo(a)", "erlaubte Funktionen"),
        ("sqrt(x=a)", "Schlüsselwort"),
        ("where(a)", "3 Argument"),
        ("'text'", "Zahlen"),
        ("a if b else c", "IfExp"),
    ],
)
def test_fa47_rejections_name_the_problem(expression, fragment):
    with pytest.raises(ValueError, match=fragment):
        parse_expression(expression)


def test_fa47_overlong_and_deeply_nested_expressions_are_rejected_cleanly():
    with pytest.raises(ValueError, match="zu lang"):
        parse_expression("a + " * 400 + "a")
    with pytest.raises(ValueError):
        parse_expression("(" * 300 + "a" + ")" * 300)


def test_fa47_the_position_of_the_problem_is_reported():
    with pytest.raises(ValueError, match=r"Zeichen 5"):
        parse_expression("a + foo(b)")


def test_fa47_a_function_name_used_as_a_band_name_is_just_a_band():
    """`abs` as a bare name is a band, `abs(...)` is the function."""
    tree = parse_expression("abs(abs) + abs")
    assert referenced_names(tree) == ["abs"]
    raster = _raster({"abs": [[-3.0]]})
    assert _calc(raster, "abs(abs) + abs").data.tolist() == [[0.0]]


def test_fa47_and_or_need_truth_values_not_numbers():
    raster = _raster({"a": [[1.0]], "b": [[2.0]]})
    with pytest.raises(ValueError, match="Wahrheitswerte"):
        _calc(raster, "a & b")
    with pytest.raises(ValueError, match="Wahrheitswerte"):
        _calc(raster, "~a")
    with pytest.raises(ValueError, match="Wahrheitswerte"):
        _calc(raster, "where(a, 1, 2)")


def test_fa47_expression_without_a_value_per_cell_is_refused():
    raster = _raster({"a": [[1.0]]})
    # references a band, but the result of clip() of constants only would not; guard the evaluator itself
    with pytest.raises(ValueError):
        calculate(raster, "2 * 3", "x")


# =====================================================================
# Contract and wiring
# =====================================================================


def test_fa47_output_band_must_be_a_name_usable_in_expressions():
    with pytest.raises(ValueError, match="output_band"):
        RasterCalcStep.Params(expression="a + 1", output_band="not valid")
    assert RasterCalcStep.Params(expression="a + 1").output_band == "result"


def test_fa47_unknown_param_is_rejected():
    with pytest.raises(ValueError, match="extra|Extra"):
        RasterCalcStep.Params(expression="a + 1", dtype="float32")


def test_fa47_contract_ports_and_types():
    assert RasterCalcStep.INPUT_PORTS == {"raster": DataType.RASTER}
    assert RasterCalcStep.OUTPUT_TYPE == DataType.RASTER
    entry = {e["op"]: e for e in api.operation_catalog()}["raster_calc"]
    assert (
        entry["input_ports"] == {"raster": "raster"}
        and entry["output_type"] == "raster"
    )
    assert entry["required_params"] == ["expression"]
    assert entry["params"]["output_band"]["default"] == "result"


def _scenario(raster_source: dict, expression: str = "a + 1") -> dict:
    return {
        "scenario": {"name": "t", "region": "13.0,50.9,13.1,51.0"},
        "layers": [raster_source],
        "steps": [
            {
                "id": "z",
                "op": "raster_calc",
                "inputs": {"raster": raster_source["id"]},
                "params": {"expression": expression},
            }
        ],
        "output": [{"type": "geotiff", "source": "z"}],
    }


def test_fa47_a_vector_layer_on_the_raster_port_is_a_type_error_at_config_time():
    issues = api.validate(
        _scenario(
            {"id": "v", "source": "file", "path": "x.geojson", "data_type": "vector"}
        )
    ).issues
    assert (
        issues
        and "raster" in issues[0][1]
        and issues[0][0] == "steps -> 0 -> inputs -> raster"
    )


def test_fa47_invalid_expression_in_a_scenario_is_reported_with_its_position():
    issues = api.validate(
        _scenario(
            {
                "id": "r",
                "source": "file",
                "path": "x.tif",
                "data_type": "raster",
                "format": "tif",
            },
            expression="a and b",
        )
    ).issues
    assert issues and issues[0][0] == "steps -> 0 -> params -> expression"
    assert "&" in issues[0][1]


def test_fa47_valid_scenario_validates():
    assert api.validate(
        _scenario(
            {
                "id": "r",
                "source": "file",
                "path": "x.tif",
                "data_type": "raster",
                "format": "tif",
            },
            expression="(band1 - band2) / (band1 + band2)",
        )
    ).valid
