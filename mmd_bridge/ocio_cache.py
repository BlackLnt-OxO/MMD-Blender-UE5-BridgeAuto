"""Content-addressed portable OCIO bundles with native-safe file paths."""
import hashlib
import shutil
import tempfile
from pathlib import Path


def portable_config(source, project_dir):
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError('OCIO 配置不存在: ' + str(source))
    files = sorted(p for p in source.parent.rglob('*') if p.is_file())
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(source.parent)
        if not relative.as_posix().isascii():
            raise ValueError('OCIO 依赖文件名称必须可由原生接口读取: ' + str(relative))
        digest.update(relative.as_posix().encode()); digest.update(path.read_bytes())
    base = Path(project_dir).resolve() / 'Saved/MMDBridge/OCIO'
    if not str(base).isascii():
        base = Path(tempfile.gettempdir()) / 'MMDBridge/OCIO'
    if not str(base).isascii():
        raise ValueError('找不到 ASCII OCIO 缓存目录')
    folder = base / digest.hexdigest()
    for path in files:
        target = folder / path.relative_to(source.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != path.read_bytes():
            temporary = target.with_name(target.name + '.partial')
            shutil.copyfile(path, temporary); temporary.replace(target)
    return folder / source.name
