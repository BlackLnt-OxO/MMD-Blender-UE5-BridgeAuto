Editor-only Python access to the actual level viewport mode. Source fog must be
restored when the user selects Path Tracing, independently of Lumen post-process settings.

From a source checkout, run `.venv\Scripts\python.exe scripts\build_viewport_helper.py
--unreal "<target engine>/Engine/Binaries/Win64/UnrealEditor.exe"`.
This compiles only this plugin using the target engine's RunUAT BuildPlugin.
Output goes to a new private folder in `%TEMP%/MMDBridgeNative`; matching Binaries
are then copied here. See `docs/INSTALL_WINDOWS.md` for Visual Studio prerequisites.
Installation requires matching engine and plugin `UnrealEditor.modules` BuildIds.
An incompatible managed copy is disabled instead of loading its old DLL.

Fog visibility is world-wide; simultaneous Lumen and Path Tracing viewports of one
world are not supported. Runtime MRQ jobs do not need this editor module.
