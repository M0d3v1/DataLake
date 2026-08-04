import pytest

from apps.core.exceptions import MappingError
from apps.pipelines.mapping import map_record, map_records


def test_maps_flat_fields():
    record = {"policy_id": "P-1", "premium": 100}
    mapping = {"id": "policy_id", "premium_amount": "premium"}
    assert map_record(record, mapping) == {"id": "P-1", "premium_amount": 100}


def test_maps_nested_source_field():
    record = {"policy": {"id": "P-1"}}
    mapping = {"id": "policy.id"}
    assert map_record(record, mapping) == {"id": "P-1"}


def test_permissive_mode_maps_missing_field_to_none():
    record = {"policy_id": "P-1"}
    mapping = {"id": "policy_id", "agent": "agent_name"}
    assert map_record(record, mapping, strict=False) == {"id": "P-1", "agent": None}


def test_strict_mode_raises_on_missing_field():
    record = {"policy_id": "P-1"}
    mapping = {"id": "policy_id", "agent": "agent_name"}
    with pytest.raises(MappingError):
        map_record(record, mapping, strict=True)


def test_preserves_explicit_null_value():
    record = {"policy_id": "P-1", "cancelled_at": None}
    mapping = {"cancelled_at": "cancelled_at"}
    assert map_record(record, mapping, strict=True) == {"cancelled_at": None}


def test_rejects_non_object_record():
    with pytest.raises(MappingError):
        map_record(["not", "an", "object"], {"id": "policy_id"})


def test_does_not_mutate_original_record():
    record = {"policy_id": "P-1", "nested": {"a": 1}}
    original_copy = {"policy_id": "P-1", "nested": {"a": 1}}
    map_record(record, {"id": "policy_id"})
    assert record == original_copy


def test_map_records_applies_mapping_to_each_record():
    records = [{"policy_id": "P-1"}, {"policy_id": "P-2"}]
    mapped = map_records(records, {"id": "policy_id"})
    assert mapped == [{"id": "P-1"}, {"id": "P-2"}]
