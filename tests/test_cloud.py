import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app import cloud


class FakeSnapshot:
    def __init__(self, value):
        self.value = value
        self.exists = value is not None

    def to_dict(self):
        return copy.deepcopy(self.value)


class FakeDocument:
    def __init__(self, database, key):
        self.database = database
        self.key = key

    def get(self, transaction=None):
        return FakeSnapshot(self.database.values.get(self.key))

    def update(self, values):
        self.database.values[self.key].update(values)

    def set(self, values):
        self.database.values[self.key] = copy.deepcopy(values)


class FakeCollection:
    def __init__(self, database):
        self.database = database

    def document(self, key):
        return FakeDocument(self.database, key)


class FakeWriteBatch:
    def __init__(self):
        self.writes = []

    def set(self, reference, values):
        self.writes.append((reference, values))

    def commit(self):
        for reference, values in self.writes:
            reference.set(values)


class FakeTransaction:
    def set(self, reference, values):
        reference.set(values)

    def update(self, reference, values):
        reference.update(values)


class FakeFirestore:
    def __init__(self, values):
        self.values = values

    def collection(self, name):
        return FakeCollection(self)

    def transaction(self):
        return FakeTransaction()

    def batch(self):
        return FakeWriteBatch()


class FakeBlob:
    def __init__(self, name):
        self.name = name
        self.saved = None
        self.size = None
        self.content_type = None
        self.content = b""

    def download_to_filename(self, path):
        with open(path, "wb") as file:
            file.write(self.content or b"paper text for a mocked analysis")

    def upload_from_string(self, content, content_type=None):
        self.saved = content

    def download_as_text(self):
        return self.saved

    def download_as_bytes(self, start=None, end=None):
        return self.content[start:end + 1 if end is not None else None]

    def generate_signed_url(self, **kwargs):
        return "https://signed-upload.example"

    def create_resumable_upload_session(self, **kwargs):
        return "https://resumable-session.example"


class FakeBucket:
    def __init__(self):
        self.blobs = {}

    def blob(self, name):
        return self.blobs.setdefault(name, FakeBlob(name))

    def get_blob(self, name):
        return self.blobs.get(name)


