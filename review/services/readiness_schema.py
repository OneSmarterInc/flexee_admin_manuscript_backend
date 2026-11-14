"""Compact structured output contract for manuscript readiness."""

def readiness_schema():
    short = {'type': 'string', 'maxLength': 160}
    props = {'summary': {'type': 'string', 'maxLength': 240}}
    for key in ('topics', 'methods', 'contributions', 'limitations'):
        props[key] = {'type': 'array', 'maxItems': 3, 'items': short}
    for key, fields in (
        ('evidence_points', {'kind': {'type': 'string', 'enum': ['topic', 'method', 'contribution', 'limitation']}, 'text': short}),
        ('findings', {'code': short, 'label': short, 'status': {'type': 'string', 'enum': ['pass', 'warning']}, 'detail': short}),
    ):
        fields = {**fields, 'line_start': {'type': 'integer'}, 'line_end': {'type': 'integer'}}
        props[key] = {'type': 'array', 'maxItems': 2, 'items': {
            'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}}
    return {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}
