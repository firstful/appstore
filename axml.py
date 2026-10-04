"""Minimal Android binary-manifest (AXML) reader — extracts versionCode.

Why this exists
---------------
The store container is a small python image with no Android SDK, so aapt2 is
never available and `versionCode` could only ever come from CI. That made the
catalog's versionCode depend on each app repo remembering to send it — and
three of them did not. An entry with no versionCode has no ordering
information at all, so the client cannot distinguish "up to date" from "an
update exists" and reports "Up to date" forever.

Reading the value straight out of the APK removes the dependency entirely: the
store is then the single authority for every published app, published and
CI-reported values must agree, and already-published apps backfill for free.

AXML is a chunked binary format. We only need the string pool (for attribute
names) and the start-element chunks, which is what `res/xml` chunks in a
compiled APK always use.
"""
import struct
import zipfile

# Chunk types from the resource-ids / chunk headers we care about.
_CHUNK_STRING_POOL = 0x001C0001
_CHUNK_XML = 0x0003
_CHUNK_XML_START_ELEMENT = 0x0102
_CHUNK_XML_END_ELEMENT = 0x0103

# Res_value data types.
_TYPE_INT_DEC = 0x10

# Attribute name we are after, plus the namespace attribute namespace URI
# (the Android platform manifest namespace).
_ATTR_VERSION_CODE = "versionCode"
_ANDROID_NS = "http://schemas.android.com/apk/res/android"

_U32 = 0xFFFFFFFF


def _utf16_len_to_bytes(buf: int) -> int:
    """Length field is in UTF-16 code units; byte length is units * 2."""
    return buf * 2


def _parse_string_pool(data: bytes, start: int) -> list[str]:
    """Decode a RES_STRING_POOL_TYPE chunk into a list of strings."""
    if len(data) < start + 28:
        return []
    (chunk_size,) = struct.unpack_from("<I", data, start + 4)
    (string_count,) = struct.unpack_from("<I", data, start + 8)
    (flags,) = struct.unpack_from("<I", data, start + 16)
    (strings_start,) = struct.unpack_from("<I", data, start + 20)
    is_utf8 = bool(flags & (1 << 8))
    if string_count > 0x10000 or chunk_size <= 0 or start + chunk_size > len(data):
        return []

    offsets_at = start + 28
    base = start + strings_start
    limit = start + chunk_size
    out: list[str] = []
    for i in range(string_count):
        off_at = offsets_at + i * 4
        if off_at + 4 > limit:
            break
        (off,) = struct.unpack_from("<I", data, off_at)
        p = base + off
        if p >= limit:
            out.append("")
            continue
        try:
            if is_utf8:
                # UTF-8: u8 utf16_len, u8 utf8_len, <bytes>, 0x00
                n1 = data[p]
                p += 1
                if n1 & 0x80:
                    p += 1
                n2 = data[p]
                p += 1
                if n2 & 0x80:
                    n2 = ((n2 & 0x7F) << 8) | data[p]
                    p += 1
                out.append(data[p:p + n2].decode("utf-8", "replace"))
            else:
                # UTF-16 (what aapt2 actually emits for AndroidManifest.xml):
                # u16 len, then len UTF-16 code units, then 0x0000.
                n = struct.unpack_from("<H", data, p)[0]
                p += 2
                if n & 0x8000:  # two-word length
                    hi = n & 0x7FFF
                    (lo,) = struct.unpack_from("<H", data, p)
                    p += 2
                    n = (hi << 16) | lo
                out.append(data[p:p + n * 2].decode("utf-16-le", "replace"))
        except (IndexError, struct.error):
            out.append("")
    return out


def version_code_from_apk(path: str) -> int | None:
    """versionCode declared by the APK at `path`, or None if unreadable.

    Returns None — never a guess — when the manifest cannot be parsed. The
    caller decides how to treat that; this function only reports facts.
    """
    try:
        with zipfile.ZipFile(path) as z:
            try:
                raw = z.read("AndroidManifest.xml")
            except KeyError:
                return None
    except (zipfile.BadZipFile, OSError):
        return None
    return version_code_from_axml(raw)


def version_code_from_axml(data: bytes) -> int | None:
    """versionCode from an in-memory binary AndroidManifest."""
    if len(data) < 8:
        return None
    magic, file_size = struct.unpack_from("<II", data, 0)
    # The XML tree header carries the extended-header flag in bit 31, so the
    # real type is the low 16 bits.
    if (magic & 0x0000FFFF) != _CHUNK_XML:
        return None
    end = min(file_size, len(data))
    if end <= 8:
        end = len(data)

    pos = 8
    strings: list[str] = []
    found: int | None = None
    # Depth is tracked so we only match the <manifest> element, not e.g. the
    # <application android:versionCode> style nesting of other chunks.
    depth = 0

    while pos + 8 <= end:
        chunk_type, chunk_size = struct.unpack_from("<II", data, pos)
        # XML node chunks carry an extended (0x80000000) header flag; their
        # type lives in the low 16 bits. The string pool is not a node chunk
        # and must be matched on its full type.
        node_type = chunk_type & 0x0000FFFF
        if chunk_size < 8 or pos + chunk_size > end:
            break
        if chunk_type == _CHUNK_STRING_POOL:
            strings = _parse_string_pool(data, pos)
        elif node_type == _CHUNK_XML_START_ELEMENT:
            body = pos + 16  # skip chunk header + lineNumber + comment
            if body + 20 <= end and depth == 0:
                # ns, name, attributeStart, attributeSize, attributeCount, ...
                _ns, name_idx, _as, _asz, attr_count = struct.unpack_from(
                    "<IIHHH", data, body
                )
                el_name = strings[name_idx] if name_idx < len(strings) else ""
                if el_name == "manifest" and attr_count:
                    # First attribute begins immediately after the
                    # start-element header fields; attributeSize (always 20)
                    # is the stride, so use it when sane.
                    attrs_at = body + (_asz if _asz == 20 else 20)
                    stride = 20
                    for i in range(attr_count):
                        a = attrs_at + i * stride
                        if a + 20 > end:
                            break
                        a_ns, a_name, _raw, _typed = struct.unpack_from(
                            "<IIiI", data, a
                        )
                        # a_name is a string-pool index; -1 means absent.
                        if a_name == 0xFFFFFFFF or a_name >= len(strings):
                            continue
                        if strings[a_name] != _ATTR_VERSION_CODE:
                            continue
                        # Require the android: namespace, so a same-named
                        # attribute in another namespace is not mistaken for it.
                        ns_ok = (
                            a_ns == 0xFFFFFFFF  # no namespace
                            or (a_ns < len(strings) and strings[a_ns] == _ANDROID_NS)
                        )
                        if not ns_ok:
                            continue
                        # Res_value layout is: u16 size | u8 res0 | u8 dataType |
                        # u32 data. dataType is ONE byte at offset 15; reading it
                        # as two bytes bleeds into data and never matches.
                        data_type = data[a + 15]
                        if data_type == _TYPE_INT_DEC:
                            found = struct.unpack_from("<i", data, a + 16)[0]
                        break
            depth += 1
        elif node_type == _CHUNK_XML_END_ELEMENT:
            depth -= 1
            if depth <= 0 and found is not None:
                break
        pos += chunk_size

    return found
