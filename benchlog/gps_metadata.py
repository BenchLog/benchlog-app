"""Detect and remove GPS location data from image uploads.

Threat model: a maker uploads a phone photo to share their build. The
photo carries the GPS where they took it — often their house. Public
viewers can read EXIF and doxx them.

Public surface:

- ``has_gps_data(data, mime)`` — boolean, never raises.
- ``strip_gps(data, mime)`` — returns scrubbed bytes, raises ``StripFailed``
  on parse error. JPEG/TIFF/WEBP/PNG/AVIF keep all other EXIF and ICC;
  GIF returns unchanged (no standard EXIF channel).
- ``transcode_heic_to_jpeg(data)`` — always run on HEIC at upload so the
  bytes that hit storage are JPEG, browser-renderable everywhere.
"""

import io

import pillow_heif

from PIL import Image, ImageOps, UnidentifiedImageError

pillow_heif.register_heif_opener()

# EXIF tag 0x8825 is the GPSInfo sub-IFD pointer. Removing this tag
# (rather than blanking the whole EXIF) preserves orientation, camera
# info, datetime, and copyright — anything the maker might want kept.
_GPS_IFD_TAG = 0x8825

_EXIF_FORMATS: frozenset[str] = frozenset({"JPEG", "TIFF", "WEBP", "PNG", "AVIF"})


class StripFailed(Exception):
    """Raised by strip_gps / transcode_heic_to_jpeg when bytes can't be parsed."""


def has_gps_data(data: bytes, declared_mime: str | None) -> bool:
    """True iff ``data`` is an image carrying a GPSInfo IFD.

    Never raises — undecodable bytes / non-image mimes return False so
    the caller doesn't need a try/except around routine upload checks.
    """
    mime = (declared_mime or "").lower()
    if not mime.startswith("image/"):
        return False
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            exif = img.getexif()
            # `getexif()` is lazy; touch get_ifd to force the GPS sub-IFD
            # to materialise so the membership check below is honest.
            gps = exif.get_ifd(_GPS_IFD_TAG)
            # Pillow returns an empty dict if the GPS sub-IFD pointer is
            # absent OR if it points at an empty IFD — both are "no GPS".
            return bool(gps)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return False


def strip_gps(data: bytes, declared_mime: str | None) -> bytes:
    """Return ``data`` with the GPSInfo IFD removed.

    For JPEG/TIFF/WEBP/PNG/AVIF: open, drop tag 0x8825 from EXIF, re-encode
    with the rest of EXIF + ICC preserved. GIF has no standard EXIF channel
    and passes through unchanged. HEIC must be transcoded first via
    ``transcode_heic_to_jpeg`` (callers shouldn't pass HEIC here).

    Raises ``StripFailed`` if the bytes can't be parsed.
    """
    mime = (declared_mime or "").lower()
    if not mime.startswith("image/"):
        return data
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            output_format = img.format
            if output_format not in _EXIF_FORMATS:
                # GIF / others: no portable EXIF channel.
                return data

            icc = img.info.get("icc_profile")
            exif = img.getexif()
            exif.get_ifd(_GPS_IFD_TAG)
            if _GPS_IFD_TAG in exif:
                del exif[_GPS_IFD_TAG]

            save_kwargs: dict = {
                "format": output_format,
                "exif": exif.tobytes(),
            }
            if icc:
                save_kwargs["icc_profile"] = icc
            # Re-encoding considerations per format:
            # - JPEG: `quality="keep"` reuses the source's quantization
            #   tables so a GPS-only edit doesn't trigger a recompress
            #   at PIL's default 75 (drops an iPhone photo ~50%).
            # - WEBP: no keep-quality flag; pin to 95/method=6 so the
            #   round-trip is at least visually lossless.
            # - AVIF: same shape as WEBP — pin a high quality so the
            #   strip doesn't degrade the source.
            # - PNG: lossless encoder, nothing to pin.
            # - TIFF: defaults are lossless for typical inputs.
            if output_format == "JPEG":
                save_kwargs["quality"] = "keep"
            elif output_format == "WEBP":
                save_kwargs["quality"] = 95
                save_kwargs["method"] = 6
            elif output_format == "AVIF":
                save_kwargs["quality"] = 90

            buf = io.BytesIO()
            img.save(buf, **save_kwargs)
            return buf.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise StripFailed(f"could not parse image: {e}") from e


def transcode_heic_to_jpeg(data: bytes) -> bytes:
    """Decode HEIC/HEIF, re-encode as JPEG, preserve EXIF + ICC.

    BenchLog always transcodes HEIC at upload because Chrome and Firefox
    don't natively render HEIC, and serving original-bytes-only would
    break the inline browser preview. JPEG quality 92 is a small visible
    quality hit vs the source HEIC; storage roughly doubles per photo.
    Both costs were accepted in design — see plan for tradeoff notes.

    EXIF is preserved so the upload route's GPS detection can run on
    the transcoded bytes; the strip-GPS modal action drops it on demand.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            oriented = ImageOps.exif_transpose(img).convert("RGB")
            icc = img.info.get("icc_profile")
            exif_bytes = img.info.get("exif", b"")
            buf = io.BytesIO()
            save_kwargs: dict = {"format": "JPEG", "quality": 92}
            if exif_bytes:
                save_kwargs["exif"] = exif_bytes
            if icc:
                save_kwargs["icc_profile"] = icc
            oriented.save(buf, **save_kwargs)
            return buf.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise StripFailed(f"could not parse heic: {e}") from e
