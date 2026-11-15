import os

from dotenv import load_dotenv

load_dotenv()

MODEL_ID = os.getenv("GEMMA_MODEL", "gemma-4-26b-a4b-it")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
MAX_NEW_TOKENS = int(os.getenv("MAX_NEW_TOKENS", "4096"))
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "8000"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))
MAX_BATCH_FILES = int(os.getenv("MAX_BATCH_FILES", "10"))
MAX_BATCH_MB = int(os.getenv("MAX_BATCH_MB", "512"))
RESUMABLE_UPLOAD_THRESHOLD_MB = int(os.getenv("RESUMABLE_UPLOAD_THRESHOLD_MB", "32"))
GEMINI_CONCURRENCY = int(os.getenv("GEMINI_CONCURRENCY", "3"))
if MAX_UPLOAD_MB < 1:
    raise ValueError("MAX_UPLOAD_MB must be at least 1.")
if MAX_BATCH_FILES < 2:
    raise ValueError("MAX_BATCH_FILES must be at least 2.")
if MAX_BATCH_MB < 1:
    raise ValueError("MAX_BATCH_MB must be at least 1.")
if RESUMABLE_UPLOAD_THRESHOLD_MB < 1:
    raise ValueError("RESUMABLE_UPLOAD_THRESHOLD_MB must be at least 1.")
if GEMINI_CONCURRENCY < 1:
    raise ValueError("GEMINI_CONCURRENCY must be at least 1.")
CROSSREF_MAILTO = os.getenv("CROSSREF_MAILTO", "")
PORT = int(os.getenv("PORT", "8080"))
CLOUD_BACKEND = os.getenv("CLOUD_BACKEND", "local").strip().lower()
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
GOOGLE_CLOUD_STORAGE_BUCKET = os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET", "")
GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "asia-south1")
FIRESTORE_DATABASE = os.getenv("FIRESTORE_DATABASE", "(default)")
CLOUD_TASKS_QUEUE = os.getenv("CLOUD_TASKS_QUEUE", "reacher-reviews")
CLOUD_TASKS_LOCATION = os.getenv("CLOUD_TASKS_LOCATION", GOOGLE_CLOUD_LOCATION)
WORKER_SERVICE_URL = os.getenv("WORKER_SERVICE_URL", "")
TASK_SERVICE_ACCOUNT = os.getenv("TASK_SERVICE_ACCOUNT", "")
SIGNED_URL_SERVICE_ACCOUNT = os.getenv("SIGNED_URL_SERVICE_ACCOUNT", "")
CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS = int(os.getenv("CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS", "1800"))
CLOUD_TASKS_MAX_ATTEMPTS = int(os.getenv("CLOUD_TASKS_MAX_ATTEMPTS", "20"))
CORS_ALLOWED_ORIGINS = [origin.strip() for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if origin.strip()]

if CLOUD_BACKEND not in {"local", "google"}:
    raise ValueError("CLOUD_BACKEND must be either 'local' or 'google'.")
if CLOUD_BACKEND == "google":
    missing = [
        name for name, value in (
            ("GEMINI_API_KEY", GEMINI_API_KEY),
            ("GOOGLE_CLOUD_PROJECT", GOOGLE_CLOUD_PROJECT),
            ("GOOGLE_CLOUD_STORAGE_BUCKET", GOOGLE_CLOUD_STORAGE_BUCKET),
            ("WORKER_SERVICE_URL", WORKER_SERVICE_URL),
            ("TASK_SERVICE_ACCOUNT", TASK_SERVICE_ACCOUNT),
            ("SIGNED_URL_SERVICE_ACCOUNT", SIGNED_URL_SERVICE_ACCOUNT),
        ) if not value
    ]
    if missing:
        raise ValueError(f"Cloud mode requires these environment variables: {', '.join(missing)}.")
    if not WORKER_SERVICE_URL.startswith("https://"):
        raise ValueError("WORKER_SERVICE_URL must be an HTTPS Cloud Run service URL.")
if not 15 <= CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS <= 1800:
    raise ValueError("CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS must be between 15 and 1800.")
if CLOUD_TASKS_MAX_ATTEMPTS < 1:
    raise ValueError("CLOUD_TASKS_MAX_ATTEMPTS must be at least 1.")
