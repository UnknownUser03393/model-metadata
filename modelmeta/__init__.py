"""modelmeta -- a queryable SQLite mirror of the curated model metadata snapshot."""

from .schema import GENERATOR, SCHEMA_VERSION, capability_rows

__all__ = ["GENERATOR", "SCHEMA_VERSION", "capability_rows"]
