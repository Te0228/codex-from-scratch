"""Chapter 23 local core, NOT a second Loop or a production filesystem boundary.

Requires Pillow. Only synthetic input is used by the probe. The root is a trusted,
single-writer test directory; production path grants and event journal belong to
earlier chapters. No network or model API is called here.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import wave

from PIL import Image


class MediaError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inspect_bytes(data: bytes, mime: str) -> dict:
    if mime == "audio/wav":
        try:
            with wave.open(BytesIO(data), "rb") as audio:
                frames, rate = audio.getnframes(), audio.getframerate()
                body = audio.readframes(frames)
                if frames <= 0 or len(body) != frames * audio.getnchannels() * audio.getsampwidth():
                    raise MediaError("truncated_audio")
                return {"duration_ms": (frames * 1000 + rate - 1) // rate}
        except (wave.Error, EOFError) as exc:
            raise MediaError("invalid_audio") from exc
    if mime not in {"image/png", "image/jpeg"}:
        raise MediaError("unsupported_mime")
    try:
        with Image.open(BytesIO(data)) as im:
            actual = Image.MIME.get(im.format)
            if actual != mime:
                raise MediaError("mime_mismatch")
            if im.width * im.height > 20_000_000:
                raise MediaError("pixel_budget")
            im.load()  # Decode pixels; a filename or signature is insufficient.
            return {"width": im.width, "height": im.height, "mode": im.mode}
    except (OSError, Image.DecompressionBombError) as exc:
        raise MediaError("invalid_image") from exc


def publish_bytes(path: Path, data: bytes) -> None:
    """Complete file before publishing, refuse overwrite; same-filesystem link.

    No crash-durability claim: directory fsync and hostile concurrent directory
    mutation are outside this trusted-root prototype.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(tmp, path)  # Fails if final destination already exists.
    finally:
        tmp.unlink(missing_ok=True)


