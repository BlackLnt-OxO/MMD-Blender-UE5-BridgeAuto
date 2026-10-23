from mmd_bridge.controller import install_viewport_helper
from mmd_bridge.core import atomic_json


def test_reconnect_does_not_rewrite_an_identical_loaded_native_dll(tmp_path,monkeypatch):
    import mmd_bridge.controller as m
    root=tmp_path/'source';project=tmp_path/'project/Test.uproject'
    source=root/'plugins/unreal/MMDBridgeViewport'
    target=project.parent/'Plugins/MMDBridgeViewport'
    engine=tmp_path/'engine/UnrealEditor.exe'
    for directory in (source/'Binaries/Win64',target/'Binaries/Win64',engine.parent):directory.mkdir(parents=True)
    project.write_text('{}')
    for modules in (source/'Binaries/Win64/UnrealEditor.modules',engine.with_name('UnrealEditor.modules')):
        atomic_json(modules,{'BuildId':'matching-build'})
    (target/'.mmd_bridge_managed').write_text('managed')
    for directory in (source,target):(directory/'Binaries/Win64/Loaded.dll').write_bytes(b'identical-native-binary')
    real=m.shutil.copy2
    def copy(src,dst,*args,**kwargs):
        assert not str(src).endswith('Loaded.dll'), 'Would fail with WinError 32 in a live editor'
        return real(src,dst,*args,**kwargs)
    monkeypatch.setattr(m.shutil,'copy2',copy)
    assert install_viewport_helper(project,root,engine)
