"""Liquid Engine .idx/.str/.dat archives and the NVN programs in their shader entries (Frostpunk), without game files."""
from pathlib import Path
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
os.environ.setdefault('DM_GAME', '010015100B514000')
import shader_split as split
import liquid

ZSTD = split.zstd_mod()


def shader(stage):
    """A shader entry as Frostpunk's common.dat stores it: 36-byte header {magic 0xcdbe5660, version 4, two u64, u32, code length, control length}, code area, control section, trailing data"""
    code = bytearray(0x100); code[:4] = split.CS_MAGIC if stage == 'cs' else split.GFX_MAGIC
    if stage != 'cs': struct.pack_into('<I', code, 0x30, {'vs': 1, 'ps': 5}[stage] << 10)
    if stage == 'ps': struct.pack_into('<I', code, 0x30 + 4 * 6, 0xff)   # reads generic attribute 0 (ps_inputs), so the pixel-input cover has a member
    ctl = bytearray(0x880); ctl[:4] = split.CTL_MAGIC
    struct.pack_into('<I', ctl, 0x20, len(ctl)); struct.pack_into('<I', ctl, 0x704, len(code))
    if stage == 'cs': struct.pack_into('<3I', ctl, 0x748, 8, 8, 1)
    head = struct.pack('<IIQQIII', 0xcdbe5660, 4, 1, 2, len(code) + len(ctl) + 8, len(code), len(ctl))
    return head + bytes(code) + bytes(ctl) + b'reflection'


def archive(tmp, members, header=b'\0\5\2'):
    """<tmp>/common.{idx,str,dat}; members: [(id, name or None, bytes, zstd)]"""
    stem = Path(tmp) / 'common'
    idx, names, dat = bytearray(header + struct.pack('<II', len(members), 0)), bytearray(header + struct.pack('<II', len(members), 0)), bytearray()
    for i, name, data, z in members:
        stored = ZSTD.compress(data) if z else data
        idx += struct.pack('<IQQQB', i, len(stored), len(data), len(dat), z); dat += stored
        if name is not None: names += struct.pack('<II', i, len(name)) + name.encode()
    stem.with_suffix('.idx').write_bytes(idx); stem.with_suffix('.str').write_bytes(names); stem.with_suffix('.dat').write_bytes(dat)
    return stem.with_suffix('.dat')


