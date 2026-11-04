"""Local English prose classifier. No network calls or downloaded Python code.

Model: Gradient AI Text Detector (DeBERTa-v3-large); pinned Q4 ONNX conversion.
Scores are uncalibrated model signals, not authorship probabilities.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

MODEL_DIR = Path(
    os.environ.get(
        "AI_FILE_INSPECTOR_MODEL_DIR",
        str(Path.home() / ".ai-file-inspector/models/gradient"),
    )
)
MODEL_NAME = "Gradient AI Text Detector (DeBERTa-v3-large, Q4)"
MODEL_SHA = "cba9f4d084a15206b0fe91aa74b6af818e503403b1df08893f88316c1a693455"
MODEL_REVISION = "776d0164ba0de631036c70f6a146d68a7cd4ea4e"
TOKENIZER_SHA = "9313554f1d10f9e6addc02ea82c727f7e646d9cfb153d2cb62560b9268dd4ca4"
CONFIG_SHA = "ad6acc7701d399e359ea381088b20f8288bba5de78e8b9512f05f6d11c4f1214"
CLS_TOKEN_ID, SEP_TOKEN_ID = 1, 2
MIN_WORDS = 100
MAX_CHARS = 200_000
MAX_PASSAGES = 12
BODY_TOKENS = 480
_lock = threading.Lock()
_engine = None


def installed():
    return all(
        (MODEL_DIR / name).is_file()
        for name in ("model.onnx", "tokenizer.json", "config.json", "manifest.json")
    )


def clean_prose(text):
    """Work on a copy. Remove formatting/code; retain natural-language wording."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"!\[.*?\]\(.*?\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+\.\s+)", "", text)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)
    text = html.unescape(text)
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return re.sub(r"\s+", " ", text).strip()


def get_engine():
    global _engine
    if _engine is not None:
        return _engine
    import onnxruntime as ort
    from tokenizers import Tokenizer

    if not installed():
        raise FileNotFoundError(
            "The local model files are missing. Restore the model folder from the installed copy."
        )
    json.loads((MODEL_DIR / "manifest.json").read_text(encoding="utf-8"))
    for name in ("model.onnx", "tokenizer.json", "config.json"):
        actual = hashlib.sha256((MODEL_DIR / name).read_bytes()).hexdigest()
        expected = {
            "model.onnx": MODEL_SHA,
            "tokenizer.json": TOKENIZER_SHA,
            "config.json": CONFIG_SHA,
        }[name]
        if actual != expected:
            raise ValueError(
                f"The local detector file {name} failed its integrity check. Restore the installed model."
            )
    config = json.loads((MODEL_DIR / "config.json").read_text())
    if (
        config.get("model_type") != "deberta-v2"
        or config.get("vocab_size") != 128100
        or len(config.get("id2label", {})) != 1
    ):
        raise ValueError(
            "Unexpected model labels. AI scoring was stopped to avoid reversing the result."
        )
    # One AI logit: use sigmoid, never a one-item softmax (always 100%).
    settings = ort.SessionOptions()
    settings.intra_op_num_threads = min(4, os.cpu_count() or 1)
    settings.inter_op_num_threads = 1
    settings.log_severity_level = 3
    session = ort.InferenceSession(
        str(MODEL_DIR / "model.onnx"),
        sess_options=settings,
        providers=["CPUExecutionProvider"],
    )
    tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    if (
        tokenizer.token_to_id("[CLS]") != CLS_TOKEN_ID
        or tokenizer.token_to_id("[SEP]") != SEP_TOKEN_ID
    ):
        raise ValueError("Tokenizer does not match the installed detector.")
    _engine = session, tokenizer
    return _engine


def language(text):
    from langdetect import DetectorFactory, detect_langs

    DetectorFactory.seed = 0
    candidates = detect_langs(text[:10000])
    best = candidates[0]
    return best.lang, float(best.prob)


def choose_ranges(token_count):
    """Balanced, nonoverlapping spans; sample evenly when documents are long."""
    count = max(1, math.ceil(token_count / BODY_TOKENS))
    size = math.ceil(token_count / count)
    ranges = [
        (start, min(start + size, token_count)) for start in range(0, token_count, size)
    ]
    indices = (
        list(range(len(ranges)))
        if len(ranges) <= MAX_PASSAGES
        else sorted(
            {
                round(i * (len(ranges) - 1) / (MAX_PASSAGES - 1))
                for i in range(MAX_PASSAGES)
            }
        )
    )
    return [(i, *ranges[i]) for i in indices], len(ranges)


def summarize(scores, weights):
    average = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
    disagrees = min(scores) <= 0.2 and max(scores) >= 0.8
    if disagrees:
        label = "Uncertain \u2014 passages give different signals"
    elif average >= 0.8:
        label = "Leans AI-generated"
    elif average <= 0.2:
        label = "Leans human-written"
    else:
        label = "Uncertain"
    return average, label


