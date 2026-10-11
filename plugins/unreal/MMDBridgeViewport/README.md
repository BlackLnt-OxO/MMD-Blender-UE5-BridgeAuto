Editor-only Python access to the actual level viewport mode. Source fog must be
restored when the user selects Path Tracing, independently of Lumen post-process settings.

Build with the target engine's `Engine/Build/BatchFiles/RunUAT.bat BuildPlugin
-Plugin=<absolute path to MMDBridgeViewport.uplugin> -Package=<output> -TargetPlatforms=Win64`.
Copy the package's Binaries into this plugin before installing through the bridge.
Installation requires matching engine and plugin `UnrealEditor.modules` BuildIds.
An incompatible managed copy is disabled instead of loading its old DLL.

Fog visibility is world-wide; simultaneous Lumen and Path Tracing viewports of one
world are not supported. Runtime MRQ jobs do not need this editor module.
