#include "MMDBridgeViewportLibrary.h"
#include "Editor.h"
#include "LevelEditorViewport.h"
#include "Modules/ModuleManager.h"
#include "Components/StaticMeshComponent.h"
#include "Engine/StaticMesh.h"
#include "MeshDescription.h"
#include "StaticMeshAttributes.h"
#include "FrameRateSectionRanges.h"

FName UMMDBridgeViewportLibrary::GetActiveViewportPipeline()
{
    if (!GEditor || !GEditor->GetEditorWorldContext().World() || !GCurrentLevelEditingViewportClient)
        return NAME_None;
    return GCurrentLevelEditingViewportClient->GetViewMode() == VMI_PathTracing
        ? FName(TEXT("path_tracing")) : FName(TEXT("raster"));
}

IMPLEMENT_MODULE(FDefaultModuleImpl, MMDBridgeViewport)

TArray<FVector> UMMDBridgeViewportLibrary::GetMaterialSlotVertices(UStaticMeshComponent* Component, int32 Slot)
{
    TArray<FVector> Points;
    UStaticMesh* Mesh = Component ? Component->GetStaticMesh() : nullptr;
    if (!Mesh || !Mesh->GetStaticMaterials().IsValidIndex(Slot)) return Points;
    const FMeshDescription* Description = Mesh->GetMeshDescription(0);
    if (!Description) return Points;
    const FStaticMaterial& Material = Mesh->GetStaticMaterials()[Slot];
    const FStaticMeshConstAttributes Attributes(*Description);
    const auto Names = Attributes.GetPolygonGroupMaterialSlotNames();
    const auto Positions = Attributes.GetVertexPositions();
    TSet<FVertexID> Vertices;
    for (FPolygonGroupID Group : Description->PolygonGroups().GetElementIDs())
    {
        if ((Material.ImportedMaterialSlotName.IsNone() || Names[Group] != Material.ImportedMaterialSlotName) &&
            (Material.MaterialSlotName.IsNone() || Names[Group] != Material.MaterialSlotName)) continue;
        for (FPolygonID Polygon : Description->GetPolygonGroupPolygons(Group))
            for (FVertexID Vertex : Description->GetPolygonVertices(Polygon)) Vertices.Add(Vertex);
    }
    const FTransform Transform = Component->GetComponentTransform();
    for (FVertexID Vertex : Vertices) Points.Add(Transform.TransformPosition(FVector(Positions[Vertex])));
    return Points;
}

int32 UMMDBridgeViewportLibrary::ProtectBoundedSequenceSections(UMovieSceneSequence* Sequence)
{
    return ProtectFrameRateSectionRanges(Sequence);
}
