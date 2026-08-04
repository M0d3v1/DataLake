from apps.core.paths import get_by_path


def test_get_by_path_top_level():
    assert get_by_path({"a": 1}, "a") == (1, True)


def test_get_by_path_nested():
    assert get_by_path({"a": {"b": {"c": 42}}}, "a.b.c") == (42, True)


def test_get_by_path_missing_returns_not_found():
    assert get_by_path({"a": 1}, "b") == (None, False)


def test_get_by_path_missing_nested_returns_not_found():
    assert get_by_path({"a": {"b": 1}}, "a.x.y") == (None, False)


def test_get_by_path_explicit_null_is_found():
    assert get_by_path({"a": None}, "a") == (None, True)


def test_get_by_path_non_dict_intermediate_returns_not_found():
    assert get_by_path({"a": [1, 2, 3]}, "a.b") == (None, False)
