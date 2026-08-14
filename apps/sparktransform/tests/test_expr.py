import pytest

from apps.core.exceptions import TransformExpressionError
from apps.sparktransform.expr import (
    BinOp,
    Cast,
    FieldRef,
    FuncCall,
    Negate,
    NumberLiteral,
    compile_to_spark_sql,
    evaluate_transform_expr,
    parse_transform_expr,
    validate_source_field,
    validate_transform_expr,
)

# --- parsing: accepted shapes ------------------------------------------


def test_parses_a_bare_field_reference():
    assert parse_transform_expr("premium") == FieldRef(name="premium")


def test_parses_a_dotted_field_reference():
    assert parse_transform_expr("policy.premium") == FieldRef(name="policy.premium")


def test_parses_a_number_literal():
    assert parse_transform_expr("42") == NumberLiteral(value=42.0)
    assert parse_transform_expr("3.5") == NumberLiteral(value=3.5)


@pytest.mark.parametrize("func_name", ["TRIM", "trim", "Upper", "LOWER"])
def test_parses_whitelisted_function_calls_case_insensitively(func_name):
    node = parse_transform_expr(f"{func_name}(name)")
    assert isinstance(node, FuncCall)
    assert node.name == func_name.upper()
    assert node.arg == FieldRef(name="name")


@pytest.mark.parametrize("type_name", ["INTEGER", "decimal", "Text", "DATE", "boolean"])
def test_parses_cast_with_every_allowed_type(type_name):
    node = parse_transform_expr(f"CAST(premium AS {type_name})")
    assert isinstance(node, Cast)
    assert node.target_type == type_name.upper()
    assert node.expr == FieldRef(name="premium")


def test_parses_arithmetic_with_correct_precedence():
    node = parse_transform_expr("1 + 2 * 3")
    inner = BinOp(op="*", left=NumberLiteral(2.0), right=NumberLiteral(3.0))
    assert node == BinOp(op="+", left=NumberLiteral(1.0), right=inner)


def test_parses_parenthesized_grouping():
    node = parse_transform_expr("(1 + 2) * 3")
    inner = BinOp(op="+", left=NumberLiteral(1.0), right=NumberLiteral(2.0))
    assert node == BinOp(op="*", left=inner, right=NumberLiteral(3.0))


def test_parses_unary_negation():
    assert parse_transform_expr("-premium") == Negate(expr=FieldRef(name="premium"))


def test_parses_nested_function_and_cast():
    node = parse_transform_expr("UPPER(TRIM(name))")
    assert node == FuncCall(name="UPPER", arg=FuncCall(name="TRIM", arg=FieldRef(name="name")))


# --- parsing: rejected shapes -------------------------------------------


@pytest.mark.parametrize(
    "bad_expr",
    [
        "",
        "   ",
        "premium +",
        "+ premium",
        "(premium",
        "premium)",
        "EVAL(premium)",
        "__import__('os')",
        "premium; DROP TABLE x",
        "CAST(premium AS VARCHAR)",  # not in the type whitelist
        "SUM(premium)",  # not in the function whitelist
        "premium.",
        ".premium",
        "1 2",
        "premium AS x",
    ],
)
def test_rejects_expressions_outside_the_grammar(bad_expr):
    with pytest.raises(TransformExpressionError):
        parse_transform_expr(bad_expr)


def test_rejects_expression_exceeding_max_length():
    too_long = "premium" + " + 1" * 200
    with pytest.raises(TransformExpressionError):
        parse_transform_expr(too_long)


def test_rejects_non_string_input():
    with pytest.raises(TransformExpressionError):
        parse_transform_expr(None)  # type: ignore[arg-type]


def test_validate_transform_expr_raises_for_invalid_and_is_silent_for_valid():
    validate_transform_expr("UPPER(name)")  # does not raise
    with pytest.raises(TransformExpressionError):
        validate_transform_expr("eval(1)")


# --- validate_source_field -----------------------------------------------


@pytest.mark.parametrize("good_field", ["premium", "policy.premium", "a.b.c", "_private"])
def test_validate_source_field_accepts_dotted_paths(good_field):
    validate_source_field(good_field)  # does not raise


@pytest.mark.parametrize(
    "bad_field", ["", "premium*", "policy[0]", "a..b", "'; DROP TABLE x", None]
)
def test_validate_source_field_rejects_unsafe_input(bad_field):
    with pytest.raises(TransformExpressionError):
        validate_source_field(bad_field)


# --- reference evaluator ---------------------------------------------------


def test_evaluate_field_reference():
    assert evaluate_transform_expr("policy.premium", {"policy": {"premium": 100}}) == 100


def test_evaluate_missing_field_is_none():
    assert evaluate_transform_expr("missing", {"other": 1}) is None


def test_evaluate_trim_upper_lower():
    record = {"name": "  Alice  "}
    assert evaluate_transform_expr("TRIM(name)", record) == "Alice"
    assert evaluate_transform_expr("UPPER(name)", record) == "  ALICE  "
    assert evaluate_transform_expr("LOWER(name)", record) == "  alice  "


def test_evaluate_arithmetic():
    assert evaluate_transform_expr("1 + 2 * 3", {}) == 7
    assert evaluate_transform_expr("(1 + 2) * 3", {}) == 9
    assert evaluate_transform_expr("premium / 2", {"premium": 10}) == 5


def test_evaluate_negation():
    assert evaluate_transform_expr("-premium", {"premium": 5}) == -5


def test_evaluate_cast_integer_decimal_text_boolean():
    record = {"raw": "42"}
    assert evaluate_transform_expr("CAST(raw AS INTEGER)", record) == 42
    assert evaluate_transform_expr("CAST(raw AS DECIMAL)", record) == 42.0
    assert evaluate_transform_expr("CAST(raw AS TEXT)", record) == "42"
    assert evaluate_transform_expr("CAST(raw AS BOOLEAN)", record) is True


def test_evaluate_cast_of_none_stays_none():
    assert evaluate_transform_expr("CAST(missing AS INTEGER)", {}) is None


# --- Spark SQL compiler -----------------------------------------------


def test_compile_field_reference_uses_the_given_alias():
    node = parse_transform_expr("policy.premium")
    sql = compile_to_spark_sql(node, {"policy.premium": "__src_0"})
    assert sql == "`__src_0`"


def test_compile_function_call():
    node = parse_transform_expr("UPPER(name)")
    sql = compile_to_spark_sql(node, {"name": "__src_0"})
    assert sql == "UPPER(`__src_0`)"


def test_compile_cast_maps_to_spark_sql_type():
    node = parse_transform_expr("CAST(premium AS DECIMAL)")
    sql = compile_to_spark_sql(node, {"premium": "__src_0"})
    assert sql == "CAST(`__src_0` AS DECIMAL(18,4))"


def test_compile_arithmetic_and_negation():
    node = parse_transform_expr("-premium * 2")
    sql = compile_to_spark_sql(node, {"premium": "__src_0"})
    assert sql == "((-`__src_0`) * 2.0)"


def test_compile_raises_when_alias_missing_for_a_referenced_field():
    node = parse_transform_expr("premium")
    with pytest.raises(TransformExpressionError):
        compile_to_spark_sql(node, {})
