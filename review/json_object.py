"""Parse a request body that must be a JSON object."""
import json


def json_object(raw):
    """json.loads, but anything other than an object is invalid JSON too (bug L2). The error is a
    json.JSONDecodeError, so every existing `except json.JSONDecodeError` / `except ValueError` catches it."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise json.JSONDecodeError('Expected a JSON object', str(raw)[:50], 0)
    return data
