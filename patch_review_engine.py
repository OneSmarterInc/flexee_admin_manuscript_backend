import re

with open(r'd:\flexee\flexee_admin_manuscript_backend\review\services\review_engine.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace local_llm import
content = content.replace(
    "from .local_llm import ollama_chat_json",
    "from .ai_provider import ai_chat_json\nfrom .local_llm import assert_prompt_fits_context, DEFAULT_OLLAMA_NUM_CTX, DEFAULT_OLLAMA_NUM_PREDICT"
)

# Replace ollama_chat_json in _repair_missing_outputs
content = content.replace(
    "_, output = ollama_chat_json(prompt, max_tokens=300, timeout=90, num_ctx=2048)",
    "_, output = ai_chat_json(prompt, max_tokens=300, timeout=90)"
)

# Add _judge_chunked function before judge_with_local_model
chunked_code = """
def _judge_chunked(text, declared_sim, rubric_items, kind, disclosure, measured, num_ctx, max_tokens):
    available_tokens = num_ctx - max_tokens - 1000
    max_chars = max(1000, available_tokens * 4)
    chunks = []
    current = 0
    while current < len(text):
        end = min(current + max_chars, len(text))
        if end < len(text):
            last_break = text.rfind('\\n\\n', current, end)
            if last_break > current + max_chars // 2:
                end = last_break + 2
        chunks.append(text[current:end])
        current = end
        
    all_judgments = []
    models_used = set()
    for chunk in chunks:
        chunk_prompt = _build_prompt(chunk, declared_sim, rubric_items, kind, measured)
        model, output = ai_chat_json(chunk_prompt, max_tokens=max_tokens)
        models_used.add(model)
        parsed = _parse_model_json(output)
        if isinstance(parsed, dict):
            by_id = {item.get('id'): item for item in parsed.get('items', []) if isinstance(item, dict)}
            allowed = {'pass', 'needs_work', 'fail'}
            items = []
            for rubric in rubric_items:
                got = by_id.get(rubric['id'], {})
                verdict = got.get('verdict') if got.get('verdict') in allowed else 'needs_work'
                if rubric.get('advisory') and verdict == 'fail':
                    verdict = 'needs_work'
                items.append({
                    'id': rubric['id'],
                    'verdict': verdict,
                    'evidence': str(got.get('evidence', '')),
                    'gap': str(got.get('gap', '')),
                    'advisory': bool(rubric.get('advisory', False)),
                })
            all_judgments.append(items)
            
    verdict_rank = {'pass': 0, 'needs_work': 1, 'fail': 2}
    final_items = []
    for rubric in rubric_items:
        worst_verdict = 'pass'
        best_evidence = ''
        best_gap = ''
        for items in all_judgments:
            for item in items:
                if item['id'] == rubric['id']:
                    if verdict_rank[item['verdict']] > verdict_rank[worst_verdict]:
                        worst_verdict = item['verdict']
                        best_evidence = item['evidence']
                        best_gap = item['gap']
                    elif verdict_rank[item['verdict']] == verdict_rank[worst_verdict] and not best_evidence:
                        best_evidence = item['evidence']
                        best_gap = item['gap']
        final_items.append({
            'id': rubric['id'],
            'verdict': worst_verdict,
            'evidence': best_evidence,
            'gap': best_gap,
            'advisory': bool(rubric.get('advisory', False)),
        })
        
    decision = compute_decision(measured, final_items)
    editor_summary = _format_editor_summary(measured, final_items, decision, "Chunked review combined.")
    author_letter = _fallback_author_letter(decision, measured, final_items)
    metadata = {"mode": "chunked", "chunk_count": len(chunks)}
    return ",".join(models_used) or "unknown", final_items, decision, editor_summary, author_letter, metadata
"""
content = content.replace("def judge_with_local_model", chunked_code + "\ndef judge_with_local_model")

# Replace judge_with_local_model implementation
old_judge_func = """def judge_with_local_model(text, declared_sim, rubric_items, kind, disclosure, measured):
    if os.getenv('MOCK_AI_REVIEW', 'false').lower() in {'1', 'true', 'yes', 'on'}:
        return _mock_judgment(rubric_items, disclosure, measured)

    model, output = ollama_chat_json(
        _build_prompt(text, declared_sim, rubric_items, kind, measured),
        max_tokens=int(os.getenv('OLLAMA_NUM_PREDICT', '4000')),
    )
    parsed = _parse_model_json(output)"""

new_judge_func = """def judge_with_local_model(text, declared_sim, rubric_items, kind, disclosure, measured):
    if os.getenv('MOCK_AI_REVIEW', 'false').lower() in {'1', 'true', 'yes', 'on'}:
        res = _mock_judgment(rubric_items, disclosure, measured)
        return res[0], res[1], res[2], res[3], res[4], {"mode": "mock"}

    prompt = _build_prompt(text, declared_sim, rubric_items, kind, measured)
    max_tokens = int(os.getenv('OLLAMA_NUM_PREDICT', '4000'))
    num_ctx = int(os.getenv('OLLAMA_NUM_CTX', str(DEFAULT_OLLAMA_NUM_CTX)))
    
    force_provider = None
    metadata = {"mode": "standard"}
    
    try:
        assert_prompt_fits_context(prompt, num_ctx=num_ctx, num_predict=max_tokens)
    except RuntimeError as exc:
        if "too large" in str(exc).lower():
            if os.getenv('ENABLE_CLOUD_FALLBACK', 'false').lower() in {'1', 'true', 'yes', 'on'}:
                force_provider = 'anthropic'
                metadata = {"mode": "cloud-full", "provider": "anthropic"}
            else:
                return _judge_chunked(text, declared_sim, rubric_items, kind, disclosure, measured, num_ctx, max_tokens)
        else:
            raise

    model, output = ai_chat_json(prompt, max_tokens=max_tokens, force_provider=force_provider)
    parsed = _parse_model_json(output)"""

content = content.replace(old_judge_func, new_judge_func)

# Fix return in judge_with_local_model
content = content.replace(
    "return model, items, decision, editor_summary, author_letter",
    "return model, items, decision, editor_summary, author_letter, metadata"
)

# Update run_review to accept 6 return values and add metadata to record
old_run_review = """    model, judgments, decision, editor_summary, author_letter = judge_with_local_model(
        f"{raw_text}\\n\\nAI-Use Disclosure (submitted with the manuscript):\\n{disclosure}",
        declared_sim,
        rubric_items,
        kind,
        disclosure,
        measured
    )"""

new_run_review = """    model, judgments, decision, editor_summary, author_letter, metadata = judge_with_local_model(
        f"{raw_text}\\n\\nAI-Use Disclosure (submitted with the manuscript):\\n{disclosure}",
        declared_sim,
        rubric_items,
        kind,
        disclosure,
        measured
    )"""
content = content.replace(old_run_review, new_run_review)

old_record = """    record = {
        'version': 'django-sqlite-v1',
        'decision': decision,
        'kind': kind,
        'model': model,
        'measured': measured,
        'structural': measured['checks'],
        'judgment': judgments,
        'declared_sim': declared_sim,
    }"""
new_record = """    record = {
        'version': 'django-sqlite-v1',
        'decision': decision,
        'kind': kind,
        'model': model,
        'measured': measured,
        'structural': measured['checks'],
        'judgment': judgments,
        'declared_sim': declared_sim,
    }
    record.update(metadata)"""
content = content.replace(old_record, new_record)

with open(r'd:\flexee\flexee_admin_manuscript_backend\review\services\review_engine.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Patching complete!")
