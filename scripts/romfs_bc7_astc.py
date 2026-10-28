#!/usr/bin/env python3
# Transcode the BC7 textures in Supergiant's packages (Hades: Content/**/*.pkg) of a RomFS image (dm/tools/romfs_image.py) to ASTC 4x4, for GPUs without BC (A16 and
# earlier iPhones), which cannot create a BC7 texture. Both formats store a 4x4 texel block in 16 bytes, so every texture keeps its size, layout and header; only the bytes
# of its blocks change. The host then creates the game's BC7 images as ASTC 4x4 textures: a build with the new image sets DM_VK_BC7_ASTC=1 (hle_vk.m fmt_init), through
# build_ipa_from_nsp.py --env-file. Textures cost the same memory as BC7, and the image is used as it is on any GPU that samples ASTC (every Apple GPU).
# Package (version 7 with the LZ4 flag: big-endian 0x20000007): chunks of flag 1, u32 big-endian size and an LZ4 block of at most 32 MB, to the end of the file. A chunk
# holds entries: a tag (0xAD texture, 0xAA 3D texture), a name (7-bit encoded length), a u32 big-endian size and an XNB of that size ("XNB", platform, version 6, flags 0,
# u32 little-endian size; a texture's u32 format, width, height, mip levels and data size, then the data); 0xBE ends a chunk, 0xFF the last one. Format 28 is BC7.
# BC7 decoding and ASTC encoding are Apple's texture encoder (libate, AppleTextureEncoder.h in the macOS SDK), compiled on first use into a small library in the temp folder.
# Anything it cannot read (an unknown entry, a malformed package) may hide BC7 textures, which the switch would then draw as ASTC: it reports them and writes nothing (exit 1)
# unless --allow-partial.
# Usage: romfs_bc7_astc.py [--allow-partial] <RomFS.img> <out image>   (written to <out>.part, renamed at the end; the input is never changed)
# An iOS build from it: a second NSP folder for the game (the NSPs hard-linked), its <id>_build/extract/<version>/ holding the original's ExeFS, control.nacp and icon.jpg and
# this script's output as RomFS.img (the NSP script reuses an extraction it finds), then build_ipa_from_nsp.py on that folder with --env-file naming DM_VK_BC7_ASTC=1.
# The original folder keeps the BC7 image for GPUs with BC.
import argparse, concurrent.futures, ctypes, hashlib, os, struct, subprocess, sys, tempfile, threading
from pathlib import Path

import lz4.block

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dm/tools'))
import romfs   # noqa: E402
from romfs_image import ENT, HEAD, MAGIC   # noqa: E402

BC7 = 28
CHUNK = 0x2000000      # the largest decompressed chunk
QUALITY = 2 ** -14     # the encoder stops trying encodings for a block below this mean square error (2^-10 fast to 2^-15 best)

HELPER = r'''
#include <AppleTextureEncoder.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
static at_encoder_t g_bc7, g_astc;
static pthread_once_t g_once = PTHREAD_ONCE_INIT;
static void init(void) {
    g_bc7 = at_encoder_create(at_texel_format_rgba8_unorm, at_alpha_not_premultiplied, at_block_format_bc7, at_alpha_not_premultiplied, NULL);
    g_astc = at_encoder_create(at_texel_format_rgba8_unorm, at_alpha_not_premultiplied, at_block_format_astc_4x4_ldr, at_alpha_not_premultiplied, NULL);
}
// src: ceil(w/4) * ceil(h/4) blocks of 16 bytes; to_astc: dst the same number of ASTC 4x4 blocks, else dst the RGBA8 texels of the block-padded size decoded from ASTC.
// Returns >= 0 on success, a negative at_error_t, or -100 without an encoder / -101 out of memory
float transcode(const void *src, void *dst, unsigned w, unsigned h, int to_astc, float quality) {
    pthread_once(&g_once, init);
    if (!g_bc7 || !g_astc) return -100;
    unsigned bw = (w + 3) / 4, bh = (h + 3) / 4;
    size_t blocks = (size_t)bw * bh * 16, texels = (size_t)bw * bh * 64;
    void *in = aligned_alloc(16, blocks), *out = aligned_alloc(16, blocks), *rgba = aligned_alloc(16, texels);   // the encoder wants 16-byte aligned blocks
    if (!in || !out || !rgba) { free(in); free(out); free(rgba); return -101; }
    memcpy(in, src, blocks);
    at_block_buffer_t from = { in, (size_t)bw * 16, blocks }, to = { out, (size_t)bw * 16, blocks };
    at_texel_region_t region = { rgba, { bw * 4, bh * 4, 1 }, (size_t)bw * 16, texels };
    float err = at_encoder_decompress_texels(to_astc ? g_bc7 : g_astc, &from, &region, at_flags_default);
    if (err >= 0 && to_astc) err = at_encoder_compress_texels(g_astc, &region, &to, quality, at_flags_skip_error_calculation);
    if (err >= 0) memcpy(dst, to_astc ? out : rgba, to_astc ? blocks : texels);
    free(in); free(out); free(rgba);
    return err;
}
'''


