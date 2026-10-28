"""scripts/romfs_bc7_astc.py on synthetic RomFS images: Supergiant packages (Hades) with BC7 textures (one level, and odd sizes with mip levels), an RGBA8 texture and a
3D lookup table in LZ4 chunks, beside another file. The BC7 blocks become ASTC 4x4 blocks of the same picture and size, everything else stays byte for byte; a package it
cannot read through stops it with nothing written. Needs macOS and clang (Apple's texture encoder)."""
import importlib.util
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / 'scripts/romfs_bc7_astc.py'
sys.path.insert(0, str(REPO / 'dm/tools'))
import romfs   # noqa: E402
import romfs_image   # noqa: E402

COLOR = (201, 101, 51, 255)   # odd channels: BC7 mode 6 endpoints are 7 bits plus one shared p bit


def bc7_solid(rgba):   # one BC7 mode 6 block of one colour: both endpoints the colour, every index 0
    bits, pos = 1 << 6, 7
    for v in rgba:
        bits |= (v >> 1) << pos | (v >> 1) << (pos + 7); pos += 14
    return (bits | 1 << 63 | 1 << 64).to_bytes(16, 'little')


def bc7_texture(w, h, mips):   # (texture header, data): every level solid COLOR
    data = b''.join(bc7_solid(COLOR) * (((max(w >> i, 1) + 3) // 4) * ((max(h >> i, 1) + 3) // 4)) for i in range(mips))
    return struct.pack('<5I', 28, w, h, mips, len(data)), data


def entry(tag, name, header, data):   # tag, 1-byte name length, name, u32 big-endian XNB size, then the XNB (u32 little-endian size, the texture header, the data)
    size = 10 + len(header) + len(data)
    return bytes([tag, len(name)]) + name + struct.pack('>I', size) + b'XNBw\x06\x00' + struct.pack('<I', size) + header + data


def package(chunks):   # version 7 with the LZ4 flag; each chunk: flag 1, u32 big-endian compressed size, an LZ4 block
    import lz4.block
    out = struct.pack('>I', 0x20000007)
    for c in chunks:
        z = lz4.block.compress(c, store_size=False); out += b'\x01' + struct.pack('>I', len(z)) + z
    return out


def chunks(pkg):
    import lz4.block
    p, out = 4, []
    while p < len(pkg):
        size = struct.unpack('>I', pkg[p + 1:p + 5])[0]
        out.append(lz4.block.decompress(pkg[p + 5:p + 5 + size], uncompressed_size=0x2000000)); p += 5 + size
    return out


def script_module():
    spec = importlib.util.spec_from_file_location('romfs_bc7_astc', SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('clang') and importlib.util.find_spec('lz4'), 'needs macOS, clang and the lz4 package')
class Transcode(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp)
        head, self.bc7 = bc7_texture(8, 12, 1)
        self.rgba = entry(0xAD, b'bin\\Win\\720p\\Plain', struct.pack('<5I', 0, 4, 4, 1, 64), bytes(range(64)))
        self.lut = entry(0xAA, b'Lut', struct.pack('<5I', 0, 2, 2, 2, 32), bytes(32))
        self.tex = entry(0xAD, b'bin\\Win\\720p\\Atlas', head, self.bc7)
        self.first = self.rgba + self.lut + self.tex + b'\xbe'
        self.second = self.tex + b'\xff'
        self.inp, self.out = self.tmp / 'in.img', self.tmp / 'out.img'
        self.pack({'Content/Win/Packages/720p/Test.pkg': package([self.first, self.second])})

    def pack(self, pkgs):
        src = self.tmp / 'src'; shutil.rmtree(src, ignore_errors=True)
        for rel, data in pkgs.items(): (src / rel).parent.mkdir(parents=True, exist_ok=True); (src / rel).write_bytes(data)
        (src / 'Content/readme.txt').write_bytes(b'unchanged')
        romfs_image.pack(src, self.inp)

    def run_script(self, *args, ok=True, env=None):
        r = subprocess.run([sys.executable, str(SCRIPT), *args, str(self.inp), str(self.out)], capture_output=True, text=True, env=env)
        if ok: self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def assert_solid(self, astc, w, h):
        pixels = script_module().decode_astc(astc, w, h)
        for i in range(0, len(pixels), 4):
            self.assertTrue(all(abs(a - b) <= 2 for a, b in zip(pixels[i:i + 4], COLOR)), pixels[i:i + 4])

    def test_bc7_becomes_astc_of_the_same_picture(self):
        self.assertIn('2 BC7 textures', self.run_script().stdout)
        first, second = chunks(romfs.Path(f'{self.out}/Content/Win/Packages/720p/Test.pkg').read_bytes())
        self.assertEqual(len(first), len(self.first)); self.assertEqual(len(second), len(self.second))
        self.assertEqual(first[:len(self.rgba) + len(self.lut)], self.rgba + self.lut)   # the RGBA8 texture and the lookup table are not touched
        head = len(self.tex) - len(self.bc7)
        for c, at in ((first, len(self.rgba) + len(self.lut)), (second, 0)):
            self.assertEqual(c[at:at + head], self.tex[:head])   # name, sizes and texture header as they were
            astc = c[at + head:at + len(self.tex)]
            self.assertNotEqual(astc, self.bc7)
            self.assert_solid(astc, 8, 12)
        self.assertEqual(first[-1:], b'\xbe'); self.assertEqual(second[-1:], b'\xff')

    def test_odd_sizes_and_mip_levels(self):
        cases = [(10, 6, 4), (13, 5, 5), (3, 3, 2), (1, 1, 1)]
        tex = [entry(0xAD, b'T%d' % i, *bc7_texture(w, h, m)) for i, (w, h, m) in enumerate(cases)]
        self.pack({'Content/Win/Packages/720p/Odd.pkg': package([b''.join(tex) + b'\xff'])})
        self.assertIn('4 BC7 textures', self.run_script().stdout)
        (c,) = chunks(romfs.Path(f'{self.out}/Content/Win/Packages/720p/Odd.pkg').read_bytes())
        at = 0
        for t, (w, h, mips) in zip(tex, cases):
            off = at + 2 + t[1] + 4 + 30   # tag, name length, name, entry size, XNB header, texture header
            for i in range(mips):
                lw, lh = max(w >> i, 1), max(h >> i, 1); n = ((lw + 3) // 4) * ((lh + 3) // 4) * 16
                self.assert_solid(c[off:off + n], lw, lh); off += n
            at += len(t)

    def test_unreadable_package_writes_nothing_unless_allowed(self):
        odd = bytes([0xDE, 3]) + b'Odd' + struct.pack('>I', 4) + bytes(4)   # an entry kind it does not know, before a BC7 texture it would then not see
        self.pack({'Content/Win/Packages/720p/Test.pkg': package([self.tex + b'\xbe']), 'Content/Win/Packages/1080p/Test.pkg': package([odd + self.tex + b'\xff'])})
        r = self.run_script(ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('/Content/Win/Packages/1080p/Test.pkg: tag 0xde', r.stdout)   # the package named by its whole path
        self.assertFalse(self.out.exists())
        self.assertIn('1 BC7 textures', self.run_script('--allow-partial').stdout)

    def test_cold_helper_cache_with_many_packages(self):
        tmpdir = self.tmp / 'cold'; tmpdir.mkdir()   # a temp folder without the compiled helper: every worker would compile it at once
        self.pack({f'Content/Win/Packages/720p/P{i}.pkg': package([self.tex + b'\xff']) for i in range(12)})
        self.assertIn('12 BC7 textures', self.run_script(env=dict(os.environ, TMPDIR=str(tmpdir))).stdout)

    def test_other_files_and_index_unchanged(self):
        self.run_script()
        self.assertEqual(romfs.Path(f'{self.out}/Content/readme.txt').read_bytes(), b'unchanged')
        self.assertEqual(sorted(romfs_image.read_index(self.inp)), sorted(romfs_image.read_index(self.out)))

    def test_refuses_to_overwrite_its_input(self):
        r = subprocess.run([sys.executable, str(SCRIPT), str(self.inp), str(self.inp)], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


if __name__ == '__main__':
    unittest.main()
