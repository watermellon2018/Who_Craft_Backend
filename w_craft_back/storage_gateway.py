"""Validated storage, signed delivery, and SSRF-safe remote image fetching."""

from __future__ import annotations

import hashlib
import ipaddress
import io
import mimetypes
import posixpath
import socket
import tempfile
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO, Iterable
from urllib.parse import ParseResult, urljoin, urlparse

from django.conf import settings
from django.core import signing
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.urls import reverse
from PIL import Image, ImageOps, UnidentifiedImageError


SIGNED_MEDIA_SALT = "w-craft-signed-media-v1"
DEFAULT_IMAGE_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_IMAGE_MAX_PIXELS = 20_000_000
DEFAULT_REMOTE_TIMEOUT_SECONDS = 15.0

_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}
_REMOTE_CONTENT_TYPES = {
    "application/octet-stream",
    *[mime for mime, _extension in _IMAGE_FORMATS.values()],
}
_PROJECT_ASSET_LIMITS = {
    "image": 10 * 1024 * 1024,
    "reference": 10 * 1024 * 1024,
    "storyboard": 10 * 1024 * 1024,
    "audio": 50 * 1024 * 1024,
    "video": 100 * 1024 * 1024,
    "document": 25 * 1024 * 1024,
    "model_3d": 100 * 1024 * 1024,
}


class StorageGatewayError(ValueError):
    """Base error for rejected media operations."""

    code = "INVALID_MEDIA"
    http_status = 400

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class MediaTooLarge(StorageGatewayError):
    code = "MEDIA_TOO_LARGE"
    http_status = 413


class UnsupportedMedia(StorageGatewayError):
    code = "UNSUPPORTED_MEDIA_TYPE"
    http_status = 415


class InvalidImage(StorageGatewayError):
    code = "INVALID_IMAGE"
    http_status = 415


class UnsafeRemoteMedia(StorageGatewayError):
    code = "UNSAFE_REMOTE_MEDIA"
    http_status = 502


@dataclass(frozen=True)
class NormalizedImage:
    """Decoded and re-encoded image ready for trusted storage."""

    data: bytes
    mime_type: str
    extension: str
    width: int
    height: int
    sha256: str


@dataclass(frozen=True)
class StoredMedia:
    """Metadata returned after a validated object is saved."""

    storage_key: str
    mime_type: str
    size_bytes: int
    sha256: str
    width: int | None = None
    height: int | None = None


