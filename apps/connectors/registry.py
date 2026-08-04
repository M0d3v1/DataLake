from apps.connectors.base import DestinationConnector, SourceConnector

_SOURCE_REGISTRY: dict[str, type[SourceConnector]] = {}
_DESTINATION_REGISTRY: dict[str, type[DestinationConnector]] = {}


def register_source(cls: type[SourceConnector]) -> type[SourceConnector]:
    _SOURCE_REGISTRY[cls.type_key] = cls
    return cls


def register_destination(cls: type[DestinationConnector]) -> type[DestinationConnector]:
    _DESTINATION_REGISTRY[cls.type_key] = cls
    return cls


def get_source_connector(type_key: str, config: dict) -> SourceConnector:
    return _SOURCE_REGISTRY[type_key](config)


def get_destination_connector(type_key: str, config: dict) -> DestinationConnector:
    return _DESTINATION_REGISTRY[type_key](config)


def available_source_types() -> list[str]:
    return sorted(_SOURCE_REGISTRY)


def available_destination_types() -> list[str]:
    return sorted(_DESTINATION_REGISTRY)
