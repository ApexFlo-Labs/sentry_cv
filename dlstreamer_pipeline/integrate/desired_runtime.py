"""Desired-state helpers shared by the engine (integrate9.py) and the supervisor (runtime_supervisor.py).

The contract (sentry-v8-contract.yaml, desiredState) says: revision strictly increasing; validate the whole
revision, then apply it atomically; an invalid revision keeps the last valid one and reports not ready.
"""
import os
from pathlib import Path

from traffic_pilot_runtime.desired_state import DesiredStateValidator

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = Path(os.getenv("INTEGRATE_SECRETS", os.path.join(HERE, "secrets"))).resolve()
CONTAINER_SECRETS = "/run/secrets/sentinel/cameras/"


def host_path(source):
    """Outside the pod the camera URL files live in our secrets folder; the desired state keeps the contract path."""
    return source.replace("file:" + CONTAINER_SECRETS, f"file:{SECRETS}/", 1)


class HostSecretsValidator(DesiredStateValidator):
    def _validate_source(self, camera_id, value):
        return super()._validate_source(camera_id, host_path(value))


def load(path):
    """Her validator on the whole file (raises on any error)."""
    return HostSecretsValidator(SECRETS).load(Path(path))


def hot_reload_signature(desired):
    """Same fields as her solution_image_entrypoint._hot_reload_signature: a change in any of them needs a new
    engine; anything else (geometry, bindings' scope/object types/geometry ids, scene interval, ...) is applied
    to the running V8 event code through V8EventPipeline.reconfigure()."""
    return (
        desired.edge_id,
        desired.deployment_id,
        tuple(
            (
                camera.camera_id,
                camera.name,
                camera.source,
                camera.fps,
                camera.apps,
                tuple(sorted({profile for binding in camera.config["v8"]["bindings"]
                              for profile in binding.get("embedding_profile_ids") or []})),
            )
            for camera in desired.cameras
        ),
    )
