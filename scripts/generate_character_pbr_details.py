"""Build repeatable, tileable PBR data maps from numerical surface profiles.

Run with the bundled Python (NumPy/Pillow). No source artwork or AI is used.
Normals use OpenGL green; the existing UE texture importer flips green.
"""
from pathlib import Path
import math
import numpy as np
from PIL import Image

FOLDER = Path(__file__).resolve().parents[1]/'resources/character_materials/Character_PBR'


def normal(height, slope):
    dx = (np.roll(height, -1, 1)-np.roll(height, 1, 1))*slope
    dy = (np.roll(height, -1, 0)-np.roll(height, 1, 0))*slope
    values = np.stack((-dx, -dy, np.ones_like(dx)), -1)
    return values/np.linalg.norm(values, axis=-1, keepdims=True)*.5+.5


def save(name, values):
    if values.ndim == 2:
        values = np.repeat(values[..., None], 3, -1)
    # Blender data images use rows ordered from the bottom of the UV tile.
    rgb = np.rint(np.clip(values[::-1], 0, 1)*255).astype(np.uint8)
    Image.fromarray(rgb).save(FOLDER/(name+'.png'))


def generate_textiles():
    """A smooth satin float weave and an interlocking nylon knit, at small scale."""
    FOLDER.mkdir(parents=True, exist_ok=True)
    y, x = np.mgrid[:512, :512]/512
    satin = .025*np.cos(math.tau*(x*32+.12*np.sin(y*math.tau*4)))
    satin += .004*np.cos(y*math.tau*32)
    save('satin_normal', normal(satin, 16))
    save('satin_roughness', .5+.12*np.cos(math.tau*(x*32-y*8)))
    row = np.floor(y*16)
    cx = (x*16+.5*(row % 2)) % 1-.5
    cy = (y*16) % 1-.5
    radius = np.sqrt((cx/.34)**2+((cy+.06)/.48)**2)
    loops = np.exp(-((radius-1)/.16)**2)
    legs = np.exp(-((np.abs(cx)-(.18+.3*(cy+.5)))/.06)**2)
    knit = np.maximum(loops, legs*.65)
    save('hosiery_normal', normal(knit*.025, 12))
    save('hosiery_roughness', .5+(knit-.4)*.16)
    save('hosiery_mask', knit)
    print('Generated 5 deterministic satin and hosiery data maps:', FOLDER)


def generate():
    FOLDER.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20261018)
    size = 512
    y, x = np.mgrid[:size, :size]/size
    noise = rng.random((size, size))-.5
    for _ in range(4):
        noise = (noise+np.roll(noise, 1, 0)+np.roll(noise, -1, 0)+
                 np.roll(noise, 1, 1)+np.roll(noise, -1, 1))/5
    pores = np.zeros_like(x)
    for j in range(11):
        for i in range(11):
            cx, cy = (np.array([i, j])+.5+rng.uniform(-.36, .36, 2))/11
            dx, dy = (x-cx+.5) % 1-.5, (y-cy+.5) % 1-.5
            radius = rng.uniform(.008, .017)
            distance = (dx/radius)**2+(dy/(radius*rng.uniform(.7, 1.3)))**2
            pores -= rng.uniform(.055, .10)*np.exp(-distance*1.8)
            pores += .009*np.exp(-((np.sqrt(distance)-1.1)/.35)**2)
    save('skin_normal', normal(pores+noise*.05, 22))
    save('skin_roughness', .5+noise*.7-pores*.5)
    warp = np.sin(x*math.tau*16)
    weft = np.sin(y*math.tau*16)
    crossing = np.cos(x*math.tau*8)*np.cos(y*math.tau*8)
    weave = .045*(warp*(1+crossing*.65)+weft*(1-crossing*.65))
    save('cloth_normal', normal(weave+noise*.025, 28))
    save('cloth_roughness', .5+crossing*.12+noise*.4)
    leather = noise.copy()
    for _ in range(10):
        leather = (leather+np.roll(leather, 1, 0)+np.roll(leather, 1, 1))/3
    save('leather_normal', normal(leather+pores*.4, 28))
    save('leather_roughness', .5+leather*1.8-pores*.6)

    # Preserve the existing gentle hair and generic defaults as separate maps.
    rng_old = np.random.default_rng(1017)
    heights = rng_old.random((256, 256), dtype=np.float32)
    for _ in range(3):
        heights = (heights+np.roll(heights, 1, 0)+np.roll(heights, -1, 0)+
                   np.roll(heights, 1, 1)+np.roll(heights, -1, 1))/5
    hy, hx = np.mgrid[:256, :256]/256
    hair = (np.sin(hx*math.tau*47+.2*np.sin(hy*math.tau*3))+
            .3*np.sin(hx*math.tau*89))*.12
    save('micro_normal', normal(heights, 1))
    save('hair_normal', normal(hair, 1))

    size = 1024
    y, x = np.mgrid[:size, :size]/size
    water = np.zeros_like(x)
    drops, streams = np.zeros_like(x), np.zeros_like(x)

    def drop(cx, cy, rx, ry):
        dx, dy = (x-cx+.5) % 1-.5, (y-cy+.5) % 1-.5
        # Slight pear shape follows gravity; round ones remain round.
        radial = (dx/(rx*(1-.16*np.clip(dy/ry, -1, 1))))**2+(dy/ry)**2
        inside = np.clip((1-radial)*min(rx, ry)*size, 0, 1)
        height = np.clip(1-radial, 0, 1)**.7*rx*size*.45
        np.maximum(water, height, out=water)
        np.maximum(drops, inside, out=drops)

    for _ in range(44):
        cx, cy = rng.random(2)
        radius = rng.uniform(.005, .019)
        drop(cx, cy, radius, radius*rng.uniform(1, 2.1))
    for _ in range(3):
        cx, bottom = rng.random(2)
        length = rng.uniform(.35, .8)
        width = rng.uniform(.0025, .0045)
        phase = rng.uniform(0, math.tau)
        along = (y-bottom) % 1
        center = cx+.007*np.sin(along*math.tau*2+phase)
        across = ((x-center+.5) % 1-.5)/width
        ends = np.clip(along/.025, 0, 1)*np.clip((length-along)/.04, 0, 1)
        mask = np.clip((1-across**2)*width*size, 0, 1)*ends
        height = np.clip(1-across**2, 0, 1)**.7*width*size*.22*ends
        np.maximum(water, height, out=water)
        np.maximum(streams, mask, out=streams)
        drop(cx+.007*math.sin(phase), bottom, width*2.3, width*3.7)
    save('wet_normal', normal(water, .5))
    save('wet_mask', np.stack((np.maximum(drops, streams), drops, streams), -1))
    print('Generated 10 deterministic PBR detail maps:', FOLDER)


if __name__ == '__main__':
    import sys
    if '--textiles-only' not in sys.argv:
        generate()
    generate_textiles()
