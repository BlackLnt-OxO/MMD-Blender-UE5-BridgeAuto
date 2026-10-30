"""Compatibility entry point for building just the viewport plugin."""
from pathlib import Path

if __package__:
    from . import build_native_plugins as native
else:
    import build_native_plugins as native

ROOT = Path(__file__).resolve().parents[1]


def build(unreal, root=ROOT, **kwargs):
    work = native.build(unreal, root, plugins=('MMDBridgeViewport',), **kwargs)
    return work / 'HostProject/Plugins/MMDBridgeViewport'


if __name__ == '__main__':
    raise SystemExit(native.main(default_plugins=('MMDBridgeViewport',)))
