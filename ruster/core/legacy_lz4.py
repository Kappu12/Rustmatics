"""Rust's LZ4Stream framing, not the incompatible modern LZ4 frame format.

Reader supports compressed and raw chunks. Writers default to raw chunks and
can opt into native block compression. The bounded Python decoder remains
available without asset dependencies installed.
Framing reference: MiloszKrajewski/lz4net LZ4Stream; no third-party code copied.
"""
from .wire import FormatError, read_varint, varint
try:
    import lz4.block as _native_lz4
except ImportError:
    _native_lz4=None

MAX_BYTES = 512 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024


def decompress_block(data, expected):
    if type(expected) is not int or not 0<=expected<=MAX_CHUNK:
        raise FormatError('Invalid LZ4 block size')
    if _native_lz4 is not None:
        try:result=_native_lz4.decompress(data,uncompressed_size=expected)
        except _native_lz4.LZ4BlockError as error:raise FormatError('Invalid compressed LZ4 block') from error
        # Native uncompressed_size is an upper bound, not an exact-size check.
        if len(result)!=expected:raise FormatError('LZ4 block length mismatch')
        return result
    return decompress_block_python(data,expected)


def decompress_block_python(data,expected):
    out, pos = bytearray(), 0

    def length(base, pos):
        if base == 15:
            while True:
                if pos >= len(data):
                    raise FormatError("Truncated LZ4 length")
                byte = data[pos]
                pos += 1
                base += byte
                if byte != 255:
                    break
        return base, pos

    while pos < len(data):
        token = data[pos]
        pos += 1
        literals, pos = length(token >> 4, pos)
        if pos + literals > len(data) or len(out) + literals > expected:
            raise FormatError("Invalid LZ4 literal length")
        out.extend(data[pos:pos + literals])
        pos += literals
        if pos == len(data):
            break
        if pos + 2 > len(data):
            raise FormatError("Truncated LZ4 match")
        offset = int.from_bytes(data[pos:pos + 2], 'little')
        pos += 2
        if offset == 0 or offset > len(out):
            raise FormatError("Invalid LZ4 match offset")
        count, pos = length(token & 15, pos)
        count += 4
        if len(out) + count > expected:
            raise FormatError("LZ4 block exceeds its declared size")
        pattern = bytes(out[-offset:])
        repeats, tail = divmod(count, offset)
        out.extend(pattern * repeats + pattern[:tail])
    if len(out) != expected:
        raise FormatError("LZ4 block length mismatch")
    return bytes(out)


def decompress(data, max_bytes=MAX_BYTES):
    out, pos = bytearray(), 0
    while pos < len(data):
        flags, pos = read_varint(data, pos)
        if flags & ~3:
            raise FormatError("Unsupported LZ4Stream chunk flags")
        original, pos = read_varint(data, pos)
        stored = original
        if flags & 1:
            stored, pos = read_varint(data, pos)
        if original > MAX_CHUNK or len(out) + original > max_bytes:
            raise FormatError("Map exceeds the decompression limit")
        if stored > original or stored > len(data) - pos:
            raise FormatError("Truncated or invalid LZ4Stream chunk")
        chunk = data[pos:pos + stored]
        pos += stored
        out.extend(decompress_block(chunk, original) if flags & 1 else chunk)
    return bytes(out)


def compress(data, block_size=1024 * 1024, *, compact=False):
    if not 0 < block_size <= MAX_CHUNK or len(data) > MAX_BYTES:
        raise FormatError("Map exceeds the supported size")
    chunks = []
    for start in range(0, len(data), block_size):
        chunk = data[start:start + block_size]
        size = varint(len(chunk))
        if compact and _native_lz4 is not None:
            packed = _native_lz4.compress(chunk, store_size=False)
            if len(packed) + len(varint(len(packed))) < len(chunk):
                chunks.append(b'\x01' + size + varint(len(packed)) + packed)
                continue
        chunks.append(b'\x00' + size + chunk)
    return b''.join(chunks)
