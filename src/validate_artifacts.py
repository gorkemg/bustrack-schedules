"""Validate both downloaded schedule artifacts before release publication."""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from .security_utils import valid_document_url
except ImportError:
    from security_utils import valid_document_url

MAX_FILE_BYTES = 10 * 1024 * 1024


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def nonempty(value):
    return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def validate(data, agency):
    if not isinstance(data, dict) or data.get("agency_id") != agency or data.get("schema_version") != "1.0":
        raise ValueError("incorrect agency or schema version")
    try:
        generated = datetime.fromisoformat(data["generated_at"])
        if generated.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be UTC")
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid UTC generated_at") from None
    routes = data.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise ValueError("routes must be a nonempty object")
    for key, route in routes.items():
        if not nonempty(key) or not isinstance(route, dict) or not nonempty(route.get("route_short_name")):
            raise ValueError("invalid route key or short name")
        if agency == "PVTA" and key != route["route_short_name"]:
            raise ValueError("PVTA key must match route_short_name")
        if not isinstance(route.get("route_long_name"), str):
            raise ValueError("invalid route_long_name")
        if "route_color" not in route or (route["route_color"] is not None and (not isinstance(route["route_color"], str) or not re.fullmatch(r"[0-9a-fA-F]{6}", route["route_color"]))):
            raise ValueError("invalid route_color")
        for field in ("schedule_pdf_url", "map_pdf_url"):
            if field not in route or (route[field] is not None and not valid_document_url(route[field])):
                raise ValueError(f"invalid {field}")
    if not any(route["schedule_pdf_url"] for route in routes.values()):
        raise ValueError("no schedule links found for agency")
    groupings = data.get("groupings")
    dimension = "region" if agency == "PVTA" else "vehicle_type"
    if not isinstance(groupings, dict) or not isinstance(groupings.get(dimension), list) or not groupings[dimension]:
        raise ValueError("missing agency grouping")
    covered = set()
    for name, groups in groupings.items():
        if not nonempty(name) or not isinstance(groups, list):
            raise ValueError("invalid grouping dimension")
        seen = set()
        for group in groups:
            if not isinstance(group, dict) or not nonempty(group.get("group_id")) or not nonempty(group.get("name")):
                raise ValueError("invalid group")
            if group["group_id"] in seen:
                raise ValueError("duplicate group ID")
            seen.add(group["group_id"])
            ids = group.get("route_ids")
            if not isinstance(ids, list) or any(not isinstance(route_id, str) or route_id not in routes for route_id in ids):
                raise ValueError("group references unknown routes")
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate route within group")
            if name == dimension:
                covered.update(ids)
    if covered != set(routes):
        raise ValueError("agency grouping does not cover all routes")


def load(path):
    with path.open("rb") as fh:
        raw = fh.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("artifact exceeds size limit")
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {value}")))


def main():
    directory = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("output")
    try:
        for agency in ("UTA", "PVTA"):
            validate(load(directory / f"{agency.lower()}_schedules.json"), agency)
            print(f"Validated {agency} schedule artifact")
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"Invalid schedule artifact: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
