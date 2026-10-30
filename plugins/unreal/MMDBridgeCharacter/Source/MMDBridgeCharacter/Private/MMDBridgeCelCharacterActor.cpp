#include "MMDBridgeCelCharacterActor.h"
#include "Components/SkeletalMeshComponent.h"
#include "Animation/AnimSequence.h"

AMMDBridgeCelCharacterActor::AMMDBridgeCelCharacterActor()
{
    FilterMesh = CreateDefaultSubobject<USkeletalMeshComponent>(TEXT("FilterMesh"));
    FilterMesh->SetupAttachment(GetSkeletalMeshComponent());
    FilterMesh->SetCollisionEnabled(ECollisionEnabled::NoCollision);
    FilterMesh->SetGenerateOverlapEvents(false);
    FilterMesh->SetCastShadow(false);
    FilterMesh->bUseAttachParentBound = true;
    FilterMesh->bSyncAttachParentLOD = true;
    FilterMesh->TranslucencySortPriority = 0;
    FilterMesh->VisibilityBasedAnimTickOption = EVisibilityBasedAnimTickOption::AlwaysTickPoseAndRefreshBones;
    FilterMesh->bEnableUpdateRateOptimizations = false;
    FilterMesh->SetUpdateAnimationInEditor(true);
}

void AMMDBridgeCelCharacterActor::OnConstruction(const FTransform& Transform)
{
    Super::OnConstruction(Transform);
    FilterMesh->SetSkeletalMeshAsset(CharacterMesh);
    FilterMesh->EmptyOverrideMaterials();
    for (int32 Slot = 0; Slot < FilterMaterials.Num(); ++Slot)
        FilterMesh->SetMaterial(Slot, FilterMaterials[Slot]);
    FilterMesh->OverrideAnimationData(FilterAnimation, true, true);
}

int32 AMMDBridgeCelCharacterActor::RefreshPose()
{
    int32 Count = 0;
    for (USkeletalMeshComponent* Mesh : {GetSkeletalMeshComponent(), FilterMesh.Get(), OutlineMesh.Get()})
    {
        if (!Mesh || !Mesh->GetSkeletalMeshAsset()) continue;
        Mesh->HandleExistingParallelEvaluationTask(true, true);
        Mesh->TickAnimation(0.f, false);
        Mesh->RefreshBoneTransforms();
        Mesh->UpdateComponentToWorld();
        Mesh->FinalizeBoneTransform();
        Mesh->MarkRenderTransformDirty();
        Mesh->MarkRenderDynamicDataDirty();
        ++Count;
    }
    return Count;
}
