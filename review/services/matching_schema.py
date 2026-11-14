"""Structured local matching output; quoted evidence is validated separately."""
def matching_schema():
    text = {'type': 'string', 'maxLength': 300}
    evidence = {'text': text,
                'venue_fields': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 3},
                'venue_quote': text,
                'manuscript_evidence_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 3},
                'manuscript_quote': text}
    def obj(props):
        return {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}
    rating = obj({**evidence, 'rating': {'type': 'integer', 'minimum': 0, 'maximum': 4},
                  'venue_fields': {'type': 'array', 'items': {'type': 'string', 'enum': ['aims_scope']}, 'minItems': 1, 'maxItems': 1}})
    rubric = obj({'application_context_only': {'type': 'boolean'},
                  'research_contribution': rating, 'subject_alignment': rating})
    return obj({'fit_summary': text,
                'subject_fit_rubric': {'anyOf': [rubric, {'type': 'null'}]},
                'reasons': {'type': 'array', 'items': obj(evidence), 'maxItems': 3},
                'gaps': {'type': 'array', 'items': obj(evidence), 'maxItems': 3}})


def operation_schema(operation):
    if operation == 'semantic_matching':
        return matching_schema()
    if operation == 'semantic_readiness':
        from .readiness_schema import readiness_schema
        return readiness_schema()
    return None
