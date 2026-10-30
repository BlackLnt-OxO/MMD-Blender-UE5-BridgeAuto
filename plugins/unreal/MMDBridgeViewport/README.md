Editor-only Python access to the actual level viewport mode. Source fog must be
restored when the user selects Path Tracing, independently of Lumen post-process settings.

From a source checkout, run `.venv\Scripts\python.exe scripts\build_native_plugins.py
--plugin MMDBridgeViewport --unreal "<target engine>/Engine/Binaries/Win64/UnrealEditor.exe"`.
Omit `--plugin` to build both native bridge plugins. The old `build_viewport_helper.py`
entry point delegates to the same implementation and still builds only this plugin.
The selected engine's bundled .NET and UBT compile a private host project under
`%TEMP%/MMDBridgeNative`. UBA acceleration and remote execution are disabled; because
UE 5.8 still uses UBA infrastructure locally, its root is explicitly isolated and
write-probed before launch. Global build configuration and ACLs are not modified.
Bounded logs, timeout and repeated-error checks stop the owned compiler process tree.
All outputs are checked before Binaries are published, with previous files retained
and rollback on publication failure. `--no-install` only builds and verifies output.
Other UE 5 versions require `--allow-untested-engine`; this does not establish runtime
compatibility. See `docs/INSTALL_WINDOWS.md` for limits and Visual Studio prerequisites.
Installation requires matching engine and plugin `UnrealEditor.modules` BuildIds.
An incompatible managed copy is disabled instead of loading its old DLL.

Fog visibility is world-wide; simultaneous Lumen and Path Tracing viewports of one
world are not supported. Runtime MRQ jobs do not need this editor module.
