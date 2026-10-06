"""Persistent rendering defaults for projects connected by the desktop tool."""
from pathlib import Path
import re
import shutil


RENDERER_DEFAULTS = {
    'r.SkinCache.CompileShaders': 'True',
    'r.RayTracing': 'True',
    'r.RayTracing.Shadows': 'True',
    'r.Lumen.HardwareRayTracing': 'True',
    'r.Lumen.HardwareRayTracing.LightingMode': '1',
}
SECTION = '/Script/Engine.RendererSettings'


def ensure_render_defaults(project):
    """Patch only requested renderer keys; keep an original backup, idempotently.

    Hardware ray tracing requires an editor restart and compatible RHI/GPU.
    LightingMode 1 selects hit lighting for both GI and reflections in UE 5.8.
    """
    project = Path(project).resolve()
    if project.suffix.lower() != '.uproject' or not project.is_file():
        raise ValueError('请选择有效的 .uproject')
    path = project.parent / 'Config/DefaultEngine.ini'
    original = path.read_bytes() if path.exists() else b''
    encoding = 'utf-16' if original.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    text = original.decode(encoding)
    newline = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines(keepends=True)
    result, pending, inside, found = [], dict(RENDERER_DEFAULTS), False, False
    for line in lines:
        section = re.fullmatch(r'\s*\[([^\]]+)\]\s*(?:[;#].*)?', line.strip())
        if section:
            if inside:
                result.extend(key + '=' + value + newline for key, value in pending.items())
                pending.clear()
            inside = section.group(1).casefold() == SECTION.casefold()
            found = found or inside
        key = line.split('=', 1)[0].strip() if inside and '=' in line else ''
        canonical = next((name for name in RENDERER_DEFAULTS if key.casefold() == name.casefold()), None)
        if canonical:
            # Replace duplicate assignments too: Unreal uses the final assignment.
            result.append(canonical + '=' + RENDERER_DEFAULTS[canonical] + newline)
            pending.pop(canonical, None)
        else:
            result.append(line)
    if result and not result[-1].endswith(('\r', '\n')):
        result[-1] += newline
    if not found:
        result.append(newline + '[' + SECTION + ']' + newline)
    result.extend(key + '=' + value + newline for key, value in pending.items())
    updated = ''.join(result)
    if updated == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + '.mmdb-render-backup')
    if original and not backup.exists():
        shutil.copy2(path, backup)
    # Preserve the BOM choice and newline convention of existing files.
    output_encoding = encoding if original.startswith((b'\xef\xbb\xbf', b'\xff\xfe', b'\xfe\xff')) else 'utf-8'
    temporary = path.with_name(path.name + '.mmdb-render-tmp')
    temporary.write_bytes(updated.encode(output_encoding))
    temporary.replace(path)
    return True
