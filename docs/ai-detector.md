# Background manuscript AI-writing detection

Detector source: https://github.com/OneSmarterInc/Ai_Detector at commit
76dd83011b7b08c8fe11d7ef86a6ebb8a0fefd36. Provenance: review/services/ai_detector_runtime/SOURCE.json.
Vendored detector.py is unchanged. The portal wrapper calls analyze() without opening the desktop app.

## Setup

Create a separate Python 3.12 environment to avoid changing portal dependencies:

```powershell
python -m venv .venv-ai
.\.venv-ai\Scripts\python.exe -m pip install -r requirements-ai-detector.txt
.\.venv-ai\Scripts\python.exe review/services/ai_detector_runtime/download_model.py
.\.venv\Scripts\python.exe manage.py migrate
```

Set AI_DETECTOR_ENABLED=true in the backend environment and restart the backend.
Default model directory: backend/var/ai_detector; default Python: backend/.venv-ai.
AI_DETECTOR_PYTHON and AI_DETECTOR_MODEL_DIR can override these absolute paths.
Weights and the private environment are gitignored and must be provisioned per deployment.
The downloader pins the model revision and verifies file hashes. No upload-time network calls.

Start the independent worker from the backend directory:

```powershell
$env:Q_CLUSTER_NAME = 'flexee_detector'
.\.venv\Scripts\python.exe manage.py qcluster
```

On Linux/AWS, use a supervised service with:
`Q_CLUSTER_NAME=flexee_detector .venv/bin/python manage.py qcluster`.
Use Linux .venv-ai/bin/python, not a Windows environment copy. The API and worker need the same
DB, storage, Django secret and environment. Keep the main readiness/matching worker running normally.
One detector job runs at a time; extraction+inference have a 600-second subprocess timeout.
CPU/RAM are still shared with other processes. This setup does not deploy to AWS.

## Behavior

Initial uploads, replaced files and new manuscript versions enqueue after commit. Same-file duplicate
requests do not enqueue twice. Each version has its own ManuscriptAIDetection row with a content hash
and job token. Changed files reset their result; stale workers cannot overwrite newer results.
There is no retroactive bulk scan. To queue an existing manuscript or retry a failed check:
`python manage.py retry_ai_detection MANUSCRIPT_UUID`.
Completed results stay cached. Failed checks are not automatically retried forever.

Author manuscript API exposes ai_score, ai_status, ai_analysis_label, ai_processed_at, ai_model,
ai_coverage and ai_reason through existing manuscript authorization. Venue Matches polls that metadata
while pending/processing. It does not rerun venue matching. Eligibility, readiness and match scores
never consume the AI-writing estimate. Model exceptions produce FAILED, not a zero or human verdict.
NOT_CHECKED covers unsupported files, too little text, and unsupported language. Missing old results
also show Not checked. The table is additive and prior manuscript rows are not rewritten.
Derived results are deleted when the associated manuscript/version is purged.

## Interpretation and limits

The 0-100 number is an uncalibrated AI-pattern signal, not an authorship probability or percentage of
AI-written words. Do not derive a human probability or label uncertain results as proven paraphrasing.
English prose, minimum 100 words. The upstream model considers the first 200,000 characters and scores
at most 12 passages of 480 tokens. Results retain model identity, coverage and extraction limitations;
no text excerpts are persisted. DOCX/PDF/TXT/Markdown extraction is supported. Existing portal upload
allowlists are unchanged (TXT utility support does not add TXT to the portal allowlist). ZIP bundles
remain usable in the portal but receive Not checked from this detector. No OCR is included.

Latest upstream review: uses the core-only detector from backend/ai_file_inspector/core/detector.py. Flexee owns authentication, version-linked storage and its durable queue; the optional standalone React/Django host is not mounted. The summary card retains the model signal out of 100, not the separate AI-like section-share percentage.

## Automatic startup on this Windows laptop

Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_detector_startup.ps1` once as the local user. The current-user Task Scheduler entry `Flexee AI Detector Worker` starts at sign-in, uses no saved password or administrator privileges, stays enabled on battery, and restarts its supervisor on failure. The supervisor restarts an exited worker after 15 seconds and uses a mutex and process identity to prevent duplicate launches. It can adopt the recorded live worker after a supervisor restart. Logs and the process record are in the ignored logs directory.

This is local Windows setup, not AWS service provisioning. Processing pauses while the laptop is off or asleep and resumes after it is awake and the user is signed in. PostgreSQL and the detector dependencies must be available. To stop automatic processing intentionally, disable the scheduled task and stop its recorded worker; closing the normal readiness worker does not stop this separate queue.