class Liquid(unittest.TestCase):
    def test_entries_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            dat = archive(tmp, [(7, 'shaders/compiled/a/0.vs_5_0', shader('vs'), 1), (9, None, b'plain', 0)])
            self.assertTrue(liquid.is_archive(str(dat)))
            entries = liquid.entries(str(dat))
            self.assertEqual([e[0] for e in entries], ['shaders/compiled/a/0.vs_5_0', '#00000009'])
            with open(dat, 'rb') as f:
                self.assertEqual(liquid.read(f, entries[0], ZSTD), shader('vs'))
                self.assertEqual(liquid.read(f, entries[1], ZSTD), b'plain')

    def test_not_an_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            lone = Path(tmp) / 'sounds.dat'; lone.write_bytes(b'OggS')
            self.assertFalse(liquid.is_archive(str(lone)))
            dat = archive(tmp, [(1, 'x.bin', b'x', 0)], header=b'\0\5\3')
            self.assertFalse(liquid.is_archive(str(dat)))
            dat.with_suffix('.idx').write_bytes(b'\0\5\2' + struct.pack('<II', 2, 0) + bytes(29))
            self.assertFalse(liquid.is_archive(str(dat)))
            with self.assertRaises(ValueError): liquid.entries(str(dat))

    def test_split_reads_shader_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            dat = archive(tmp, [(1, 'shaders/compiled/a/0.vs_5_0', shader('vs'), 1), (2, 'shaders/compiled/a/0.ps_5_0', shader('ps'), 1),
                                (3, 'shaders/compiled/b/0.cs_5_0', shader('cs'), 1), (4, 'shaders/shaders.defbin', b'not a shader', 1),
                                (5, 'textures/a.texture', b'\0' * 64, 0)])
            rel, keys, pairs, _ = split.scan_file((str(dat), 'common.dat'))
            self.assertEqual(rel, 'common.dat')
            self.assertEqual(sorted(v[1] for v in keys.values()), ['cs', 'ps', 'vs'])
            self.assertTrue(all('#LIQUID@' in v[0] for v in keys.values()))
            self.assertEqual(pairs, set())   # single-stage programs: separable_pairs pads the vertex shaders
            cover_pairs, _ = split.separable_pairs(keys)
            self.assertEqual(len(cover_pairs), 1)

    def test_inside_a_romfs_image(self):
        import romfs_image
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'RomFS'; src.mkdir()
            archive(src, [(1, 'shaders/compiled/a/0.vs_5_0', shader('vs'), 1), (2, 'shaders/compiled/a/0.ps_5_0', shader('ps'), 1)])
            img = Path(tmp) / 'RomFS.img'; romfs_image.pack(src, img)
            dat = f'{img}/common.dat'
            self.assertTrue(liquid.is_archive(dat))
            self.assertEqual(sorted(v[1] for v in split.scan_file((dat, 'common.dat'))[1].values()), ['ps', 'vs'])

    def test_damaged_archives_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            dat = archive(tmp, [(1, 'a.vs_5_0', shader('vs'), 1)])
            names = dat.with_suffix('.str'); good = names.read_bytes()
            names.write_bytes(b'\0\5\3' + good[3:])   # another header
            with self.assertRaises(ValueError): liquid.entries(str(dat))
            names.write_bytes(good + b'\1\2')   # a name record cut short
            with self.assertRaises(ValueError): liquid.entries(str(dat))
            names.write_bytes(good)
            idx = dat.with_suffix('.idx'); rec = bytearray(idx.read_bytes())
            struct.pack_into('<Q', rec, 11 + 4, 1 << 62); idx.write_bytes(rec)   # stored size past the end of the .dat
            with self.assertRaises(ValueError): liquid.entries(str(dat))
            struct.pack_into('<Q', rec, 11 + 4, dat.stat().st_size); struct.pack_into('<Q', rec, 11 + 12, 999); idx.write_bytes(rec)   # size the index gets wrong
            with open(dat, 'rb') as f, self.assertRaises(ValueError): liquid.read(f, liquid.entries(str(dat))[0], ZSTD)
            dat.write_bytes(b'\x28\xb5\x2f\xfd' + bytes(dat.stat().st_size - 4))   # not a zstd frame after its magic
            struct.pack_into('<Q', rec, 11 + 12, len(shader('vs'))); idx.write_bytes(rec)
            with open(dat, 'rb') as f, self.assertRaises(ValueError) as e: liquid.read(f, liquid.entries(str(dat))[0], ZSTD)
            self.assertIn('a.vs_5_0', str(e.exception))

    def test_entry_sizes_bound_the_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = ZSTD.compress(shader('ps'))
            dat = archive(tmp, [(1, 'a.ps_5_0', shader('ps'), 1)])
            e = liquid.entries(str(dat))[0]
            for size in ((1 << 63) - 1, 1 << 64 - 1):   # past what a decoder takes as a length
                with open(dat, 'rb') as f, self.assertRaises(ValueError) as r: liquid.read(f, (e[0], size) + e[2:], ZSTD)
                self.assertIn('a.ps_5_0', str(r.exception))
            dat.write_bytes(frame + frame)   # a second frame after the entry's own
            with open(dat, 'rb') as f, self.assertRaises(ValueError) as r: liquid.read(f, e[:3] + (2 * len(frame),) + e[4:], ZSTD)
            self.assertIn('after its frame', str(r.exception))
            dat.write_bytes(frame[:-4])   # cut short
            with open(dat, 'rb') as f, self.assertRaises(ValueError): liquid.read(f, e[:3] + (len(frame) - 4,) + e[4:], ZSTD)

    def test_bad_shader_entry_is_reported(self):
        bad = bytearray(shader('ps')); struct.pack_into('<I', bad, 36 + 0x100 + 0x704, 0x104)
        with self.assertRaises(SystemExit): split.liquid_shader(bytes(bad), 'bad.ps_5_0')
        with self.assertRaises(SystemExit): split.liquid_shader(b'\0' * 64, 'short.ps_5_0')


if __name__ == '__main__': unittest.main()
