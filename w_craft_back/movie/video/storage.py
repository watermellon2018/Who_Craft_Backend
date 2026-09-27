"""Bounded MP4 structural checks before private project storage."""
import struct

from django.core.files.base import ContentFile

from w_craft_back.storage_gateway import StorageGatewayError, store_project_upload


def _boxes(data: bytes, start=0, end=None):
    end = len(data) if end is None else end
    count = 0
    while start < end:
        if end - start < 8 or count > 10000:
            raise StorageGatewayError("Invalid MP4 container.")
        size, kind = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1:
            if end - start < 16:
                raise StorageGatewayError("Invalid MP4 box.")
            size = struct.unpack_from(">Q", data, start + 8)[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header or size > end - start:
            raise StorageGatewayError("Invalid MP4 box size.")
        yield kind, start + header, start + size
        start += size
        count += 1


def video_metadata(data: bytes, content_type: str) -> dict:
    if (content_type.split(";", 1)[0].strip() != "video/mp4"
            or not 32 <= len(data) <= 100 * 1024 * 1024):
        raise StorageGatewayError("Unsupported or oversized generated video.")
    duration = None
    width = height = 0
    has_media = has_type = False
    for kind, start, end in _boxes(data):
        has_type |= kind == b"ftyp"
        has_media |= kind == b"mdat" and end > start
        if kind != b"moov":
            continue
        for child, begin, stop in _boxes(data, start, end):
            if child == b"mvhd":
                version = data[begin] if stop > begin else 255
                offset = begin + (20 if version == 1 else 12)
                required = 12 if version == 1 else 8
                if version not in (0, 1) or stop - offset < required:
                    raise StorageGatewayError("Invalid video duration header.")
                timescale = struct.unpack_from(">I", data, offset)[0]
                ticks = struct.unpack_from(
                    ">Q" if version == 1 else ">I", data, offset + 4,
                )[0]
                duration = ticks / timescale if timescale else None
            elif child == b"trak":
                for entry, payload, finish in _boxes(data, begin, stop):
                    if entry == b"tkhd" and finish - payload >= 84:
                        track_width, track_height = struct.unpack_from(
                            ">II", data, finish - 8,
                        )
                        track_width //= 65536
                        track_height //= 65536
                        if track_width and track_height:
                            width, height = track_width, track_height
                            break
    if (not has_type or not has_media or not duration
            or not 0 < duration <= 600
            or not 0 < width <= 16384 or not 0 < height <= 16384):
        raise StorageGatewayError("Generated video lacks valid duration or dimensions.")
    return {"durationSeconds": duration, "width": width, "height": height}


def store_generated_video(data: bytes, content_type: str, project_id: int):
    metadata = video_metadata(data, content_type)
    stored = store_project_upload(ContentFile(data, name="generated.mp4"),
                                  project_id=project_id, asset_type="video")
    return stored, metadata
