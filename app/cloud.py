import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

from . import analysis, config, questions

logger = logging.getLogger(__name__)

ALLOWED_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
TOKEN_HEADER = "X-Job-Token"
UPLOAD_URL_MINUTES = 15
LEASE_SECONDS = config.CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS + 6 * 60


def _clients():
    from google.cloud import firestore, storage, tasks_v2

    return (
        firestore.Client(project=config.GOOGLE_CLOUD_PROJECT, database=config.FIRESTORE_DATABASE),
        storage.Client(project=config.GOOGLE_CLOUD_PROJECT),
        tasks_v2.CloudTasksClient(),
    )


def _safe_filename(filename):
    name = Path(filename.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:255] or "paper"


def validate_uploads(files):
    if not files:
        raise HTTPException(400, "Select at least one paper.")
    if len(files) > config.MAX_BATCH_FILES:
        raise HTTPException(400, f"Select no more than {config.MAX_BATCH_FILES} papers.")
    max_file_size = config.MAX_UPLOAD_MB * 1024 * 1024
    max_batch_size = config.MAX_BATCH_MB * 1024 * 1024
    total = 0
    validated = []
    for item in files:
        filename = _safe_filename(item.filename)
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_TYPES:
            raise HTTPException(400, f"{filename}: upload a PDF, DOCX, TXT or MD file.")
        if item.size < 1:
            raise HTTPException(400, f"{filename}: the file is empty.")
        if item.size > max_file_size:
            raise HTTPException(413, f"{filename} is larger than {config.MAX_UPLOAD_MB} MB.")
        total += item.size
        validated.append({
            "filename": filename,
            "size": item.size,
            "extension": suffix,
            "content_type": ALLOWED_TYPES[suffix],
        })
    if len(files) > 1 and total > max_batch_size:
        raise HTTPException(413, f"The paper bundle is larger than {config.MAX_BATCH_MB} MB.")
    return validated


def _task_parent(client):
    return client.queue_path(
        config.GOOGLE_CLOUD_PROJECT,
        config.CLOUD_TASKS_LOCATION,
        config.CLOUD_TASKS_QUEUE,
    )


def _signing_access_token():
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(Request())
    return credentials.token


def _enqueue(client, job_id):
    from google.protobuf import duration_pb2
    from google.cloud import tasks_v2

    target = config.WORKER_SERVICE_URL.rstrip("/")
    task = {
        "name": client.task_path(
            config.GOOGLE_CLOUD_PROJECT,
            config.CLOUD_TASKS_LOCATION,
            config.CLOUD_TASKS_QUEUE,
            f"review-{job_id}",
        ),
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{target}/internal/tasks/process",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"job_id": job_id}).encode(),
            "oidc_token": {
                "service_account_email": config.TASK_SERVICE_ACCOUNT,
                "audience": target,
            },
        },
        "dispatch_deadline": duration_pb2.Duration(seconds=config.CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS),
    }
    try:
        client.create_task(parent=_task_parent(client), task=task)
    except Exception as exc:
        from google.api_core.exceptions import AlreadyExists
        if isinstance(exc, AlreadyExists):
            return
        raise