class AttachmentStore:
    def __init__(self, root: Path, max_bytes: int = 8 * 1024 * 1024):
        self.root, self.max_bytes = Path(root), max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def ingest(self, data: bytes, mime: str, source: str, parent: str | None = None) -> dict:
        if len(data) > self.max_bytes:
            raise MediaError("byte_budget")
        metadata = inspect_bytes(data, mime)
        sha = digest(data)
        ref = {"id": "sha256:" + sha, "sha256": sha, "mime": mime,
               "size": len(data), "source": source, "parent": parent, **metadata}
        path = self.root / sha
        try:
            publish_bytes(path, data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise MediaError("stored_hash_mismatch")
        return ref

    def resolve(self, ref: dict) -> bytes:
        sha = ref["sha256"]
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise MediaError("invalid_reference")
        if ref["id"] != "sha256:" + sha:
            raise MediaError("invalid_reference")
        data = (self.root / sha).read_bytes()
        if digest(data) != sha:
            raise MediaError("stored_hash_mismatch")
        actual_metadata = inspect_bytes(data, ref["mime"])
        if len(data) != ref["size"] or any(ref.get(k) != v for k, v in actual_metadata.items()):
            raise MediaError("reference_metadata_mismatch")
        return data

    def preview(self, ref: dict) -> dict:
        data = self.resolve(ref)
        return {"attachment_id": ref["id"], "preview_bytes": len(data),
                "model_delivery": "not_submitted"}

    def prepare_content(self, ref: dict, capabilities: frozenset[str], *,
                        max_dimension: int = 1000, detail: str = "high",
                        max_wire_bytes: int = 12 * 1024 * 1024,
                        max_audio_ms: int = 60_000) -> tuple[dict, dict]:
        modality = ref["mime"].split("/")[0]
        if modality not in capabilities:
            raise MediaError("unsupported_model_modality")
        data = self.resolve(ref)
        prepared = ref
        if modality == "image":
            # Teaching model supports low/high. Codex's read branch rejects low.
            if detail not in {"low", "high"} or max_dimension <= 0:
                raise MediaError("unsupported_detail_or_budget")
            with Image.open(BytesIO(data)) as im:
                if max(im.size) > max_dimension:
                    im.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                    target = BytesIO()
                    im.save(target, format="PNG")
                    data = target.getvalue()
                    prepared = self.ingest(data, "image/png", "resize", ref["id"])
        elif ref["duration_ms"] > max_audio_ms:
            raise MediaError("audio_duration_budget")
        encoded = base64.b64encode(data).decode("ascii")
        url = "data:" + prepared["mime"] + ";base64," + encoded
        if len(url.encode()) > max_wire_bytes:
            raise MediaError("wire_budget")
        field = "image_url" if modality == "image" else "audio_url"
        content = {"type": "input_" + modality, field: url}
        if modality == "image":
            content["detail"] = detail
        return prepared, content


@dataclass
class ImageJob:
    job_id: str
    operation: str
    input_refs: list[dict]
    transparent_background: bool
    remote: str = "not_submitted"
    delivery: str = "not_attempted"
    provider_request_id: str | None = None
    result_ref: dict | None = None
    artifact: dict | None = None
    cancel_requested: bool = False
    error: str | None = None


class Jobs:
    """Explicit state owner; provider is a byte-returning boundary in the probe."""
    def __init__(self, root: Path, store: AttachmentStore):
        self.root, self.store = Path(root), store
        self.root.mkdir(parents=True, exist_ok=True)

    def record(self, job: ImageJob) -> None:
        if not job.job_id.isalnum():
            raise MediaError("invalid_job_id")
        data = json.dumps(asdict(job), ensure_ascii=False, sort_keys=True).encode()
        fd, raw = tempfile.mkstemp(prefix=".state-", dir=self.root)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(raw, self.root / (job.job_id + ".json"))

    def request(self, job: ImageJob, prompt: str) -> dict:
        if job.remote != "not_submitted" or job.cancel_requested:
            raise MediaError("unsafe_resubmit")
        if job.operation not in {"generate", "edit"}:
            raise MediaError("invalid_operation")
        if ((job.operation == "generate" and job.input_refs) or
                (job.operation == "edit" and not 1 <= len(job.input_refs) <= 5)):
            raise MediaError("invalid_edit_references")
        images = []
        for ref in job.input_refs:
            if not ref["mime"].startswith("image/"):
                raise MediaError("edit_requires_images")
            images.append({"attachment_id": ref["id"], "sha256": ref["sha256"],
                           "data": self.store.resolve(ref)})
        job.remote = "pending"
        self.record(job)
        return {"operation": job.operation, "prompt": prompt, "images": images,
                "transparent_background": job.transparent_background}

    def complete(self, job: ImageJob, data: bytes, request_id: str) -> None:
        if job.remote not in {"pending", "unknown"}:
            raise MediaError("unexpected_provider_result")
        job.remote = "succeeded"
        job.provider_request_id = request_id
        # Provider success and payload validity are different facts.
        self.record(job)
        try:
            job.result_ref = self.store.ingest(data, "image/png", "provider:" + request_id)
        except (OSError, MediaError) as exc:
            job.delivery, job.error = "invalid_or_unstaged_result", str(exc)
            self.record(job)
            return
        job.delivery = "cancelled_before_commit" if job.cancel_requested else "staged"
        self.record(job)

    def save_artifact(self, job: ImageJob, destination: Path) -> None:
        if job.cancel_requested:
            if job.delivery != "committed":
                job.delivery = "cancelled_before_commit"
                self.record(job)
            return
        if job.remote != "succeeded" or job.result_ref is None:
            raise MediaError("no_valid_provider_result")
        if job.delivery == "committed":
            # Recovery validates the existing artifact instead of generating again.
            if digest(Path(job.artifact["path"]).read_bytes()) != job.result_ref["sha256"]:
                raise MediaError("artifact_hash_mismatch")
            return
        try:
            payload = self.store.resolve(job.result_ref)
            publish_bytes(Path(destination), payload)
            actual = Path(destination).read_bytes()
            if digest(actual) != job.result_ref["sha256"]:
                raise MediaError("artifact_hash_mismatch")
            inspect_bytes(actual, "image/png")
            job.artifact = {"path": str(Path(destination).resolve()),
                            "sha256": digest(actual), "attachment_id": job.result_ref["id"]}
            job.delivery, job.error = "committed", None
        except (OSError, MediaError) as exc:
            job.delivery, job.error = "failed", type(exc).__name__ + ":" + str(exc)
        self.record(job)

    def cancel(self, job: ImageJob) -> None:
        job.cancel_requested = True
        if job.remote == "pending":
            job.remote = "unknown"  # Local cancellation is not remote acknowledgement.
        if job.delivery == "staged":
            job.delivery = "cancelled_before_commit"
        self.record(job)

    def restore(self, job_id: str) -> ImageJob:
        if not job_id.isalnum():
            raise MediaError("invalid_job_id")
        job = ImageJob(**json.loads((self.root / (job_id + ".json")).read_text()))
        if job.result_ref:
            self.store.resolve(job.result_ref)
        if job.artifact and digest(Path(job.artifact["path"]).read_bytes()) != job.artifact["sha256"]:
            raise MediaError("artifact_hash_mismatch")
        return job


def attach_readme(job: ImageJob, workspace: Path) -> dict:
    if job.delivery != "committed" or not job.artifact:
        raise MediaError("artifact_not_committed")
    path = Path(job.artifact["path"])
    if digest(path.read_bytes()) != job.artifact["sha256"]:
        raise MediaError("artifact_hash_mismatch")
    relative = path.relative_to(workspace.resolve()).as_posix()
    readme = workspace / "README.md"
    with readme.open("a", encoding="utf-8") as output:
        output.write("\n![Task-list illustration](" + relative + ")\n")
    text = readme.read_text()
    if relative not in text or not (workspace / relative).is_file():
        raise MediaError("readme_reference_invalid")
    return {"readme": str(readme), "target": relative, "sha256": digest(path.read_bytes())}
