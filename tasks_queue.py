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


_client: object | None = None


def _get_client():
    """The process-wide Cloud Tasks client, built once.

    Constructing one costs ~2.5s on a cold process — importing grpc, resolving
    ADC and opening a TLS channel — which is most of Discord's 3-second
    interaction deadline. It has to happen once per process, not once per call.
    """
    global _client
    if _client is None:
        from google.cloud import tasks_v2

        _client = tasks_v2.CloudTasksClient()
    return _client


def warmup() -> None:
    """Pay the client-construction cost off the interaction deadline."""
    _get_client()


def enqueue(payload: dict, self_url: str) -> None:
    """Enqueue a POST to {self_url}/process carrying `payload` as JSON."""
    global _client
    from google.cloud import tasks_v2

    client = _get_client()
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
    try:
        client.create_task(request={"parent": parent, "task": task})
    except Exception as e:
        # Cloud Run throttles CPU to ~0 between requests, which can leave a
        # long-idle gRPC channel stale. Rebuild once and retry before giving up.
        log.warning("create_task failed (%s) — rebuilding client and retrying", e)
        _client = None
        _get_client().create_task(request={"parent": parent, "task": task})
    log.info("Enqueued task kind=%s", payload.get("kind"))