def create_uploads(files, verify_online, idempotency_key):
    from google.cloud import firestore

    if not re.fullmatch(r"[a-f0-9]{32,64}", idempotency_key or ""):
        raise HTTPException(400, "A 32- to 64-character hexadecimal Idempotency-Key is required.")
    firestore_client, storage_client, _ = _clients()
    bucket = storage_client.bucket(config.GOOGLE_CLOUD_STORAGE_BUCKET)
    validated = validate_uploads(files)
    fingerprint = hashlib.sha256(json.dumps({
        "files": [{"filename": item["filename"], "size": item["size"]} for item in validated],
        "verify_online": bool(verify_online),
    }, sort_keys=True).encode()).hexdigest()
    idempotency_ref = firestore_client.collection("idempotency").document(_token_hash(idempotency_key))
    generated_batch_id = uuid.uuid4().hex if len(validated) > 1 else None
    generated_jobs = []
    for item in validated:
        job_id = uuid.uuid4().hex
        generated_jobs.append({
            **item,
            "job_id": job_id,
            "object_path": f"uploads/{job_id}/{item['filename']}",
        })

    transaction = firestore_client.transaction()

    @firestore.transactional
    def create_or_reuse(transaction):
        snapshot = idempotency_ref.get(transaction=transaction)
        if snapshot.exists:
            existing = snapshot.to_dict()
            if existing["fingerprint"] != fingerprint:
                raise HTTPException(409, "This Idempotency-Key was already used for a different request.")
            return existing, True

        batch_id = generated_batch_id
        job_records = [{
            **item,
            "batch_id": batch_id,
            "upload_expires_at": datetime.now(timezone.utc) + timedelta(
                days=7 if item["size"] > config.RESUMABLE_UPLOAD_THRESHOLD_MB * 1024 * 1024 else 0,
                minutes=60 if item["size"] <= config.RESUMABLE_UPLOAD_THRESHOLD_MB * 1024 * 1024 else 0,
            ),
        } for item in generated_jobs]
        if batch_id:
            transaction.set(firestore_client.collection("batches").document(batch_id), {
                "batch_id": batch_id,
                "status": "uploading",
                "job_ids": [item["job_id"] for item in job_records],
                "created_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
                "token_hash": _token_hash(idempotency_key),
                "manifest_storage_path": f"manifests/{batch_id}.json",
            })
        for item in job_records:
            transaction.set(firestore_client.collection("jobs").document(item["job_id"]), {
                "job_id": item["job_id"],
                "job_type": "analysis",
                "status": "uploading",
                "stage": "upload",
                "progress": 0.0,
                "log": ["Waiting for paper upload"],
                "created_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
                "original_filename": item["filename"],
                "content_type": item["content_type"],
                "expected_size": item["size"],
                "upload_expires_at": item["upload_expires_at"],
                "storage_object_path": item["object_path"],
                "result_storage_path": None,
                "error_message": "",
                "batch_id": batch_id,
                "verify_online": bool(verify_online),
                "token_hash": _token_hash(idempotency_key),
            })
        created = {
            "batch_id": batch_id,
            "job_ids": [item["job_id"] for item in job_records],
            "jobs": job_records,
            "fingerprint": fingerprint,
        }
        transaction.set(idempotency_ref, {
            **created,
            "created_at": firestore.SERVER_TIMESTAMP,
        })
        return created, False

    idempotency_record, reused = create_or_reuse(transaction)
    batch_id = idempotency_record["batch_id"]
    job_records = []
    for item in idempotency_record["jobs"]:
        job = firestore_client.collection("jobs").document(item["job_id"]).get()
        if not job.exists:
            raise HTTPException(500, "The saved upload request is missing its job record.")
        job_record = job.to_dict()
        if reused and job_record["status"] == "uploading":
            upload_expiry = timedelta(
                days=7 if item["size"] > config.RESUMABLE_UPLOAD_THRESHOLD_MB * 1024 * 1024 else 0,
                minutes=60 if item["size"] <= config.RESUMABLE_UPLOAD_THRESHOLD_MB * 1024 * 1024 else 0,
            )
            firestore_client.collection("jobs").document(item["job_id"]).update({
                "upload_expires_at": datetime.now(timezone.utc) + upload_expiry,
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
        job_records.append({
            "job_id": item["job_id"],
            "filename": item["filename"],
            "size": item["size"],
            "content_type": item["content_type"],
            "extension": item["extension"],
            "object_path": item["object_path"],
            "status": job_record["status"],
        })

    uploads = []
    signed_url_access_token = None
    try:
        for item in job_records:
            if item["status"] != "uploading":
                uploads.append({
                    "job_id": item["job_id"],
                    "token": idempotency_key,
                    "filename": item["filename"],
                    "size": item["size"],
                    "content_type": item["content_type"],
                    "status": item["status"],
                })
                continue
            blob = bucket.blob(item["object_path"])
            if item["size"] > config.RESUMABLE_UPLOAD_THRESHOLD_MB * 1024 * 1024:
                url = blob.create_resumable_upload_session(
                    content_type=item["content_type"],
                    size=item["size"],
                    if_generation_match=0,
                )
                upload_mode = "resumable"
            else:
                if signed_url_access_token is None:
                    signed_url_access_token = _signing_access_token()
                url = blob.generate_signed_url(
                    version="v4",
                    expiration=timedelta(minutes=UPLOAD_URL_MINUTES),
                    method="PUT",
                    content_type=item["content_type"],
                    query_parameters={"ifGenerationMatch": "0"},
                    service_account_email=config.SIGNED_URL_SERVICE_ACCOUNT,
                    access_token=signed_url_access_token,
                )
                upload_mode = "signed_put"
            uploads.append({
                "job_id": item["job_id"],
                "token": idempotency_key,
                "filename": item["filename"],
                "size": item["size"],
                "content_type": item["content_type"],
                "upload_url": url,
                "upload_mode": upload_mode,
                "status": "uploading",
            })
    except Exception:
        logger.exception("Could not generate upload authorization")
        raise HTTPException(503, "Could not authorize the upload. Check Cloud Storage configuration and retry.")

    _try_update_batch_manifest(firestore_client, storage_client, batch_id)
    return {
        "batch_id": batch_id,
        "batch_token": idempotency_key if batch_id else None,
        "uploads": uploads,
        "reused": reused,
    }


def secrets_token():
    import secrets

    return secrets.token_urlsafe(32)


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _authorize(record, token):
    if not token or not hmac.compare_digest(record.get("token_hash", ""), _token_hash(token)):
        raise HTTPException(404, "Job not found.")


def _get_job(firestore_client, job_id, token):
    snapshot = firestore_client.collection("jobs").document(job_id).get()
    if not snapshot.exists:
        raise HTTPException(404, "Unknown job.")
    job = snapshot.to_dict()
    _authorize(job, token)
    return job


def _expire_upload(firestore_client, job_id, job):
    expires_at = job.get("upload_expires_at")
    if job["status"] != "uploading" or not expires_at or expires_at > datetime.now(timezone.utc):
        return job
    from google.cloud import firestore

    firestore_client.collection("jobs").document(job_id).update({
        "status": "failed",
        "stage": "upload",
        "error_message": "Upload authorization expired. Please start a new review.",
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    return {**job, "status": "failed", "error_message": "Upload authorization expired. Please start a new review."}


def _update_batch_manifest(firestore_client, storage_client, batch_id):
    from google.cloud import firestore

    if not batch_id:
        return
    batch_ref = firestore_client.collection("batches").document(batch_id)
    batch_snapshot = batch_ref.get()
    if not batch_snapshot.exists:
        return
    batch = batch_snapshot.to_dict()
    items = []
    job_updated_at = []
    for job_id in batch["job_ids"]:
        snapshot = firestore_client.collection("jobs").document(job_id).get()
        if not snapshot.exists:
            items.append({
                "job_id": job_id,
                "name": "Unknown paper",
                "status": "failed",
                "progress": 1.0,
                "error": "Job record is unavailable.",
                "result_storage_path": None,
            })
            continue
        job = snapshot.to_dict()
        if job.get("updated_at"):
            job_updated_at.append(job["updated_at"])
        items.append({
            "job_id": job_id,
            "name": job["original_filename"],
            "status": job["status"],
            "progress": job.get("progress", 0.0),
            "error": job.get("error_message", ""),
            "result_storage_path": job.get("result_storage_path"),
        })
    status = (
        "completed"
        if items and all(item["status"] in {"completed", "failed"} for item in items)
        else "uploading"
        if any(item["status"] == "uploading" for item in items)
        else "processing"
    )
    manifest_path = batch["manifest_storage_path"]
    storage_client.bucket(config.GOOGLE_CLOUD_STORAGE_BUCKET).blob(manifest_path).upload_from_string(
        json.dumps({"batch_id": batch_id, "status": status, "items": items}),
        content_type="application/json",
    )
    batch_ref.update({
        "status": status,
        "completed": sum(item["status"] in {"completed", "failed"} for item in items),
        "manifest_updated_at": firestore.SERVER_TIMESTAMP,
        "manifest_job_updated_at": max(job_updated_at) if job_updated_at else None,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })


def _try_update_batch_manifest(firestore_client, storage_client, batch_id):
    if not batch_id:
        return
    try:
        _update_batch_manifest(firestore_client, storage_client, batch_id)
    except Exception as exc:
        logger.error(
            "cloud_storage.batch_manifest_write_failed",
            extra={"batch_id": batch_id, "error_type": type(exc).__name__},
        )


def complete_upload(job_id, token):
    firestore_client, storage_client, tasks_client = _clients()
    job_ref = firestore_client.collection("jobs").document(job_id)
    job = _get_job(firestore_client, job_id, token)
    original_status = job["status"]
    job = _expire_upload(firestore_client, job_id, job)
    if original_status == "uploading" and job["status"] == "failed":
        raise HTTPException(410, job.get("error_message", "Upload authorization expired."))
    if job["status"] in {"queued", "processing", "completed"}:
        if job["status"] == "queued":
            _enqueue(tasks_client, job_id)
        return {"job_id": job_id, "status": job["status"]}
    if job["status"] != "uploading":
        raise HTTPException(409, f"Upload cannot be completed while the job is {job['status']}.")
    blob = storage_client.bucket(config.GOOGLE_CLOUD_STORAGE_BUCKET).get_blob(job["storage_object_path"])
    if blob is None:
        raise HTTPException(400, "The uploaded file was not found. Upload it again before continuing.")
    if int(blob.size or 0) != int(job["expected_size"]):
        raise HTTPException(400, "The uploaded file size did not match the authorized size.")
    if blob.content_type != job["content_type"]:
        raise HTTPException(400, "The uploaded file type did not match the authorized file type.")
    header = blob.download_as_bytes(start=0, end=min(int(blob.size or 1) - 1, 1023))
    extension = Path(job["original_filename"]).suffix.lower()
    if extension == ".pdf" and b"%PDF-" not in header:
        raise HTTPException(400, "The uploaded file does not appear to be a PDF.")
    if extension == ".docx":
        if not header.startswith(b"PK"):
            raise HTTPException(400, "The uploaded file does not appear to be a DOCX document.")
        try:
            with blob.open("rb") as source:
                with zipfile.ZipFile(source) as document:
                    required = {"[Content_Types].xml", "word/document.xml"}
                    if not required.issubset(document.namelist()):
                        raise HTTPException(400, "The uploaded file is not a valid DOCX document.")
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "The uploaded file is not a valid DOCX document.") from exc
    if extension in {".txt", ".md"} and b"\x00" in header:
        raise HTTPException(400, "The uploaded text file contains binary data.")

    from google.cloud import firestore

    job_ref.update({
        "status": "queued",
        "stage": "queued",
        "progress": 0.0,
        "log": ["Upload verified; waiting for review"],
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    try:
        _enqueue(tasks_client, job_id)
    except Exception as exc:
        job_ref.update({
            "status": "failed",
            "error_message": "The review could not be queued. Please start a new review.",
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        logger.error(
            "cloud_tasks.enqueue_failed",
            extra={"job_id": job_id, "error_type": type(exc).__name__},
        )
        raise HTTPException(503, "The review could not be queued. Please try again.") from exc
    _try_update_batch_manifest(firestore_client, storage_client, job.get("batch_id"))
    return {"job_id": job_id, "status": "queued"}


def fail_upload(job_id, token):
    from google.cloud import firestore

    firestore_client, storage_client, _ = _clients()
    job = _get_job(firestore_client, job_id, token)
    if job["status"] != "uploading":
        return {"job_id": job_id, "status": job["status"]}
    firestore_client.collection("jobs").document(job_id).update({
        "status": "failed",
        "stage": "upload",
        "error_message": "Upload did not complete. Please start a new review.",
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    _try_update_batch_manifest(firestore_client, storage_client, job.get("batch_id"))
    return {"job_id": job_id, "status": "failed"}


def job_status(job_id, token):
    firestore_client, _, _ = _clients()
    job = _get_job(firestore_client, job_id, token)
    job = _expire_upload(firestore_client, job_id, job)
    return {
        "status": job["status"],
        "stage": job.get("stage", ""),
        "progress": job.get("progress", 0.0),
        "log": job.get("log", [])[-12:],
        "error": job.get("error_message", ""),
    }


def aggregate_batch_status(items, total):
    completed = sum(item["status"] in {"completed", "failed"} for item in items)
    progress = sum(
        1.0 if item["status"] in {"completed", "failed"} else item.get("progress", 0.0)
        for item in items
    )
    state = (
        "completed"
        if items and completed == len(items)
        else "uploading"
        if any(item["status"] == "uploading" for item in items)
        else "processing"
    )
    active = next((item for item in items if item["status"] in {"uploading", "queued", "processing"}), None)
    return {
        "status": state,
        "stage": active.get("stage", "") if active else "done",
        "progress": round(progress / max(len(items), 1), 3),
        "completed": completed,
        "total": total,
        "log": active.get("log", [])[-12:] if active else [],
        "items": items,
    }


def batch_status(batch_id, token):
    firestore_client, storage_client, _ = _clients()
    snapshot = firestore_client.collection("batches").document(batch_id).get()
    if not snapshot.exists:
        raise HTTPException(404, "Unknown paper bundle.")
    batch = snapshot.to_dict()
    _authorize(batch, token)
    items = []
    expired = False
    stale_manifest = not batch.get("manifest_job_updated_at")
    for job_id in batch["job_ids"]:
        job_snapshot = firestore_client.collection("jobs").document(job_id).get()
        if not job_snapshot.exists:
            stale_manifest = True
            items.append({
                "job_id": job_id,
                "name": "Unknown paper",
                "status": "failed",
                "progress": 1.0,
                "error": "Job record is unavailable.",
                "stage": "done",
                "log": [],
            })
            continue
        job = job_snapshot.to_dict()
        job_updated_at = job.get("updated_at")
        manifest_updated_at = batch.get("manifest_job_updated_at")
        stale_manifest = stale_manifest or bool(
            job_updated_at and manifest_updated_at and job_updated_at > manifest_updated_at
        )
        was_uploading = job["status"] == "uploading"
        job = _expire_upload(firestore_client, job_id, job)
        expired = expired or (was_uploading and job["status"] == "failed")
        items.append({
            "job_id": job_id,
            "name": job["original_filename"],
            "status": job["status"],
            "progress": job.get("progress", 0.0),
            "error": job.get("error_message", ""),
            "stage": job.get("stage", ""),
            "log": job.get("log", []),
        })
    if expired or stale_manifest:
        _try_update_batch_manifest(firestore_client, storage_client, batch_id)
    return aggregate_batch_status(items, len(batch["job_ids"]))


def job_result(job_id, token):
    firestore_client, storage_client, _ = _clients()
    job = _get_job(firestore_client, job_id, token)
    if job["status"] != "completed" or not job.get("result_storage_path"):
        raise HTTPException(404, "Result not ready.")
    blob = storage_client.bucket(config.GOOGLE_CLOUD_STORAGE_BUCKET).blob(job["result_storage_path"])
    try:
        return json.loads(blob.download_as_text())
    except Exception as exc:
        logger.error(
            "cloud_storage.result_read_failed",
            extra={"job_id": job_id, "error_type": type(exc).__name__},
        )
        raise HTTPException(500, "The saved paper review could not be read.") from exc


def ask_about_paper(job_id, token, request):
    result = job_result(job_id, token)
    try:
        return questions.answer(result["text"], request.question.strip(), [item.model_dump() for item in request.history[-8:]])
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


def verify_task_request(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Cloud Tasks identity token.")
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import verify_token

    try:
        claims = verify_token(
            authorization.removeprefix("Bearer "),
            Request(),
            audience=config.WORKER_SERVICE_URL.rstrip("/"),
        )
    except Exception as exc:
        raise HTTPException(401, "Invalid Cloud Tasks identity token.") from exc
    if claims.get("email") != config.TASK_SERVICE_ACCOUNT or claims.get("email_verified") is not True:
        raise HTTPException(403, "Cloud Tasks service account is not authorized.")


def _claim_job(firestore_client, job_id):
    from google.cloud import firestore

    ref = firestore_client.collection("jobs").document(job_id)
    transaction = firestore_client.transaction()

    @firestore.transactional
    def claim(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return None, "missing"
        job = snapshot.to_dict()
        if job["status"] in {"completed", "failed"}:
            return None, "finished"
        if job["status"] not in {"queued", "processing"}:
            return None, "not_ready"
        now = datetime.now(timezone.utc)
        lease_until = job.get("lease_until")
        if job["status"] == "processing" and lease_until and lease_until > now:
            return None, "busy"
        token = uuid.uuid4().hex
        transaction.update(ref, {
            "status": "processing",
            "stage": "read",
            "updated_at": firestore.SERVER_TIMESTAMP,
            "lease_token": token,
            "lease_until": now + timedelta(seconds=LEASE_SECONDS),
        })
        return (job, token), "claimed"

    return claim(transaction)


def _progress(firestore_client, job_id, lease_token, stage, progress, message):
    from google.cloud import firestore

    ref = firestore_client.collection("jobs").document(job_id)
    transaction = firestore_client.transaction()

    @firestore.transactional
    def update(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists or snapshot.to_dict().get("lease_token") != lease_token:
            return
        job = snapshot.to_dict()
        log = (job.get("log") or []) + ([message] if message else [])
        transaction.update(ref, {
            "status": "processing",
            "stage": stage,
            "progress": round(progress, 3),
            "log": log[-20:],
            "updated_at": firestore.SERVER_TIMESTAMP,
        })

    update(transaction)


def process_job(job_id, retry_count=0):
    from google.cloud import firestore

    firestore_client, storage_client, _ = _clients()
    claimed, state = _claim_job(firestore_client, job_id)
    if state == "missing":
        raise HTTPException(404, "Unknown job.")
    if state == "busy":
        raise HTTPException(503, "This job already has an active worker; retry later.")
    if state != "claimed":
        return {"status": state}
    job, lease_token = claimed

    extension = Path(job["original_filename"]).suffix.lower()
    bucket = storage_client.bucket(config.GOOGLE_CLOUD_STORAGE_BUCKET)
    try:
        with tempfile.TemporaryDirectory(prefix="reacher-worker-") as temp_dir:
            temporary_path = os.path.join(temp_dir, f"paper{extension}")
            bucket.blob(job["storage_object_path"]).download_to_filename(temporary_path)
            result = analysis.analyze(
                temporary_path,
                job["original_filename"],
                job["verify_online"],
                lambda stage, progress, message=None: _progress(
                    firestore_client, job_id, lease_token, stage, progress, message
                ),
            )
            result_path = f"results/{job_id}/{lease_token}.json"
            bucket.blob(result_path).upload_from_string(
                json.dumps(result, ensure_ascii=False),
                content_type="application/json",
            )
        ref = firestore_client.collection("jobs").document(job_id)
        transaction = firestore_client.transaction()

        @firestore_transactional
        def finish(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists or snapshot.to_dict().get("lease_token") != lease_token:
                return False
            transaction.update(ref, {
                "status": "completed",
                "stage": "done",
                "progress": 1.0,
                "log": (snapshot.to_dict().get("log") or [])[-19:] + ["Review complete"],
                "result_storage_path": result_path,
                "error_message": "",
                "lease_until": None,
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
            return True

        committed = finish(transaction)
        if committed:
            _try_update_batch_manifest(firestore_client, storage_client, job.get("batch_id"))
            return {"status": "completed"}
        return {"status": "stale"}
    except Exception as exc:
        logger.error(
            "analysis.failed",
            extra={"job_id": job_id, "attempt": retry_count + 1, "error_type": type(exc).__name__},
        )
        ref = firestore_client.collection("jobs").document(job_id)
        terminal = retry_count >= config.CLOUD_TASKS_MAX_ATTEMPTS - 1
        transaction = firestore_client.transaction()

        @firestore_transactional
        def fail(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists or snapshot.to_dict().get("lease_token") != lease_token:
                return False
            transaction.update(ref, {
                "status": "failed" if terminal else "queued",
                "error_message": str(exc)[:500] if terminal else "Review attempt failed; retrying.",
                "lease_until": None,
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
            return True

        fail(transaction)
        _try_update_batch_manifest(firestore_client, storage_client, job.get("batch_id"))
        if terminal:
            return {"status": "failed"}
        raise


def firestore_transactional(function):
    from google.cloud import firestore

    return firestore.transactional(function)
