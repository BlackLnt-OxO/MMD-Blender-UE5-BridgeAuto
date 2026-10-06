"""UE 5.8 acceptance for Plan V2 texture import settings."""
import json
import struct
import zlib
from pathlib import Path

import unreal as u

ROOT = Path(__file__).resolve().parents[1]
from mmd_bridge.ue_materials import Materials

DEST = "/Game/On_9_24_开发_PlanV2"
OUTPUT = ROOT / "work" / "plan_v2_validation" / "ue_textures.json"
SOURCE = ROOT / "work" / "plan_v2_validation" / "texture_sources"


def png(path, rgb):
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))
    pixel = b"\0" + bytes(rgb)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" +
                     chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) +
                     chunk(b"IDAT", zlib.compress(pixel)) + chunk(b"IEND", b""))


def main():
    SOURCE.mkdir(parents=True, exist_ok=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    u.EditorAssetLibrary.make_directory(DEST + "/Textures")
    u.EditorAssetLibrary.make_directory(DEST + "/Materials")
    report = {"warnings": [], "checks": {}}
    materials = Materials(DEST, "PlanV2_TextureAcceptance", report)
    cases = (("color", (210, 70, 30), "sRGB", False, True,
              u.TextureCompressionSettings.TC_DEFAULT),
             ("linear", (128, 128, 128), "Non-Color", True, False,
              u.TextureCompressionSettings.TC_BC7))
    results = {}
    for name, pixel, space, data, expected_srgb, expected_compression in cases:
        path = SOURCE / (name + ".png")
        png(path, pixel)
        image = {"id": "PlanV2_" + name, "path": str(path), "color_space": space}
        texture = materials.texture(image, data=data)
        srgb = texture.get_editor_property("srgb")
        compression = texture.get_editor_property("compression_settings")
        results[name] = {"asset": texture.get_path_name(), "srgb": bool(srgb),
                         "compression": str(compression)}
        assert srgb == expected_srgb, (name, srgb)
        assert compression == expected_compression, (name, compression)
        if data:
            materials.material = materials.create("M_PlanV2_TexturePolicy", u.Material,
                                                  u.MaterialFactoryNew())
            _, output = materials.expr({"op": "texture", "image": image,
                                        "uv": {"op": "uv", "index": 0}}, data=True)
            assert output == "R", output
            u.EditorAssetLibrary.save_loaded_asset(materials.material)
    OUTPUT.write_text(json.dumps({"ok": True, "textures": results},
                                 ensure_ascii=False, indent=2), encoding="utf-8")


try:
    main()
except Exception as exc:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({"ok": False, "error": str(exc)},
                                 ensure_ascii=False), encoding="utf-8")
    raise
