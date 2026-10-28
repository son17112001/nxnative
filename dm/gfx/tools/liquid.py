"""11 bit studios' Liquid Engine archives (Frostpunk: common, templates, textures-s3, ... in the RomFS root): <stem>.idx, <stem>.str and <stem>.dat.
.idx: 3 header bytes (00 05 02), u32 entry count, u32 0, then count records of 29 bytes {u32 id, u64 stored size, u64 size, u64 offset in .dat, u8 zstd}.
.str: the same 11-byte header, then {u32 id, u32 length, name} per named entry (Frostpunk's templates archive has 11 entries without a name).
.dat: each entry's bytes at its offset, one zstd frame when the record's last byte is 1.
Read through romfs so archives inside a RomFS image need no extraction. Each entry's range is checked against the .dat before it is read, and its size bounds the decode.
"""
import struct
from romfs import os, open

MAGIC = b'\0\5\2'
HEAD = 11
RECORD = struct.Struct('<IQQQB')


def _count(head, n):   # the entry count of an 11-byte .idx / .str header; None when it is not one, or (with n, the file's size) when an .idx's size does not fit it
    if len(head) < HEAD or head[:3] != MAGIC: return None
    count = struct.unpack_from('<I', head, 3)[0]
    return count if n is None or n == HEAD + RECORD.size * count else None


def is_archive(path):
    """<stem>.dat with its .idx and .str beside it, the .idx's size matching its entry count"""
    stem, ext = os.path.splitext(path)
    if ext != '.dat' or not (os.path.exists(stem + '.idx') and os.path.exists(stem + '.str')): return False
    with open(stem + '.idx', 'rb') as f: head = f.read(HEAD)
    return _count(head, os.path.getsize(stem + '.idx')) is not None


def entries(path):
    """[(name, size, offset, stored size, zstd)] of the archive whose .dat is path; an entry without a name is #<id>"""
    stem = os.path.splitext(path)[0]
    with open(stem + '.idx', 'rb') as f: idx = f.read()
    with open(stem + '.str', 'rb') as f: names = f.read()
    count = _count(idx, len(idx))
    if count is None: raise ValueError(f'{stem}.idx: not a Liquid Engine index, or its size does not fit its entry count')
    if _count(names, None) != count: raise ValueError(f'{stem}.str: not the name table of a {count}-entry index')
    named, o = {}, HEAD
    while o < len(names):
        if o + 8 > len(names): raise ValueError(f'{stem}.str: {len(names) - o} bytes after the last name')
        i, n = struct.unpack_from('<II', names, o)
        if o + 8 + n > len(names): raise ValueError(f'{stem}.str: the name at {o:#x} runs past the end')
        named[i] = names[o + 8:o + 8 + n].decode('utf-8'); o += 8 + n
    end, out = os.path.getsize(path), []
    for k in range(count):
        i, stored, size, offset, z = RECORD.unpack_from(idx, HEAD + RECORD.size * k)
        name = named.get(i, f'#{i:08x}')
        if offset + stored > end: raise ValueError(f'{path}: {name}: {stored} bytes at {offset:#x} run past the end of the archive ({end} bytes)')
        out.append((name, size, offset, stored, z))
    return out


def read(f, entry, zstd):
    """An entry's bytes from the open .dat (entries checked its range); zstd: a module with ZstdDecompressor (shader_split.zstd_mod). A zstd entry decodes no further than one byte past
    the size the index gives (a size past what the decoder takes as a length is cut to 2^62), and must be one whole frame: every one of Frostpunk's 2,225 common.dat entries ends at its frame's end"""
    name, size, offset, stored, z = entry
    f.seek(offset); d = f.read(stored)
    if len(d) != stored: raise ValueError(f'{name}: {stored} bytes at {offset:#x} run past the end of the archive')
    if z:
        dec = zstd.ZstdDecompressor()
        try: d = dec.decompress(d, min(size, 1 << 62) + 1)
        except zstd.ZstdError as e: raise ValueError(f'{name}: {e}') from e
        if not dec.eof or dec.unused_data: raise ValueError(f'{name}: {"bytes after its frame" if dec.eof else "its zstd frame does not end within the entry, or decodes past the size the index gives"}')
    if len(d) != size: raise ValueError(f'{name}: {len(d)} bytes, the index says {size}')
    return d
