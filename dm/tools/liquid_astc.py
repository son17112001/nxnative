#!/usr/bin/env python3
# Frostpunk's textures (Liquid Engine: textures-s3.dat beside its .idx and .str, dm/gfx/tools/liquid.py) with their BC3 (DXT5) blocks re-encoded as ASTC 4x4, for GPUs that cannot sample BC
# (supportsBCTextureCompression NO: the A16 and older iPhones). A .texture entry is a 28-byte header ("ex" 1c 00, u32 NVN format, u32 target, u16 width, u16 height, u8 depth, 0, layers,
# levels, a swizzle byte per component, u32 data size) and the texture's storage as the game hands it to NVN: block-linear, every level, 16 bytes per 4x4 block. BC3 and ASTC 4x4 blocks are
# both 16 bytes and both stand alone, so each block is re-encoded where it lies: sizes, offsets and block-linear addresses stay as they are, and the host only creates the textures as ASTC 4x4
# (DM_NVN_BC3_ASTC=1, nvn_fmt_mtl.c). Formats 0x44 (DXT5) and 0x48 (DXT5 sRGB) are converted (3,402 of Frostpunk Demo's 3,585 textures); the rest (R8, RGBA8, R16F) are left alone.
# Encoder: Xcode's TextureConverter. The blocks are laid out as one image, 1,024 blocks a row, decoded from BC3 and encoded as ASTC 4x4 (ARM's astcenc, quality Normal, alpha kept);
# the decode and encode treat the values as stored, so an sRGB texture stays sRGB through its view. Results are cached by the data's hash and the encoder settings.
# Usage: liquid_astc.py [--jobs N] [--cache DIR] <RomFS image> <out image>   (a copy of the image with textures-s3.dat / .idx rewritten)
import argparse, concurrent.futures, hashlib, os, shutil, struct, subprocess, sys, tempfile, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent)); sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gfx/tools'))
import romfs, romfs_image, liquid

ARCHIVE = 'textures-s3'
DXT5 = {0x44, 0x48}
HEAD = 28
ROW = 1024   # blocks per row of the image handed to the encoder
GL_DXT5, GL_ASTC_4x4, GL_RGBA = 0x83F3, 0x93B0, 0x1908
ENCODE = ['--compression_format=ASTC4x4', '--compressor=ARM', '--compression_quality=Normal', '--max_mipmaps=1', '--alpha_mode=Preserve']


def zstd_mod():
    try: from compression import zstd
    except ImportError: from backports import zstd
    return zstd


def run(cmd):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode: raise RuntimeError(f'{Path(str(cmd[0])).name} exit code {r.returncode}: {(r.stderr or r.stdout).strip()[-400:]}')


def texture_converter():   # Xcode's TextureConverter, or None
    r = subprocess.run(['xcrun', '--find', 'TextureConverter'], capture_output=True, text=True)
    p = Path(r.stdout.strip()) if r.returncode == 0 else None
    return p if p and p.is_file() else None


def ktx(blocks, w, h, gl_format):   # a one-level KTX 1 file
    return (b'\xabKTX 11\xbb\r\n\x1a\n' + struct.pack('<13I', 0x04030201, 0, 1, 0, gl_format, GL_RGBA, w, h, 0, 0, 1, 1, 0)
            + struct.pack('<I', len(blocks)) + blocks)


def ktx_level(path):   # (glInternalFormat, level 0's bytes, width, height) of a KTX 1 file
    b = Path(path).read_bytes()
    if b[:12] != b'\xabKTX 11\xbb\r\n\x1a\n': raise ValueError(f'not a KTX 1 file: {path}')
    gl_format, = struct.unpack_from('<I', b, 28); w, h = struct.unpack_from('<II', b, 36); kv, = struct.unpack_from('<I', b, 60)
    n, = struct.unpack_from('<I', b, 64 + kv)
    return gl_format, b[68 + kv:68 + kv + n], w, h


def convertible(d):   # the NVN format of a .texture entry whose data are BC3 blocks, else None
    if len(d) < HEAD or d[:3] != b'ex\x1c': return None
    fmt, = struct.unpack_from('<I', d, 4); size, = struct.unpack_from('<I', d, 24)
    return fmt if fmt in DXT5 and size == len(d) - HEAD and size % 16 == 0 else None