def score_logit(logits):
    """Validate a single AI logit and apply a stable sigmoid."""
    import numpy as np

    logits = np.asarray(logits, dtype=float)
    if logits.shape != (1, 1) or not np.isfinite(logits).all():
        raise ValueError("The detector returned an invalid score.")
    value = float(logits[0, 0])
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def analyze(text, enabled=True):
    base = {
        "model": MODEL_NAME,
        "model_revision": MODEL_REVISION,
        "model_sha256": MODEL_SHA,
        "minimum_words": MIN_WORDS,
        "meaning": "This is a trained-model estimate, not proof. The score is not a percentage of AI-written words or a calibrated probability.",
        "scope": "English prose only; short text, code, other languages and heavily edited writing are not reliably supported.",
    }

    def skip(reason, status="not_checked"):
        return {**base, "status": status, "reason": reason}

    if not enabled:
        return skip(
            "This looks like a data or code file. The AI writing check is for English prose."
        )
    lines = [s for s in text[:MAX_CHARS].splitlines() if s.strip()]
    code_lines = sum(
        bool(
            re.match(
                r"\s*(?:import\s|from\s+\w+.*import|def\s|class\s|return\b|if\s.*:|for\s.*:|[{}\[\]]\s*$|\w+\s*=)",
                s,
            )
        )
        for s in lines
    )
    if lines and code_lines / len(lines) > 0.2:
        return skip(
            "Much of this file looks like code. This detector is intended for ordinary English paragraphs."
        )
    cleaned = clean_prose(text[:MAX_CHARS])
    words = re.findall(r"\b[^\W\d_]+(?:['\u2019][^\W\d_]+)?\b", cleaned)
    base["words_in_checked_text"] = len(words)
    if len(words) < MIN_WORDS:
        return skip(
            f"Only {len(words)} words were available. Use at least {MIN_WORDS} words of English prose for an estimate."
        )
    try:
        with _lock:
            lang, certainty = language(cleaned)
            base["detected_language"] = lang
            if lang != "en" or certainty < 0.9:
                return skip(
                    "The text was not confidently recognised as English. This model supports English prose only."
                )
            started = time.monotonic()
            session, tokenizer = get_engine()
            import numpy as np

            encoded = tokenizer.encode(cleaned, add_special_tokens=False)
            ranges, total_passages = choose_ranges(len(encoded.ids))
            inputs_expected = {x.name for x in session.get_inputs()}
            if inputs_expected != {"input_ids", "attention_mask"}:
                raise ValueError("Unexpected model inputs.")
            passages, scores, weights = [], [], []
            for index, start, end in ranges:
                ids = np.array(
                    [[CLS_TOKEN_ID] + encoded.ids[start:end] + [SEP_TOKEN_ID]],
                    dtype=np.int64,
                )
                logits = np.asarray(
                    session.run(
                        None, {"input_ids": ids, "attention_mask": np.ones_like(ids)}
                    )[0],
                    dtype=float,
                )
                score = score_logit(logits)
                first, last = encoded.offsets[start][0], encoded.offsets[end - 1][1]
                passages.append(
                    {
                        "passage_number": index + 1,
                        "ai_pattern_score": round(score * 100, 1),
                        "signal_band": "ai_like"
                        if score >= 0.8
                        else "human_like"
                        if score <= 0.2
                        else "uncertain",
                        "text_excerpt": cleaned[first : min(last, first + 240)],
                        "start_character": first + 1,
                        "end_character": last,
                        "tokens": end - start,
                    }
                )
                scores.append(score)
                weights.append(end - start)
            average, label = summarize(scores, weights)
            notes = [
                "Scores of 20 or lower lean human; 80 or higher lean AI. Middle scores or strongly disagreeing passages are uncertain. These are application rules, not validated accuracy guarantees."
            ]
            if len(ranges) < total_passages:
                notes.append(
                    f"Checked {len(ranges)} passages spread through {total_passages} passages. The rest were not scored."
                )
            if len(text) > MAX_CHARS:
                notes.append(
                    f"Only the first {MAX_CHARS:,} characters were considered. The remaining text was not checked."
                )
            return {
                **base,
                "status": "estimated",
                "label": label,
                "ai_pattern_score": round(average * 100, 1),
                "passages_checked": len(ranges),
                "total_passages": total_passages,
                "passages": passages,
                "notes": notes,
                "coverage": "sampled"
                if len(ranges) < total_passages or len(text) > MAX_CHARS
                else "all eligible text",
                "seconds": round(time.monotonic() - started, 2),
            }
    except Exception as exc:
        return {
            **skip(
                "The AI writing check could not run. The other file checks can still finish.",
                "error",
            ),
            "error": str(exc),
        }
