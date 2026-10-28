# Frostpunk Demo (`0100193028708000`)

Base game v0 (display version 1.0.0), SDK 20.5.19. 11 bit studios' Liquid Engine on **NVN**. The RomFS root holds `.idx` / `.str` / `.dat` archives (`common`, `templates`, `textures-s3`, `sounds`, `videos`, ...); the ExeFS only `main` and `sdk`. Plays on the Mac from the title through the opening cinematic into the city on day 1 (the Generator, the people and the buildings drawn), about 29 fps against the game's 30 fps cap. The iPhone build is untested.

## What it needs

| Setting | Where | Why |
|---|---|---|
| `shader_global_window on` | [`game.tsv`](game.tsv) | its mesh shaders (857 of 2,141: `meshsolid`, `meshtranslucent`) read a pointer per instance from uniform slot 13 (`c16`) at 8 × (instance − base instance) and their instance data through it; see [Rendering](#rendering) |
| 4 GB layout: `mem_reserve 0x100000000`, `mem_alias 0x10000000`, `mem_stack 0x10000000`, `app_pool 0xCD500000`, `mem_used 0x5c00000` | `game.tsv` | the provisional profile's guesses; not measured for this game |

## Files and shaders

- The archives are read by `dm/gfx/tools/liquid.py` ([reference](../../../docs/reference/shaders.md#liquid-engine-idxstrdat-archives)): 2,141 shaders from `common.dat` (1,361 pixel, 676 vertex, 75 compute, 14 hull, 14 domain, 1 geometry), each a program of one stage.
- Textures: `textures-s3.dat`, 3,585 `.texture` files, a 28-byte header and the storage the game hands NVN (block-linear). 3,402 are DXT5 / DXT5 sRGB, the rest R8, RGBA8 and R16F. The game makes a memory pool on the file's storage for each and creates nearly all with `MINIMAL_LAYOUT` (259 of the first 271 textures).
- **Open:** 2 shaders fail the Metal compile (`dmsh_build.log`).

## Rendering

- **Resolved: the separate sampler count.** `nvnDeviceGetInteger(0x5F)` returned 4096; the game binds min(`0x5E`, `0x5F`) slots per stage into arrays of 32 and crashed in `nnMain` (`main+0x1aa929c`, slot 64). It is 32 now ([reference](../../../docs/reference/nvn-objects.md#separate-sampler-bindings-per-stage)).
- **Resolved: `MINIMAL_LAYOUT` textures** came out in tiles and stripes; the host now lays them out as the files are ([reference](../../../docs/reference/nvn-objects.md#minimal_layout-textures)).
- **Resolved: no buildings or people,** only their shadows: the mesh shaders' instance data came through global memory the translator could not trace; the global memory window reads it ([reference](../../../docs/reference/shaders.md#global-memory-the-translator-cannot-trace)).
- **Open:** some fog in the title and city backgrounds stays blocky; one `[RED] texture type mismatch` (shader `9776ec1d1fd3b32f_b`, a 3D texture bound where it declares 2D) and one `[RED] stage 1 cb1 has no uniform buffer bound`.

## iPhone

- **Not checked:** the game on an iPhone. On the Mac it stays at about 2.4 GB resident in the city. Its textures are BC3 (DXT5), which an A17 Pro or later samples.
