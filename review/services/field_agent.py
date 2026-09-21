import os
import json
import httpx
import random

from .local_llm import ollama_chat_json

def _extract_citations(raw_text):
    """Extract reference metadata using the same local Qwen3 model as the review engine."""
    text_end = raw_text[-30000:] if len(raw_text) > 30000 else raw_text
    prompt = f"""
You are an expert academic assistant.
Extract the bibliography or references section from the manuscript text below.

Return JSON only with:
1. "total_count": integer total number of references found.
2. "citations": an array containing at most 15 citation strings.

Do not invent citations. If references are absent, return exactly:
{{"total_count": 0, "citations": []}}

MANUSCRIPT END:
{text_end}
"""
    try:
        _, output = ollama_chat_json(prompt, max_tokens=2000, timeout=120)
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            total_count = parsed.get("total_count", 0)
            citations = parsed.get("citations", [])
            if isinstance(total_count, int) and isinstance(citations, list):
                clean_citations = [c.strip() for c in citations if isinstance(c, str) and c.strip()]
                return max(total_count, 0), clean_citations[:15]
    except (RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return 0, []

def _verify_citation_crossref(citation_text):
    """Queries Crossref to check if the citation is real."""
    url = "https://api.crossref.org/works"
    params = {
        "query.bibliographic": citation_text,
        "rows": 1,
        "select": "title,score"
    }
    
    try:
        # We need a user-agent as best practice for Crossref
        headers = {"User-Agent": "Flexee Review Engine (editor@flexee.org)"}
        resp = httpx.get(url, params=params, headers=headers, timeout=10.0)
        
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("message", {}).get("items", [])
            if items:
                # If a result is returned, we consider it a verified existence-check.
                return True, items[0].get("title", [""])[0]
    except Exception:
        pass
        
    return False, None

def run_field_agent(raw_text):
    """
    Runs the field agent logic: extracts references, randomly samples up to 7,
    verifies them via Crossref, and returns a briefing string.
    """
    total_citations, citations = _extract_citations(raw_text)
    
    if not citations:
        return "\n\nFIELD BRIEFING:\nNo citations or references were found in this manuscript."
        
    # Randomly pick up to 7 citations (as requested by user)
    sample_size = min(7, len(citations))
    sampled = random.sample(citations, sample_size)
    
    verified_count = 0
    failed_citations = []
    
    for citation in sampled:
        is_valid, matched_title = _verify_citation_crossref(citation)
        if is_valid:
            verified_count += 1
        else:
            failed_citations.append(citation)
            
    briefing = f"\n\nFIELD BRIEFING:\n"
    briefing += f"The manuscript contains {total_citations} references. A random sample of {sample_size} citations was existence-checked against the Crossref scholarly index.\n"
    briefing += f"Verification Rate: {verified_count}/{sample_size} passed.\n\n"
    
    briefing += "Sampled Citations Checked:\n"
    for idx, citation in enumerate(sampled, 1):
        short_citation = citation if len(citation) < 200 else citation[:197] + "..."
        briefing += f"{idx}. {short_citation}\n"
    
    if failed_citations:
        briefing += "\nWARNING: The following sampled citations could not be verified in the scholarly index (potential hallucination or inaccurate citation):\n"
        for idx, fail in enumerate(failed_citations, 1):
            short_fail = fail if len(fail) < 150 else fail[:147] + "..."
            briefing += f"- {short_fail}\n"
        
    return briefing
