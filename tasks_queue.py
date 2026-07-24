"""
Cloud Tasks helper — used to run slow work (Sheets reads/writes, chart
rendering) *after* acknowledging a Discord interaction within its 3-second
deadline. Each task POSTs back to this same Cloud Run service at /process.
"""

import json
import logging
import os

log = logging.getLogger(__name__)


def _project_id() -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if project:
        return project
    import google.auth

    _, project = google.auth.default()
    if not project:
        raise RuntimeError(
            "Could not determine GCP project. Set the GOOGLE_CLOUD_PROJECT env var."
        )
    return project


def enqueue(payload: dict, self_url: str) -> None:
    """Enqueue a POST to {self_url}/process carrying `payload` as JSON."""
    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(
        _project_id(),
        os.environ.get("TASKS_LOCATION", "us-west1"),
        os.environ.get("TASKS_QUEUE", "discord-followups"),
    )
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{self_url.rstrip('/')}/process",
            "headers": {
                "Content-Type": "application/json",
                "X-Task-Secret": os.environ["TASK_SECRET"],
            },
            "body": json.dumps(payload).encode(),
        }
    }
    client.create_task(request={"parent": parent, "task": task})
    log.info("Enqueued task kind=%s", payload.get("kind"))