def bc3_astc(blocks, tc, cache):   # BC3 blocks (any order: each stands alone) as ASTC 4x4 blocks in the same order
    key = hashlib.sha256(blocks + ' '.join(ENCODE).encode()).hexdigest()
    hit = Path(cache) / key[:2] / key
    if hit.exists(): return hit.read_bytes()
    n = len(blocks) // 16; bw = min(n, ROW); bh = -(-n // bw)
    with tempfile.TemporaryDirectory(prefix='liquid_astc.') as tmp:
        tmp = Path(tmp)
        (tmp / 'bc3.ktx').write_bytes(ktx(blocks + bytes(16 * (bw * bh - n)), 4 * bw, 4 * bh, GL_DXT5))
        run([tc, '--mode=decompress', '--file_format=KTX', f'--output={tmp / "rgba.ktx"}', tmp / 'bc3.ktx'])
        run([tc, *ENCODE, '--file_format=KTX', '--disable_annotation', f'--output={tmp / "astc.ktx"}', tmp / 'rgba.ktx'])
        fmt, astc, aw, ah = ktx_level(tmp / 'astc.ktx')
    if (fmt, aw, ah, len(astc)) != (GL_ASTC_4x4, 4 * bw, 4 * bh, 16 * bw * bh):
        raise ValueError(f'TextureConverter gave format {fmt:#x} {aw}x{ah}, {len(astc)} bytes for {n} BC3 blocks')
    astc = astc[:16 * n]
    hit.parent.mkdir(parents=True, exist_ok=True)
    part = hit.with_name(hit.name + f'.{os.getpid()}.{threading.get_ident()}.part')
    part.write_bytes(astc); os.replace(part, hit)
    return astc


def convert_archive(img, out_dir, tc, cache, jobs, log=print):
    """textures-s3.dat / .idx / .str of the RomFS image img, rewritten into out_dir with the BC3 textures' blocks as ASTC 4x4; returns (converted, kept) entry counts"""
    zstd = zstd_mod(); dat = f'{img}/{ARCHIVE}.dat'
    ents = liquid.entries(dat)
    with romfs.open(f'{img}/{ARCHIVE}.idx', 'rb') as f: idx = bytearray(f.read())
    with romfs.open(dat, 'rb') as f: raw = [(f.seek(e[2]), f.read(e[3]))[1] for e in ents]   # stored bytes, index order

    def one(k):
        e = ents[k]
        d = zstd.decompress(raw[k]) if e[4] else raw[k]
        if convertible(d) is None: return k, None
        astc = bc3_astc(d[HEAD:], tc, cache)
        new = d[:HEAD] + astc
        return k, zstd.compress(new) if e[4] else new

    done = {}
    with concurrent.futures.ThreadPoolExecutor(jobs) as ex:
        for i, (k, stored) in enumerate(ex.map(one, range(len(ents)))):
            done[k] = stored
            if (i + 1) % 250 == 0: log(f'{i + 1} / {len(ents)} entries')
    out_dir.mkdir(parents=True, exist_ok=True)
    off = 0
    with open(out_dir / f'{ARCHIVE}.dat', 'wb') as o:
        for k, e in enumerate(ents):   # in index order; each record gets its entry's new offset and stored size (the size it decodes to is unchanged)
            stored = done[k] if done[k] is not None else raw[k]
            o.write(stored)
            i_, _, size, _, z = liquid.RECORD.unpack_from(idx, liquid.HEAD + liquid.RECORD.size * k)
            liquid.RECORD.pack_into(idx, liquid.HEAD + liquid.RECORD.size * k, i_, len(stored), size, off, z)
            off += len(stored)
    (out_dir / f'{ARCHIVE}.idx').write_bytes(idx)
    with romfs.open(f'{img}/{ARCHIVE}.str', 'rb') as f: (out_dir / f'{ARCHIVE}.str').write_bytes(f.read())
    n = sum(v is not None for v in done.values())
    return n, len(ents) - n


def main(argv=None):
    ap = argparse.ArgumentParser(description='re-encode the BC3 textures of a Liquid Engine RomFS (Frostpunk) as ASTC 4x4')
    ap.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2) // 2), help='textures encoded at once (TextureConverter also uses threads)')
    ap.add_argument('--cache', type=Path, help='encoded blocks are kept here by hash (default: <out image dir>/cache)')
    ap.add_argument('image', type=Path); ap.add_argument('out', type=Path)
    a = ap.parse_args(argv)
    tc = texture_converter()
    if not tc: sys.exit('[RED] Xcode\'s TextureConverter not found (xcrun --find TextureConverter): install Xcode and select it with xcode-select -s')
    if not romfs.exists(f'{a.image}/{ARCHIVE}.dat') or not liquid.is_archive(f'{a.image}/{ARCHIVE}.dat'): sys.exit(f'[RED] {a.image} has no Liquid Engine {ARCHIVE}.dat archive')
    cache = a.cache or a.out.parent / 'cache'
    with tempfile.TemporaryDirectory(prefix='liquid_astc.', dir=a.out.parent) as tmp:
        tree = Path(tmp) / 'RomFS'
        n, kept = convert_archive(a.image, tree, tc, cache, a.jobs)
        for root, _, files in romfs.walk(str(a.image)):   # every other file of the image as it is
            rel = Path(root[len(str(a.image)):].lstrip('/'))
            for name in files:
                dst = tree / rel / name
                if dst.exists(): continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                with romfs.open(f'{root}/{name}', 'rb') as src, open(dst, 'wb') as o: shutil.copyfileobj(src, o, 1 << 22)
        romfs_image.pack(tree, a.out)
    print(f'liquid_astc: {n} BC3 textures re-encoded as ASTC 4x4, {kept} entries kept, {a.out}')


if __name__ == '__main__': main()
