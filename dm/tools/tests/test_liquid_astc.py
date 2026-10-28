"""liquid_astc.py's archive rewrite (Frostpunk's textures-s3.dat): which entries are converted, and the index and data it writes, with a stand-in encoder (no TextureConverter)."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'gfx/tools'))
import liquid
import liquid_astc as la

ZSTD = la.zstd_mod()


def texture(fmt, blocks):   # a .texture entry: 28-byte header, then the storage
    data = bytes(range(16)) * blocks
    return b'ex\x1c\0' + struct.pack('<IIHHBBBBBBBBI', fmt, 1, 4 * blocks, 4, 1, 0, 1, 1, 2, 3, 4, 5, len(data)) + data


def archive(d, members):   # <d>/textures-s3.{idx,str,dat}; members: [(id, name, bytes, zstd)]
    stem = Path(d) / la.ARCHIVE
    head = b'\0\5\2' + struct.pack('<II', len(members), 0)
    idx, names, dat = bytearray(head), bytearray(head), bytearray()
    for i, name, data, z in members:
        stored = ZSTD.compress(data) if z else data
        idx += struct.pack('<IQQQB', i, len(stored), len(data), len(dat), z); dat += stored
        names += struct.pack('<II', i, len(name)) + name.encode()
    stem.with_suffix('.idx').write_bytes(idx); stem.with_suffix('.str').write_bytes(names); stem.with_suffix('.dat').write_bytes(dat)


class LiquidAstc(unittest.TestCase):
    def test_convertible(self):
        self.assertEqual(la.convertible(texture(0x48, 2)), 0x48)
        self.assertEqual(la.convertible(texture(0x44, 1)), 0x44)
        self.assertIsNone(la.convertible(texture(0x01, 2)))   # R8: left alone
        self.assertIsNone(la.convertible(texture(0x48, 2)[:-1]))   # the header's size does not match
        self.assertIsNone(la.convertible(b'<TextureArrayDescriptor>'))

    def test_rewrite_keeps_sizes_and_moves_offsets(self):
        members = [(1, 'a.texture', texture(0x48, 3), 1), (2, 'cursor.tga', b'tga' * 10, 0), (3, 'b.texture', texture(0x01, 2), 1), (4, 'c.texture', texture(0x44, 1), 1)]
        encoded = []
        orig = la.bc3_astc
        la.bc3_astc = lambda blocks, tc, cache: (encoded.append(len(blocks)), bytes(b ^ 0xff for b in blocks))[1]   # stands in for TextureConverter: same size, other bytes
        try:
            with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
                archive(src, members)
                n, kept = la.convert_archive(src, Path(out), None, None, 2, log=lambda *_: None)
                self.assertEqual((n, kept), (2, 2))
                self.assertEqual(sorted(encoded), [16, 48])
                dat = Path(out) / f'{la.ARCHIVE}.dat'
                ents = liquid.entries(str(dat))
                self.assertEqual([e[0] for e in ents], ['a.texture', 'cursor.tga', 'b.texture', 'c.texture'])
                with open(dat, 'rb') as f:
                    got = {e[0]: liquid.read(f, e, ZSTD) for e in ents}
                self.assertEqual(got['a.texture'][:28], texture(0x48, 3)[:28])
                self.assertEqual(got['a.texture'][28:], bytes(b ^ 0xff for b in texture(0x48, 3)[28:]))
                self.assertEqual(got['cursor.tga'], b'tga' * 10)
                self.assertEqual(got['b.texture'], texture(0x01, 2))
                self.assertEqual(sum(e[3] for e in ents), dat.stat().st_size)   # the entries tile the new archive
        finally:
            la.bc3_astc = orig


if __name__ == '__main__': unittest.main()
