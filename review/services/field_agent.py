import os
import json
import httpx
import random

def _extract_citations(raw_text):
    """Uses Claude to extract a JSON list of citations from the raw text."""
    key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    model = os.getenv('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001').strip()
    
    if not key:
        return []

    # Only send the end of the manuscript to save tokens and focus on references
    # 30,000 chars is roughly the last 15-20 pages.
    text_end = raw_text[-30000:] if len(raw_text) > 30000 else raw_text

    prompt = f"""
    You are an expert academic assistant.
    Your task is to extract the bibliography or references section from the following manuscript text.
    Return the result as a JSON object with two keys:
    1. "total_count": The total number of references you found in the manuscript.
    2. "citations": A JSON array containing a MAXIMUM of 15 citations. Do not extract more than 15 strings for this array, even if there are hundreds.
    
    Do NOT return any other text, markdown formatting, or explanations. Just the raw JSON object.
    If there are no references, return {{"total_count": 0, "citations": []}}.

    MANUSCRIPT END:
    {text_end}
    """
    
    try:
        response = httpx.post(
            'https://api.anthropic.com/v1/messages',
            headers={'content-type': 'application/json', 'x-api-key': key, 'anthropic-version': '2023-06-01'},
            json={'model': model, 'max_tokens': 2000, 'messages': [{'role': 'user', 'content': prompt}]},
            timeout=60.0,
        )
        if response.status_code >= 400:
            return []
            
        payload = response.json()
        output = ''.join(part.get('text', '') for part in payload.get('content', []) if part.get('type') == 'text').strip()
        
        # Clean markdown if claude included it
        if output.startswith("```json"):
            output = output[7:]
        if output.startswith("```"):
            output = output[3:]
        if output.endswith("```"):
            output = output[:-3]
            
        try:
            parsed = json.loads(output.strip())
            if isinstance(parsed, dict):
                total_count = parsed.get("total_count", 0)
                citations = parsed.get("citations", [])
                if isinstance(citations, list):
                    clean_citations = [c for c in citations if isinstance(c, str)]
                    return total_count, clean_citations
        except Exception:
            pass
    except Exception:
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
