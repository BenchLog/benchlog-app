"""Unit tests for benchlog.gps_metadata."""

import io

import pytest
from PIL import Image

from benchlog.gps_metadata import StripFailed, has_gps_data, strip_gps, transcode_heic_to_jpeg


def _jpeg_with_gps(canary: str = "GPS_CANARY") -> bytes:
    img = Image.new("RGB", (40, 30), color=(180, 80, 60))
    exif = img.getexif()
    gps = exif.get_ifd(0x8825)
    gps[0x0001] = "N"
    gps[0x001B] = canary.encode()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _jpeg_without_gps() -> bytes:
    img = Image.new("RGB", (40, 30), color=(180, 80, 60))
    exif = img.getexif()
    exif[0x010E] = "no gps here"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def test_has_gps_true_for_jpeg_with_gps_ifd():
    assert has_gps_data(_jpeg_with_gps(), "image/jpeg") is True


def test_has_gps_false_for_jpeg_without_gps_ifd():
    assert has_gps_data(_jpeg_without_gps(), "image/jpeg") is False


def test_has_gps_false_for_plain_png():
    img = Image.new("RGB", (10, 10))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    assert has_gps_data(buf.getvalue(), "image/png") is False


def test_has_gps_returns_false_for_non_image_mime():
    assert has_gps_data(b"%PDF-1.4 stuff", "application/pdf") is False
    assert has_gps_data(b"any bytes", "application/octet-stream") is False


def test_has_gps_returns_false_for_corrupt_image():
    # Treat undecodable bytes as "no GPS" rather than raising — caller
    # already has thumbnail-failure handling for this case.
    assert has_gps_data(b"not an image", "image/jpeg") is False


def test_strip_gps_removes_gps_keeps_other_exif():
    img = Image.new("RGB", (40, 30), color=(180, 80, 60))
    exif = img.getexif()
    exif[0x010E] = "DESCRIPTION_CANARY"
    gps = exif.get_ifd(0x8825)
    gps[0x001B] = b"GPS_CANARY"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    raw = buf.getvalue()

    out = strip_gps(raw, "image/jpeg")

    assert b"GPS_CANARY" not in out
    assert b"DESCRIPTION_CANARY" in out
    assert has_gps_data(out, "image/jpeg") is False


def test_strip_gps_png_removes_gps_keeps_other_exif():
    img = Image.new("RGB", (40, 30), color=(180, 80, 60))
    exif = img.getexif()
    exif[0x010E] = "PNG_DESCRIPTION_CANARY"
    gps = exif.get_ifd(0x8825)
    gps[0x001B] = b"PNG_GPS_CANARY"
    buf = io.BytesIO()
    img.save(buf, format="PNG", exif=exif.tobytes())
    raw = buf.getvalue()
    assert b"PNG_GPS_CANARY" in raw
    assert has_gps_data(raw, "image/png") is True

    out = strip_gps(raw, "image/png")

    assert b"PNG_GPS_CANARY" not in out
    assert b"PNG_DESCRIPTION_CANARY" in out
    assert has_gps_data(out, "image/png") is False


def test_strip_gps_gif_passes_through_unchanged():
    img = Image.new("P", (10, 10))
    buf = io.BytesIO()
    img.save(buf, format="GIF")
    raw = buf.getvalue()
    assert strip_gps(raw, "image/gif") == raw


def test_strip_gps_tiff_removes_gps_keeps_other_exif():
    img = Image.new("RGB", (40, 30), color=(180, 80, 60))
    exif = img.getexif()
    exif[0x010E] = "TIFF_DESCRIPTION_CANARY"
    gps = exif.get_ifd(0x8825)
    gps[0x001B] = b"TIFF_GPS_CANARY"
    buf = io.BytesIO()
    img.save(buf, format="TIFF", exif=exif.tobytes())
    raw = buf.getvalue()
    assert has_gps_data(raw, "image/tiff") is True

    out = strip_gps(raw, "image/tiff")

    assert has_gps_data(out, "image/tiff") is False
    assert b"TIFF_DESCRIPTION_CANARY" in out


def test_strip_gps_webp_removes_gps_keeps_other_exif():
    img = Image.new("RGB", (40, 30), color=(180, 80, 60))
    exif = img.getexif()
    exif[0x010E] = "WEBP_DESCRIPTION_CANARY"
    gps = exif.get_ifd(0x8825)
    gps[0x001B] = b"WEBP_GPS_CANARY"
    buf = io.BytesIO()
    img.save(buf, format="WEBP", exif=exif.tobytes(), quality=90)
    raw = buf.getvalue()
    assert has_gps_data(raw, "image/webp") is True

    out = strip_gps(raw, "image/webp")

    assert has_gps_data(out, "image/webp") is False
    assert b"WEBP_DESCRIPTION_CANARY" in out


def test_strip_gps_avif_removes_gps_keeps_other_exif():
    img = Image.new("RGB", (40, 30), color=(180, 80, 60))
    exif = img.getexif()
    exif[0x010E] = "AVIF_DESCRIPTION_CANARY"
    gps = exif.get_ifd(0x8825)
    gps[0x001B] = b"AVIF_GPS_CANARY"
    buf = io.BytesIO()
    img.save(buf, format="AVIF", exif=exif.tobytes())
    raw = buf.getvalue()
    assert has_gps_data(raw, "image/avif") is True

    out = strip_gps(raw, "image/avif")

    assert has_gps_data(out, "image/avif") is False
    assert b"AVIF_DESCRIPTION_CANARY" in out


def test_strip_gps_raises_on_corrupt():
    with pytest.raises(StripFailed):
        strip_gps(b"not an image", "image/jpeg")


