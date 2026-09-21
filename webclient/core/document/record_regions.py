"""Back-compat shim: record-region detection now lives in :mod:`webclient.dom.records`."""

from ...dom.records import (  # noqa: F401
    RecordRegion,
    find_record_regions,
    mark_for,
    region_marks,
    scan_regions as _scan,
    xpath_of as _path,
)

__all__ = ["RecordRegion", "find_record_regions", "region_marks", "mark_for"]