def _positive_setting(name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _safe_namespace(namespace: str) -> str:
    normalized = posixpath.normpath(str(namespace or "").replace("\\", "/"))
    if normalized in {"", ".", "/"}:
        raise StorageGatewayError("A managed storage namespace is required.")
    normalized = normalized.strip("/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise StorageGatewayError("Unsafe storage namespace.")
    for part in path.parts:
        if not part or any(
            char not in "-_.abcdefghijklmnopqrstuvwxyz0123456789"
            for char in part.lower()
        ):
            raise StorageGatewayError("Unsafe storage namespace.")
    return path.as_posix()


def safe_storage_key(storage_key: str) -> str:
    """Normalize a relative storage key and reject traversal/absolute paths."""

    raw = str(storage_key or "").replace("\\", "/").strip()
    normalized = posixpath.normpath(raw).lstrip("/")
    path = PurePosixPath(normalized)
    if (
        not raw
        or raw.startswith("/")
        or path.is_absolute()
        or normalized in {"", "."}
        or ".." in path.parts
        or any(":" in part for part in path.parts)
    ):
        raise StorageGatewayError("Unsafe storage key.")
    return path.as_posix()


def _read_upload_bounded(upload, max_bytes: int) -> bytes:
    declared = int(getattr(upload, "size", 0) or 0)
    if declared > max_bytes:
        raise MediaTooLarge(f"File exceeds the {max_bytes}-byte limit.")
    payload = bytearray()
    chunks: Iterable[bytes]
    if hasattr(upload, "chunks"):
        chunks = upload.chunks()
    else:
        chunks = iter(lambda: upload.read(64 * 1024), b"")
    for chunk in chunks:
        if not chunk:
            continue
        payload.extend(chunk)
        if len(payload) > max_bytes:
            raise MediaTooLarge(f"File exceeds the {max_bytes}-byte limit.")
    try:
        upload.seek(0)
    except (AttributeError, OSError):
        pass
    return bytes(payload)


def normalize_image_bytes(
    payload: bytes,
    *,
    max_bytes: int = DEFAULT_IMAGE_MAX_BYTES,
    max_pixels: int = DEFAULT_IMAGE_MAX_PIXELS,
) -> NormalizedImage:
    """Validate image magic/decode and re-encode without untrusted metadata."""

    if not isinstance(payload, bytes) or not payload:
        raise InvalidImage("Image payload is empty.")
    if len(payload) > max_bytes:
        raise MediaTooLarge(f"Image exceeds the {max_bytes}-byte limit.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as probe:
                width, height = probe.size
                image_format = (probe.format or "").upper()
                is_animated = bool(getattr(probe, "is_animated", False))
                probe.verify()
        if image_format not in _IMAGE_FORMATS:
            raise UnsupportedMedia("Only JPEG, PNG and WEBP images are supported.")
        if width <= 0 or height <= 0 or width * height > max_pixels:
            raise InvalidImage(f"Image exceeds the {max_pixels}-pixel limit.")
        if is_animated:
            raise UnsupportedMedia("Animated images are not supported.")

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as source:
                source.load()
                image = ImageOps.exif_transpose(source)
                if image_format == "JPEG":
                    image = image.convert("RGB")
                elif image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                width, height = image.size
                output = io.BytesIO()
                save_options = {
                    "JPEG": {"quality": 90, "optimize": True},
                    "PNG": {"optimize": True},
                    "WEBP": {"quality": 90, "method": 4},
                }[image_format]
                image.save(output, format=image_format, **save_options)
    except StorageGatewayError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ) as exc:
        raise InvalidImage("File contents are not a valid supported image.") from exc

    normalized = output.getvalue()
    if len(normalized) > max_bytes:
        raise MediaTooLarge("Re-encoded image exceeds the byte limit.")
    mime_type, extension = _IMAGE_FORMATS[image_format]
    return NormalizedImage(
        data=normalized,
        mime_type=mime_type,
        extension=extension,
        width=width,
        height=height,
        sha256=hashlib.sha256(normalized).hexdigest(),
    )


def normalize_image_upload(
    upload,
    *,
    max_bytes: int = DEFAULT_IMAGE_MAX_BYTES,
    max_pixels: int = DEFAULT_IMAGE_MAX_PIXELS,
) -> NormalizedImage:
    """Read and normalize an uploaded image without trusting filename or MIME."""

    return normalize_image_bytes(
        _read_upload_bounded(upload, max_bytes),
        max_bytes=max_bytes,
        max_pixels=max_pixels,
    )


def store_normalized_image(
    image: NormalizedImage,
    *,
    namespace: str,
) -> StoredMedia:
    """Store a normalized image under a generated, non-user-controlled name."""

    safe_namespace = _safe_namespace(namespace)
    filename = f"{uuid.uuid4().hex}.{image.extension}"
    requested_key = f"{safe_namespace}/{filename}"
    stored_key = safe_storage_key(
        default_storage.save(requested_key, ContentFile(image.data, name=filename))
    )
    return StoredMedia(
        storage_key=stored_key,
        mime_type=image.mime_type,
        size_bytes=len(image.data),
        sha256=image.sha256,
        width=image.width,
        height=image.height,
    )


def store_image_upload(
    upload,
    *,
    namespace: str,
    max_bytes: int = DEFAULT_IMAGE_MAX_BYTES,
    max_pixels: int = DEFAULT_IMAGE_MAX_PIXELS,
) -> StoredMedia:
    """Normalize and store an uploaded image."""

    image = normalize_image_upload(
        upload,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
    )
    return store_normalized_image(image, namespace=namespace)


def store_image_bytes(
    payload: bytes,
    *,
    namespace: str,
    max_bytes: int = DEFAULT_IMAGE_MAX_BYTES,
    max_pixels: int = DEFAULT_IMAGE_MAX_PIXELS,
) -> StoredMedia:
    """Normalize and store generated/provider image bytes."""

    image = normalize_image_bytes(
        payload,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
    )
    return store_normalized_image(image, namespace=namespace)


def _sniff_non_image(head: bytes) -> tuple[str, str] | None:
    if head.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/mp4", "mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm", "webm"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if head.startswith(b"OggS"):
        return "audio/ogg", "ogg"
    if head.startswith(b"ID3") or (
        len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0
    ):
        return "audio/mpeg", "mp3"
    if head.startswith(b"glTF"):
        return "model/gltf-binary", "glb"
    return None


def _allowed_mimes_for_asset_type(asset_type: str) -> set[str]:
    if asset_type in {"image", "reference", "storyboard"}:
        return {mime for mime, _extension in _IMAGE_FORMATS.values()}
    if asset_type == "audio":
        return {"audio/mpeg", "audio/ogg", "audio/wav"}
    if asset_type == "video":
        return {"video/mp4", "video/webm"}
    if asset_type == "document":
        return {"application/pdf"}
    if asset_type == "model_3d":
        return {"model/gltf-binary"}
    return set()


def store_project_upload(
    upload,
    *,
    project_id: int,
    asset_type: str,
    max_pixels: int = DEFAULT_IMAGE_MAX_PIXELS,
) -> StoredMedia:
    """Validate and store a project asset according to its declared category."""

    max_bytes = _PROJECT_ASSET_LIMITS.get(asset_type)
    if max_bytes is None:
        raise UnsupportedMedia("Unsupported project asset category.")
    namespace = f"projects/{int(project_id)}/assets/{asset_type}"
    if asset_type in {"image", "reference", "storyboard"}:
        return store_image_upload(
            upload,
            namespace=namespace,
            max_bytes=max_bytes,
            max_pixels=max_pixels,
        )

    declared = int(getattr(upload, "size", 0) or 0)
    if declared > max_bytes:
        raise MediaTooLarge(f"File exceeds the {max_bytes}-byte limit.")
    hasher = hashlib.sha256()
    size = 0
    with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024) as temporary:
        chunks = upload.chunks() if hasattr(upload, "chunks") else iter(
            lambda: upload.read(64 * 1024), b""
        )
        for chunk in chunks:
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise MediaTooLarge(f"File exceeds the {max_bytes}-byte limit.")
            hasher.update(chunk)
            temporary.write(chunk)
        temporary.seek(0)
        sniffed = _sniff_non_image(temporary.read(32))
        if (
            sniffed is None
            or sniffed[0] not in _allowed_mimes_for_asset_type(asset_type)
        ):
            raise UnsupportedMedia("File magic does not match the project asset type.")
        mime_type, extension = sniffed
        temporary.seek(0)
        filename = f"{uuid.uuid4().hex}.{extension}"
        requested_key = f"{_safe_namespace(namespace)}/{filename}"
        stored_key = safe_storage_key(
            default_storage.save(requested_key, File(temporary, name=filename))
        )
    return StoredMedia(
        storage_key=stored_key,
        mime_type=mime_type,
        size_bytes=size,
        sha256=hasher.hexdigest(),
    )


def delete_storage_key(storage_key: str) -> None:
    """Delete a managed key if it exists."""

    key = safe_storage_key(storage_key)
    if default_storage.exists(key):
        default_storage.delete(key)


def _signed_media_ttl() -> int:
    return _positive_setting("SIGNED_MEDIA_TTL_SECONDS", 300)


def signed_media_url(
    storage_key: str | None,
    request=None,
    *,
    project=None,
) -> str | None:
    """Issue a short-lived bearer URL after optional project VIEW enforcement."""

    if not storage_key:
        return None
    key = safe_storage_key(storage_key)
    if project is not None:
        from w_craft_back.movie.project.policy import can_view

        actor = getattr(request, "user", None) if request is not None else None
        if actor is None or not actor.is_authenticated or not can_view(actor, project):
            return None
    token = signing.dumps(
        {"key": key, "project_id": getattr(project, "id", None)},
        salt=SIGNED_MEDIA_SALT,
        compress=True,
    )
    relative_url = reverse("signed-media", kwargs={"token": token})
    base_url = str(getattr(settings, "SIGNED_MEDIA_BASE_URL", "") or "").strip()
    if base_url:
        return urljoin(f"{base_url.rstrip('/')}/", relative_url.lstrip("/"))
    if request is not None:
        return request.build_absolute_uri(relative_url)
    public_base_url = str(
        getattr(settings, "PUBLIC_BASE_URL", "") or ""
    ).strip()
    if public_base_url:
        return urljoin(
            f"{public_base_url.rstrip('/')}/",
            relative_url.lstrip("/"),
        )
    return relative_url


def signed_url_for_file(file_field, request=None, *, project=None) -> str | None:
    """Issue a signed URL for a Django FileField without exposing MEDIA_URL."""

    if not file_field:
        return None
    return signed_media_url(getattr(file_field, "name", ""), request, project=project)


def storage_key_from_legacy_url(raw_url: str | None) -> str | None:
    """Extract a local storage key from an old ``/media/...`` URL."""

    if not raw_url:
        return None
    parsed = urlparse(str(raw_url).strip())
    path = parsed.path
    media_path = urlparse(str(getattr(settings, "MEDIA_URL", "/media/"))).path
    prefix = f"/{media_path.strip('/')}/"
    if not path.startswith(prefix):
        return None
    try:
        return safe_storage_key(path[len(prefix):])
    except StorageGatewayError:
        return None


def signed_url_for_asset(
    *,
    storage_key: str | None,
    legacy_url: str | None = None,
    request=None,
    project=None,
) -> str | None:
    """Issue a signed local URL for a storage-backed asset."""

    key = storage_key or storage_key_from_legacy_url(legacy_url)
    if key:
        return signed_media_url(key, request, project=project)

    # Compatibility-only browser URL. It is never fetched server-side, and
    # unsafe schemes/credential-bearing URLs remain rejected.
    parsed = urlparse(str(legacy_url or "").strip())
    if (
        parsed.scheme.lower() in {"http", "https"}
        and parsed.netloc
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    ):
        return parsed.geturl()
    return None


def _parse_byte_range(raw_header: str, total_size: int) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range, returning inclusive offsets."""

    if not raw_header:
        return None
    if not raw_header.startswith("bytes=") or "," in raw_header:
        raise ValueError("Unsupported byte range")
    bounds = raw_header[6:].strip()
    if "-" not in bounds:
        raise ValueError("Malformed byte range")
    start_text, end_text = bounds.split("-", 1)
    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("Invalid suffix byte range")
        start = max(total_size - suffix_length, 0)
        end = total_size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else total_size - 1
    if start < 0 or start >= total_size or end < start:
        raise ValueError("Unsatisfiable byte range")
    return start, min(end, total_size - 1)


def _range_chunks(handle: BinaryIO, start: int, length: int):
    remaining = length
    try:
        handle.seek(start)
        while remaining:
            chunk = handle.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()


def _apply_media_headers(response, key: str, content_type: str) -> None:
    disposition = (
        "inline"
        if content_type.startswith(("image/", "video/", "audio/"))
        or content_type == "model/gltf-binary"
        else "attachment"
    )
    response["Content-Disposition"] = (
        f'{disposition}; filename="{PurePosixPath(key).name}"'
    )
    response["Cache-Control"] = (
        f"private, max-age={min(_signed_media_ttl(), 300)}, no-transform"
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    response["Cross-Origin-Resource-Policy"] = "same-site"


def serve_signed_media(request, token: str):
    """Serve a signed media object with a private, non-sniffable response."""

    try:
        payload = signing.loads(
            token,
            salt=SIGNED_MEDIA_SALT,
            max_age=_signed_media_ttl(),
        )
        key = safe_storage_key(payload["key"])
    except (KeyError, TypeError, ValueError, signing.BadSignature) as exc:
        raise Http404 from exc
    if not default_storage.exists(key):
        raise Http404
    try:
        handle: BinaryIO = default_storage.open(key, "rb")
    except OSError as exc:
        raise Http404 from exc
    content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    try:
        total_size = int(default_storage.size(key))
        requested_range = _parse_byte_range(
            request.META.get("HTTP_RANGE", ""),
            total_size,
        )
    except (OSError, TypeError, ValueError):
        if request.META.get("HTTP_RANGE"):
            handle.close()
            response = HttpResponse(status=416)
            if "total_size" in locals():
                response["Content-Range"] = f"bytes */{total_size}"
            return response
        requested_range = None

    if requested_range is None:
        response = FileResponse(handle, content_type=content_type)
        if "total_size" in locals():
            response["Accept-Ranges"] = "bytes"
    else:
        start, end = requested_range
        length = end - start + 1
        response = StreamingHttpResponse(
            _range_chunks(handle, start, length),
            status=206,
            content_type=content_type,
        )
        response["Content-Length"] = str(length)
        response["Content-Range"] = f"bytes {start}-{end}/{total_size}"
        response["Accept-Ranges"] = "bytes"

    _apply_media_headers(response, key, content_type)
    return response


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def _resolve_remote_target(url: str) -> tuple[ParseResult, str, int]:
    """Validate a remote URL and pin it to one public DNS result."""

    parsed = urlparse(url)
    allow_http = bool(getattr(settings, "MEDIA_REMOTE_FETCH_ALLOW_HTTP", False))
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if (
        parsed.scheme.lower() not in allowed_schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise UnsafeRemoteMedia("Provider media URL is not allowed.")
    try:
        parsed_port = parsed.port
        port = parsed_port or (
            443 if parsed.scheme.lower() == "https" else 80
        )
        if parsed_port is not None and parsed_port <= 0:
            raise ValueError
    except ValueError as exc:
        raise UnsafeRemoteMedia("Provider media URL has an invalid port.") from exc
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise UnsafeRemoteMedia(
            "Provider media hostname could not be resolved."
        ) from exc
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise UnsafeRemoteMedia("Provider media URL resolves to a private network.")
    selected = min(
        addresses,
        key=lambda value: (ipaddress.ip_address(value).version, value),
    )
    return parsed, selected, port


def _origin_host_header(parsed: ParseResult, port: int) -> str:
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 443 if parsed.scheme == "https" else 80
    return hostname if port == default_port else f"{hostname}:{port}"


def fetch_remote_image(
    url: str,
    *,
    max_bytes: int | None = None,
    max_pixels: int | None = None,
    timeout_seconds: float | None = None,
    max_redirects: int = 2,
) -> NormalizedImage:
    """Fetch through a DNS-pinned connection, then decode and re-encode."""

    import urllib3

    byte_limit = max_bytes or _positive_setting(
        "IMAGE_PROVIDER_MAX_OUTPUT_BYTES",
        DEFAULT_IMAGE_MAX_BYTES,
    )
    pixel_limit = max_pixels or _positive_setting(
        "IMAGE_PROVIDER_MAX_OUTPUT_PIXELS",
        DEFAULT_IMAGE_MAX_PIXELS,
    )
    timeout = float(
        timeout_seconds
        or getattr(
            settings,
            "IMAGE_PROVIDER_FETCH_TIMEOUT_SECONDS",
            DEFAULT_REMOTE_TIMEOUT_SECONDS,
        )
    )
    if timeout <= 0:
        timeout = DEFAULT_REMOTE_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    current_url = str(url or "").strip()

    try:
        for redirect_count in range(max_redirects + 1):
            parsed, vetted_ip, port = _resolve_remote_target(current_url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UnsafeRemoteMedia("Provider media download timed out.")
            timeout_config = urllib3.Timeout(
                connect=min(5.0, remaining),
                read=remaining,
                total=remaining,
            )
            pool_kwargs = {
                "port": port,
                "timeout": timeout_config,
                "maxsize": 1,
                "block": True,
            }
            if parsed.scheme == "https":
                pool = urllib3.HTTPSConnectionPool(
                    vetted_ip,
                    cert_reqs="CERT_REQUIRED",
                    assert_hostname=parsed.hostname,
                    server_hostname=parsed.hostname,
                    **pool_kwargs,
                )
            else:
                pool = urllib3.HTTPConnectionPool(vetted_ip, **pool_kwargs)
            target = parsed.path or "/"
            if parsed.query:
                target = f"{target}?{parsed.query}"
            response = None
            try:
                response = pool.urlopen(
                    "GET",
                    target,
                    headers={
                        "Host": _origin_host_header(parsed, port),
                        "Accept": "image/png,image/jpeg,image/webp",
                        "User-Agent": "w-craft-media-fetch/1",
                    },
                    redirect=False,
                    retries=False,
                    preload_content=False,
                    decode_content=False,
                    timeout=timeout_config,
                )
                if response.status in {301, 302, 303, 307, 308}:
                    if redirect_count >= max_redirects:
                        raise UnsafeRemoteMedia(
                            "Provider media redirected too many times."
                        )
                    location = response.headers.get("Location")
                    if not location:
                        raise UnsafeRemoteMedia("Provider media redirect is invalid.")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status != 200:
                    raise UnsafeRemoteMedia(
                        f"Provider media returned HTTP {response.status}."
                    )
                content_type = (
                    response.headers.get("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if content_type and content_type not in _REMOTE_CONTENT_TYPES:
                    raise UnsupportedMedia("Provider response is not an image.")
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        declared_length = int(content_length)
                        if declared_length < 0:
                            raise ValueError
                        if declared_length > byte_limit:
                            raise MediaTooLarge(
                                "Provider image exceeds the byte limit."
                            )
                    except ValueError as exc:
                        raise UnsafeRemoteMedia(
                            "Provider sent an invalid Content-Length."
                        ) from exc
                payload = bytearray()
                for chunk in response.stream(64 * 1024):
                    if time.monotonic() > deadline:
                        raise UnsafeRemoteMedia("Provider media download timed out.")
                    if not chunk:
                        continue
                    payload.extend(chunk)
                    if len(payload) > byte_limit:
                        raise MediaTooLarge("Provider image exceeds the byte limit.")
                return normalize_image_bytes(
                    bytes(payload),
                    max_bytes=byte_limit,
                    max_pixels=pixel_limit,
                )
            finally:
                if response is not None:
                    response.release_conn()
                pool.close()
    except StorageGatewayError:
        raise
    except (OSError, urllib3.exceptions.HTTPError) as exc:
        raise UnsafeRemoteMedia("Provider media download failed.") from exc
    raise UnsafeRemoteMedia("Provider media download failed.")