def test_strip_gps_preserves_jpeg_quality():
    """Removing GPS shouldn't recompress the JPEG. Without `quality="keep"`
    PIL's default 75 quality shrinks high-quality phone photos ~50%."""
    # Build a 400x300 JPEG with high-frequency content + GPS, encoded
    # at quality 95 (close to what phones produce).
    import random
    random.seed(0)
    img = Image.new("RGB", (400, 300))
    pixels = img.load()
    for y in range(300):
        for x in range(400):
            pixels[x, y] = (
                random.randint(0, 255),
                random.randint(0, 255),
                random.randint(0, 255),
            )
    exif = img.getexif()
    gps = exif.get_ifd(0x8825)
    gps[0x001B] = b"GPS_HERE"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, exif=exif.tobytes())
    raw = buf.getvalue()

    out = strip_gps(raw, "image/jpeg")

    # The stripped output should be within ~10% of the original size.
    # Without `quality="keep"` PIL would re-encode at quality 75 and
    # shrink the file by 30-50% (the user-reported bug). 10% absorbs
    # JPEG framing/header overhead from the round-trip while still
    # failing loudly if the keep-quality flag stops working.
    delta = abs(len(out) - len(raw)) / len(raw)
    assert delta < 0.10, (
        f"strip_gps re-encoded JPEG: original {len(raw):,} bytes, "
        f"stripped {len(out):,} bytes ({delta:.1%} delta)"
    )
    assert b"GPS_HERE" not in out


def test_transcode_heic_produces_jpeg():
    import pillow_heif as ph
    img = Image.new("RGB", (40, 30), color=(120, 60, 30))
    buf = io.BytesIO()
    ph.from_pillow(img).save(buf, format="HEIF")
    raw = buf.getvalue()

    out = transcode_heic_to_jpeg(raw)

    with Image.open(io.BytesIO(out)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (40, 30)


def test_transcode_heic_preserves_exif_so_gps_can_be_detected_after():
    """We need transcode to preserve EXIF (including GPS) so the post-
    transcode has_gps_data() check can find it. Stripping happens only
    when the user confirms via the modal.
    """
    import pillow_heif as ph
    pil = Image.new("RGB", (40, 30), color=(120, 60, 30))
    exif = pil.getexif()
    gps = exif.get_ifd(0x8825)
    gps[0x001B] = b"HEIC_GPS_CANARY"
    pil.info["exif"] = exif.tobytes()
    buf = io.BytesIO()
    ph.from_pillow(pil).save(buf, format="HEIF")
    raw = buf.getvalue()

    out = transcode_heic_to_jpeg(raw)

    assert has_gps_data(out, "image/jpeg") is True


def test_transcode_heic_raises_on_corrupt():
    with pytest.raises(StripFailed):
        transcode_heic_to_jpeg(b"not heic")


import uuid
from benchlog.files import store_upload, StoredBlob
from benchlog.storage import LocalStorage


async def test_store_upload_marks_has_gps_true(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = _jpeg_with_gps()
    blob = await store_upload(
        storage,
        file_id=uuid.uuid4(),
        version_number=1,
        source=io.BytesIO(raw),
        original_filename="photo.jpg",
        declared_mime="image/jpeg",
        max_bytes=10_000_000,
    )
    assert isinstance(blob, StoredBlob)
    assert blob.has_gps is True


async def test_store_upload_marks_has_gps_false(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = _jpeg_without_gps()
    blob = await store_upload(
        storage,
        file_id=uuid.uuid4(),
        version_number=1,
        source=io.BytesIO(raw),
        original_filename="photo.jpg",
        declared_mime="image/jpeg",
        max_bytes=10_000_000,
    )
    assert blob.has_gps is False


async def test_store_upload_marks_has_gps_none_for_non_image(tmp_path):
    storage = LocalStorage(tmp_path)
    blob = await store_upload(
        storage,
        file_id=uuid.uuid4(),
        version_number=1,
        source=io.BytesIO(b"binary stuff"),
        original_filename="model.stl",
        declared_mime="application/octet-stream",
        max_bytes=10_000_000,
    )
    assert blob.has_gps is None


async def test_store_upload_transcodes_heic(tmp_path):
    import pillow_heif as ph
    storage = LocalStorage(tmp_path)
    pil = Image.new("RGB", (40, 30), color=(120, 60, 30))
    buf = io.BytesIO()
    ph.from_pillow(pil).save(buf, format="HEIF")
    raw = buf.getvalue()

    blob = await store_upload(
        storage,
        file_id=uuid.uuid4(),
        version_number=1,
        source=io.BytesIO(raw),
        original_filename="IMG_1234.heic",
        declared_mime="image/heic",
        max_bytes=10_000_000,
    )

    assert blob.rewritten_filename == "IMG_1234.jpg"
    assert blob.rewritten_mime == "image/jpeg"
    on_disk = (tmp_path / blob.storage_path).read_bytes()
    with Image.open(io.BytesIO(on_disk)) as decoded:
        assert decoded.format == "JPEG"


async def test_store_upload_heic_caps_below_global_max(tmp_path, monkeypatch):
    """HEIC has a tighter in-memory cap than the global max_upload_size to
    bound the per-request memory footprint of the synchronous transcode."""
    from benchlog import files as files_module
    from benchlog.files import UploadTooLarge

    monkeypatch.setattr(files_module, "_HEIC_MAX_BYTES", 1024)

    storage = LocalStorage(tmp_path)
    # Body well above the patched HEIC cap but below `max_bytes`.
    bogus = b"x" * 4096

    with pytest.raises(UploadTooLarge):
        await store_upload(
            storage,
            file_id=uuid.uuid4(),
            version_number=1,
            source=io.BytesIO(bogus),
            original_filename="IMG.heic",
            declared_mime="image/heic",
            max_bytes=10_000_000,
        )
