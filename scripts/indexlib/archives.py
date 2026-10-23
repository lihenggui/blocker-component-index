"""Read a ZIP manifest by HTTP ranges, with a size-capped full-download fallback."""

import hashlib
import io
import re
import struct
import zipfile
import zlib


def eocd(raw):
    position = len(raw)
    while True:
        position = raw.rfind(b"PK\x05\x06", 0, position)
        if position < 0:
            raise ValueError("ZIP end-of-central-directory not found")
        if position + 22 <= len(raw):
            fields = struct.unpack_from("<4s4H2LH", raw, position)
            if position + 22 + fields[7] == len(raw):
                if fields[1] or fields[2] or fields[3] != fields[4]:
                    raise ValueError("Multi-disk ZIP is unsupported")
                if fields[4] == 0xffff or fields[5] == 0xffffffff or fields[6] == 0xffffffff:
                    raise ValueError("ZIP64 requires full download")
                return {"count": fields[4], "size": fields[5], "offset": fields[6]}


def manifest_entry(raw, count):
    position = 0
    for _ in range(count):
        if position + 46 > len(raw):
            raise ValueError("Truncated ZIP central directory")
        fields = struct.unpack_from("<4s6H3L5H2L", raw, position)
        if fields[0] != b"PK\x01\x02":
            raise ValueError("Invalid ZIP central-directory signature")
        end = position + 46 + fields[10] + fields[11] + fields[12]
        if end > len(raw):
            raise ValueError("Truncated ZIP central-directory entry")
        name = raw[position + 46:position + 46 + fields[10]].decode("utf-8" if fields[3] & 0x800 else "cp437")
        if name == "AndroidManifest.xml":
            if fields[3] & 1 or fields[4] not in (0, 8) or fields[13]:
                raise ValueError("Encrypted or unsupported manifest ZIP entry")
            return {"method": fields[4], "crc": fields[7], "compressed": fields[8],
                    "size": fields[9], "offset": fields[16]}
        position = end
    raise ValueError("AndroidManifest.xml is absent")


def _content_range(response, start=None, end=None):
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
    if response.status != 206 or not match:
        raise ValueError("Server did not honor Range")
    first, last, total = map(int, match.groups())
    if len(response.body) != last - first + 1 or last >= total or (start is not None and first != start) or (end is not None and last != end):
        raise ValueError("Invalid Content-Range")
    return first, last, total


def _range(client, url, first, last, limit):
    response = client.get(url, headers={"Range": f"bytes={first}-{last}"}, limit=limit)
    _content_range(response, first, last)
    return response.body


def _inflate(data, entry, limit):
    if entry["size"] > limit:
        raise ValueError("Manifest exceeds size cap")
    if entry["method"] == 8:
        inflater = zlib.decompressobj(-15)
        raw = inflater.decompress(data, limit + 1)
        if not inflater.eof or inflater.unused_data or len(raw) > limit:
            raise ValueError("Invalid or oversized compressed manifest")
    else:
        raw = data
    if len(raw) != entry["size"] or zlib.crc32(raw) & 0xffffffff != entry["crc"]:
        raise ValueError("Manifest ZIP size or CRC mismatch")
    return raw


def read_manifest(client, url, max_bytes):
    response = client.get(url, headers={"Range": "bytes=-65536"}, limit=max_bytes)
    if response.status == 206:
        try:
            start, last, total = _content_range(response)
            if start != max(0, total - 65536) or last != total - 1:
                raise ValueError("Invalid ZIP tail range")
            directory = eocd(response.body)
            end = directory["offset"] + directory["size"]
            if directory["size"] > max_bytes or end > total:
                raise ValueError("Invalid or oversized central directory")
            raw = (response.body[directory["offset"] - start:end - start] if directory["offset"] >= start
                   else _range(client, url, directory["offset"], end - 1, max_bytes))
            entry = manifest_entry(raw, directory["count"])
            if entry["compressed"] > max_bytes:
                raise ValueError("Compressed manifest exceeds size cap")
            header = _range(client, url, entry["offset"], entry["offset"] + 29, max_bytes)
            fields = struct.unpack("<4s5H3L2H", header)
            if fields[0] != b"PK\x03\x04" or fields[3] != entry["method"] or fields[2] & 1:
                raise ValueError("Invalid ZIP local header")
            offset = entry["offset"] + 30 + fields[9] + fields[10]
            data = _range(client, url, offset, offset + entry["compressed"] - 1, max_bytes)
            manifest = _inflate(data, entry, min(max_bytes, 16 * 1024 * 1024))
            return manifest, hashlib.sha256(manifest).hexdigest()
        except (ValueError, struct.error, zlib.error):
            response = client.get(url, limit=max_bytes)
    elif response.status != 200:
        raise OSError(f"HTTP {response.status}: {url}")
    if response.status != 200:
        raise OSError(f"Full AAR HTTP {response.status}: {url}")
    if len(response.body) > max_bytes:
        raise ValueError("AAR exceeds size cap")
    with zipfile.ZipFile(io.BytesIO(response.body)) as zipped:
        info = zipped.getinfo("AndroidManifest.xml")
        if info.file_size > min(max_bytes, 16 * 1024 * 1024):
            raise ValueError("Manifest exceeds size cap")
        manifest = zipped.read(info)
    return manifest, hashlib.sha256(response.body).hexdigest()
