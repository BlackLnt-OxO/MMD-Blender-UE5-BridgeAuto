#include "MMDBridgeViewportLibrary.h"
#include "Editor.h"
#include "LevelEditorViewport.h"
#include "Modules/ModuleManager.h"

FName UMMDBridgeViewportLibrary::GetActiveViewportPipeline()
{
    if (!GEditor || !GEditor->GetEditorWorldContext().World() || !GCurrentLevelEditingViewportClient)
        return NAME_None;
    return GCurrentLevelEditingViewportClient->GetViewMode() == VMI_PathTracing
        ? FName(TEXT("path_tracing")) : FName(TEXT("raster"));
}

IMPLEMENT_MODULE(FDefaultModuleImpl, MMDBridgeViewport)
