"""Input contracts shared by the independent yard desks."""

import json
from datetime import datetime, timezone


class RuleError(ValueError):
    """An input or business operation is refused without committing changes."""


def require(condition, code, detail):
    if not condition:
        raise RuleError(f"{code}: {detail}")


def text(obj, key):
    value = obj.get(key)
    require(isinstance(value, str) and value and value.strip() == value, "field", key)
    return value


def integer(obj, key, minimum=0, maximum=10**9):
    value = obj.get(key)
    require(type(value) is int and minimum <= value <= maximum, "field", key)
    return value


def records(obj, key):
    value = obj.get(key)
    require(isinstance(value, list) and value and all(isinstance(item, dict) for item in value), "field", key)
    return value


def identifiers(obj, key, *, allow_empty=False):
    value = obj.get(key)
    require(isinstance(value, list) and (value or allow_empty), "field", key)
    for item in value:
        text({key: item}, key)
    require(len(value) == len(set(value)), "duplicate_reference", key)
    return value


def expected(obj, key):
    require(key in obj, "field", key)
    return None if obj[key] is None else text(obj, key)


def timestamp(obj):
    value = text(obj, "at")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise RuleError("time: expected YYYY-MM-DDTHH:MM:SSZ") from exc
    require(parsed.isoformat().replace("+00:00", "Z") == value, "time", "expected canonical UTC seconds")
    return value


def one(db, sql, args=()):
    return db.execute(sql, args).fetchone()


def known(db, sql, args, code):
    row = one(db, sql, args)
    require(row is not None, code, ", ".join(str(arg) for arg in args))
    return row


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def strict_object(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, "duplicate_field", key)
        obj[key] = value
    return obj


def invalid_constant(value):
    raise RuleError(f"json_constant: {value}")