_lib_lock = threading.Lock()


def lib():   # the helper, compiled once per source: under a lock in this process, through unique temporary files and an atomic rename across processes
    with _lib_lock:
        if getattr(lib, 'cached', None) is None:
            out = Path(tempfile.gettempdir()) / f'dm_bc7_astc_{hashlib.sha256(HELPER.encode()).hexdigest()[:12]}.dylib'
            if not out.exists():
                fd, src = tempfile.mkstemp(suffix='.c', dir=out.parent); os.write(fd, HELPER.encode()); os.close(fd)
                fd, part = tempfile.mkstemp(suffix='.dylib', dir=out.parent); os.close(fd)
                try:
                    subprocess.run(['clang', '-O2', '-shared', '-o', part, src, '-late'], check=True); os.replace(part, out)
                finally:
                    for p in (src, part):
                        if os.path.exists(p): os.unlink(p)
            L = ctypes.CDLL(str(out)); L.transcode.restype = ctypes.c_float
            L.transcode.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_float]
            lib.cached = L
        return lib.cached


def blocks(w, h): return ((w + 3) // 4) * ((h + 3) // 4) * 16


def run(src, w, h, to_astc):
    out = ctypes.create_string_buffer(blocks(w, h) if to_astc else blocks(w, h) * 4)
    err = lib().transcode(src, out, w, h, int(to_astc), QUALITY)
    if err < 0: raise RuntimeError(f'Apple texture encoder error {err:g} ({w}x{h})')
    return out.raw


def encode_astc(bc7, w, h): return run(bc7, w, h, True)
def decode_astc(astc, w, h): return run(astc, w, h, False)   # RGBA8 texels of the block-padded size, for checks


def levels(w, h, mips):   # (width, height, offset, size) of each mip level of a block texture
    out, off = [], 0
    for i in range(max(mips, 1)):
        lw, lh = max(w >> i, 1), max(h >> i, 1); out.append((lw, lh, off, blocks(lw, lh))); off += blocks(lw, lh)
    return out


def chunk(d, notes):   # (the chunk with its BC7 textures transcoded or None when it has none, their count)
    out, n, q = None, 0, 0
    while q < len(d) and d[q] not in (0xBE, 0xFF):
        tag, p, ln, shift = d[q], q + 1, 0, 0
        if tag not in (0xAD, 0xAA): notes.append(f'tag {tag:#x} at {q:#x}: rest of the chunk left as it is'); break
        while True:
            b = d[p]; p += 1; ln |= (b & 0x7f) << shift; shift += 7
            if not b & 0x80: break
        x = p + ln + 4; size = struct.unpack_from('>I', d, x - 4)[0]
        if d[x:x + 3] != b'XNB' or d[x + 4] != 6 or d[x + 5] or struct.unpack_from('<I', d, x + 6)[0] != size or x + size > len(d):
            notes.append(f'entry at {q:#x} is not a plain XNB: rest of the chunk left as it is'); break
        if tag == 0xAD:
            fmt, w, h, mips, ds = struct.unpack_from('<5I', d, x + 10)
            lv = levels(w, h, mips) if fmt == BC7 and mips <= 16 else []
            if fmt == BC7 and (sum(s for *_, s in lv) != ds or 30 + ds > size): notes.append(f'BC7 texture at {q:#x}: {w}x{h} with {mips} levels is not {ds} bytes, left as it is')
            elif fmt == BC7:
                if out is None: out = bytearray(d)
                for lw, lh, off, s in lv:
                    at = x + 30 + off; out[at:at + s] = encode_astc(bytes(d[at:at + s]), lw, lh)
                n += 1
        q = x + size
    return out, n


def package(data, notes):   # (the package with its BC7 textures transcoded, their count)
    if len(data) < 4 or struct.unpack_from('>I', data)[0] != 0x20000007: notes.append('not a version 7 LZ4 package: left as it is'); return data, 0
    out, p, n = [data[:4]], 4, 0
    while p < len(data):
        if data[p] != 1: notes.append(f'chunk flag {data[p]} at {p:#x}: rest left as it is'); out.append(data[p:]); break
        size = struct.unpack_from('>I', data, p + 1)[0]; z = data[p + 5:p + 5 + size]
        new, k = chunk(lz4.block.decompress(z, uncompressed_size=CHUNK), notes)
        if new is not None:
            z = lz4.block.compress(bytes(new), mode='high_compression', compression=9, store_size=False); n += k
        out.append(b'\x01' + struct.pack('>I', len(z)) + z); p += 5 + size
    return b''.join(out), n


def paths(img):   # {file entry: its path in the image}
    out, todo = {}, [(0, '')]
    while todo:
        k, p = todo.pop()
        for name, j in img.children(k):
            if img.entry(j)[4]: out[j] = f'{p}/{name}'
            else: todo.append((j, f'{p}/{name}'))
    return out


def main():
    ap = argparse.ArgumentParser(description='Transcode the BC7 textures of the Supergiant packages in a RomFS image to ASTC 4x4')
    ap.add_argument('--allow-partial', action='store_true', help='write the image even when a package could not be read through (its BC7 textures there stay BC7)')
    ap.add_argument('image'); ap.add_argument('out')
    a = ap.parse_args()
    src, dst = Path(a.image).resolve(), Path(a.out).resolve()
    if src == dst: sys.exit('romfs_bc7_astc.py: the output must be another file than the input (the input is the BC7 original)')
    img = romfs.Image(src)
    ents = [img.entry(k) for k in range(img.count)]
    files = sorted((e[0], k) for k, e in enumerate(ents) if e[4])   # by data offset, the order they are written in again
    pkgs = [k for _, k in files if img.name(k).endswith(b'.pkg')]
    where = paths(img)

    def one(k):
        notes = []
        with open(src, 'rb') as f: f.seek(ents[k][0]); data = f.read(ents[k][1])
        try: new, n = package(data, notes)
        except (IndexError, ValueError, struct.error, lz4.block.LZ4BlockError) as e: notes.append(f'malformed ({e}): left as it is'); new, n = data, 0
        return k, new, n, notes
    lib()   # compiled before the workers start
    new, total, partial = {}, 0, 0
    with concurrent.futures.ThreadPoolExecutor(os.cpu_count()) as ex:   # the encoder and LZ4 run without the GIL
        for k, data, n, notes in ex.map(one, pkgs):
            if n: new[k] = data; total += n
            for s in notes: print(f'{where[k]}: {s}')
            partial += len(notes)
    if partial and not a.allow_partial:
        sys.exit(f'romfs_bc7_astc.py: {partial} places not read through, which may hold BC7 textures that DM_VK_BC7_ASTC=1 would draw as ASTC; nothing written '
                 '(--allow-partial writes the image anyway)')
    part = dst.with_name(dst.name + '.part'); index = bytearray(img.index); off = HEAD.size
    with open(src, 'rb') as f, open(part, 'wb') as o:
        o.write(bytes(HEAD.size))
        for a0, k in files:
            _, b, no, nl, t, pad = ents[k]
            if k in new: o.write(new[k]); b = len(new[k])
            else:
                f.seek(a0); left = b
                while left:
                    piece = f.read(min(left, 1 << 23))
                    if not piece: raise OSError(f'{src}: ends inside {where[k]}')
                    o.write(piece); left -= len(piece)
            ENT.pack_into(index, k * ENT.size, off, b, no, nl, t, pad); off += b
        o.write(index); o.seek(0); o.write(HEAD.pack(MAGIC, off, len(index), img.count))
    romfs.Image(part)   # a valid image before it replaces anything
    os.replace(part, dst)
    print(f'{total} BC7 textures in {len(new)} of {len(pkgs)} packages transcoded to ASTC 4x4: {dst} (set DM_VK_BC7_ASTC=1 in the build that uses it)')


if __name__ == '__main__':
    main()