class FakeStorage:
    def __init__(self):
        self.fake_bucket = FakeBucket()

    def bucket(self, name):
        return self.fake_bucket


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.transactional_patcher = patch(
            "google.cloud.firestore.transactional",
            side_effect=lambda function: function,
        )
        self.transactional_patcher.start()
        self.addCleanup(self.transactional_patcher.stop)

    def test_validate_accepts_safe_supported_upload(self):
        result = cloud.validate_uploads([
            SimpleNamespace(filename=r"C:\temp\paper.PDF", size=1024)
        ])
        self.assertEqual(result[0]["filename"], "paper.PDF")
        self.assertEqual(result[0]["extension"], ".pdf")
        self.assertEqual(result[0]["content_type"], "application/pdf")

    def test_enqueue_uses_configured_dispatch_deadline(self):
        class FakeTasksClient:
            def __init__(self):
                self.task = None

            def task_path(self, *parts):
                return "/".join(parts)

            def queue_path(self, *parts):
                return "/".join(parts)

            def create_task(self, parent, task):
                self.task = task

        client = FakeTasksClient()
        with patch.object(cloud.config, "CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS", 120):
            cloud._enqueue(client, "a" * 32)
        self.assertEqual(client.task["dispatch_deadline"].seconds, 120)

    def test_validate_rejects_bad_extension_and_oversize(self):
        with self.assertRaises(HTTPException) as bad_type:
            cloud.validate_uploads([SimpleNamespace(filename="paper.exe", size=10)])
        self.assertEqual(bad_type.exception.status_code, 400)

        with patch.object(cloud.config, "MAX_UPLOAD_MB", 1):
            with self.assertRaises(HTTPException) as too_large:
                cloud.validate_uploads([SimpleNamespace(filename="paper.txt", size=1024 * 1024 + 1)])
        self.assertEqual(too_large.exception.status_code, 413)

    def test_validate_rejects_batch_over_limit(self):
        with patch.object(cloud.config, "MAX_BATCH_MB", 1):
            with self.assertRaises(HTTPException) as too_large:
                cloud.validate_uploads([
                    SimpleNamespace(filename="one.txt", size=600 * 1024),
                    SimpleNamespace(filename="two.txt", size=500 * 1024),
                ])
        self.assertEqual(too_large.exception.status_code, 413)

    def test_job_tokens_are_required_and_unforgeable(self):
        record = {"token_hash": cloud._token_hash("correct-token")}
        cloud._authorize(record, "correct-token")
        with self.assertRaises(HTTPException) as unauthorized:
            cloud._authorize(record, "wrong-token")
        self.assertEqual(unauthorized.exception.status_code, 404)

    def test_missing_jobs_are_not_found(self):
        with patch.object(cloud, "_clients", return_value=(FakeFirestore({}), object(), object())):
            with self.assertRaises(HTTPException) as missing:
                cloud.job_result("a" * 32, "token")
        self.assertEqual(missing.exception.status_code, 404)

    def test_upload_completion_checks_actual_storage_size(self):
        job_id = "b" * 32
        token = "upload-token"
        database = FakeFirestore({
            job_id: {
                "job_id": job_id,
                "status": "uploading",
                "token_hash": cloud._token_hash(token),
                "storage_object_path": f"uploads/{job_id}/paper.pdf",
                "expected_size": 100,
                "content_type": "application/pdf",
                "original_filename": "paper.pdf",
                "upload_expires_at": None,
            }
        })
        storage = FakeStorage()
        storage.fake_bucket.blobs["uploads/" + job_id + "/paper.pdf"] = SimpleNamespace(
            size=50,
            content_type="application/pdf",
        )
        with (
            patch.object(cloud, "_clients", return_value=(database, storage, object())),
            patch.object(cloud, "_signing_access_token", return_value="token"),
        ):
            with self.assertRaises(HTTPException) as mismatch:
                cloud.complete_upload(job_id, token)
        self.assertEqual(mismatch.exception.status_code, 400)
        self.assertEqual(database.values[job_id]["status"], "uploading")

    def test_create_upload_records_and_returns_short_lived_upload_authorization(self):
        database, storage = FakeFirestore({}), FakeStorage()
        with (
            patch.object(cloud, "_clients", return_value=(database, storage, object())),
            patch.object(cloud, "_signing_access_token", return_value="token"),
        ):
            result = cloud.create_uploads(
                [SimpleNamespace(filename="paper.txt", size=1024)],
                verify_online=False,
                idempotency_key="a" * 32,
            )
        upload = result["uploads"][0]
        job = database.values[upload["job_id"]]
        self.assertEqual(result["batch_id"], None)
        self.assertEqual(upload["upload_mode"], "signed_put")
        self.assertEqual(job["status"], "uploading")
        self.assertEqual(job["token_hash"], cloud._token_hash(upload["token"]))

    def test_mocked_upload_queue_worker_and_result_workflow(self):
        database, storage = FakeFirestore({}), FakeStorage()
        clients = (database, storage, object())
        with (
            patch.object(cloud, "_clients", return_value=clients),
            patch.object(cloud, "_signing_access_token", return_value="token"),
        ):
            authorization = cloud.create_uploads(
                [SimpleNamespace(filename="paper.txt", size=1024)],
                verify_online=False,
                idempotency_key="b" * 32,
            )
        upload = authorization["uploads"][0]
        job_id = upload["job_id"]
        object_path = database.values[job_id]["storage_object_path"]
        uploaded_blob = storage.fake_bucket.blob(object_path)
        uploaded_blob.size = 1024
        uploaded_blob.content_type = "text/plain"
        uploaded_blob.content = b"review input"

        with (
            patch.object(cloud, "_clients", return_value=clients),
            patch.object(cloud, "_enqueue") as enqueue,
        ):
            queued = cloud.complete_upload(job_id, upload["token"])
        enqueue.assert_called_once_with(clients[2], job_id)
        self.assertEqual(queued["status"], "queued")

        with (
            patch.object(cloud, "_clients", return_value=clients),
            patch("google.cloud.firestore.transactional", side_effect=lambda function: function),
            patch.object(cloud.analysis, "analyze", return_value={"text": "paper", "summary": {}}),
        ):
            self.assertEqual(cloud.process_job(job_id)["status"], "completed")
            self.assertEqual(cloud.job_status(job_id, upload["token"])["status"], "completed")
            self.assertEqual(cloud.job_result(job_id, upload["token"])["text"], "paper")

    def test_large_upload_uses_resumable_storage_session(self):
        database, storage = FakeFirestore({}), FakeStorage()
        with (
            patch.object(cloud, "_clients", return_value=(database, storage, object())),
            patch.object(cloud.config, "RESUMABLE_UPLOAD_THRESHOLD_MB", 1),
        ):
            result = cloud.create_uploads(
                [SimpleNamespace(filename="paper.pdf", size=2 * 1024 * 1024)],
                verify_online=False,
                idempotency_key="c" * 32,
            )
        self.assertEqual(result["uploads"][0]["upload_mode"], "resumable")

    def test_upload_authorization_reuses_idempotency_key_and_rejects_mismatched_payload(self):
        database, storage = FakeFirestore({}), FakeStorage()
        clients = (database, storage, object())
        files = [SimpleNamespace(filename="paper.txt", size=1024)]
        key = "d" * 32
        with (
            patch.object(cloud, "_clients", return_value=clients),
            patch.object(cloud, "_signing_access_token", return_value="token"),
        ):
            first = cloud.create_uploads(files, verify_online=False, idempotency_key=key)
            second = cloud.create_uploads(files, verify_online=False, idempotency_key=key)
            self.assertEqual(first["uploads"][0]["job_id"], second["uploads"][0]["job_id"])
            self.assertEqual(second["uploads"][0]["token"], key)
            self.assertTrue(second["reused"])
            self.assertEqual(len([doc for doc in database.values if len(doc) == 32]), 1)

            job_id = first["uploads"][0]["job_id"]
            database.values[job_id]["status"] = "queued"
            queued_retry = cloud.create_uploads(files, verify_online=False, idempotency_key=key)
            self.assertEqual(queued_retry["uploads"][0]["status"], "queued")
            self.assertNotIn("upload_url", queued_retry["uploads"][0])

            with self.assertRaises(HTTPException) as mismatch:
                cloud.create_uploads(
                    [SimpleNamespace(filename="different.txt", size=1024)],
                    verify_online=False,
                    idempotency_key=key,
                )
        self.assertEqual(mismatch.exception.status_code, 409)

    def test_batch_progress_counts_failures_as_terminal(self):
        result = cloud.aggregate_batch_status([
            {"status": "completed", "progress": 1.0, "stage": "done"},
            {"status": "failed", "progress": 0.4, "stage": "read"},
            {"status": "processing", "progress": 0.5, "stage": "review"},
        ], 3)
        self.assertEqual(result["status"], "processing")
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["progress"], 0.833)
        self.assertEqual(result["stage"], "review")

    def test_duplicate_task_does_not_repeat_completed_analysis(self):
        job_id = "a" * 32
        database = FakeFirestore({
            job_id: {
                "job_id": job_id,
                "job_type": "analysis",
                "status": "queued",
                "stage": "queued",
                "progress": 0.0,
                "log": [],
                "original_filename": "paper.txt",
                "storage_object_path": f"uploads/{job_id}/paper.txt",
                "verify_online": False,
                "batch_id": None,
            }
        })
        storage = FakeStorage()
        with (
            patch.object(cloud, "_clients", return_value=(database, storage, object())),
            patch("google.cloud.firestore.transactional", side_effect=lambda function: function),
            patch.object(cloud.analysis, "analyze", return_value={"text": "review result"}) as analyze,
        ):
            first = cloud.process_job(job_id)
            second = cloud.process_job(job_id)

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "finished")
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(database.values[job_id]["status"], "completed")
        self.assertTrue(database.values[job_id]["result_storage_path"].startswith(f"results/{job_id}/"))

    def test_active_worker_lease_prevents_parallel_analysis(self):
        job_id = "c" * 32
        database = FakeFirestore({
            job_id: {
                "status": "processing",
                "lease_until": datetime.now(timezone.utc) + timedelta(minutes=2),
            }
        })
        with (
            patch.object(cloud, "_clients", return_value=(database, FakeStorage(), object())),
            patch("google.cloud.firestore.transactional", side_effect=lambda function: function),
            patch.object(cloud.analysis, "analyze") as analyze,
        ):
            with self.assertRaises(HTTPException) as busy:
                cloud.process_job(job_id)
        self.assertEqual(busy.exception.status_code, 503)
        analyze.assert_not_called()

    def test_worker_failures_retry_and_eventually_become_terminal(self):
        job_id = "d" * 32
        database = FakeFirestore({
            job_id: {
                "status": "queued",
                "log": [],
                "original_filename": "paper.txt",
                "storage_object_path": f"uploads/{job_id}/paper.txt",
                "verify_online": False,
                "batch_id": None,
            }
        })
        with (
            patch.object(cloud, "_clients", return_value=(database, FakeStorage(), object())),
            patch("google.cloud.firestore.transactional", side_effect=lambda function: function),
            patch.object(cloud.analysis, "analyze", side_effect=ValueError("failed analysis")),
        ):
            with self.assertRaises(ValueError):
                cloud.process_job(job_id, retry_count=0)
            self.assertEqual(database.values[job_id]["status"], "queued")
            terminal = cloud.process_job(job_id, retry_count=cloud.config.CLOUD_TASKS_MAX_ATTEMPTS - 1)

        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(database.values[job_id]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
