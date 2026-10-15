#include "MMDBridgeCharacterActor.h"
#include "Components/SkeletalMeshComponent.h"
#include "Animation/AnimSequence.h"

AMMDBridgeCharacterActor::AMMDBridgeCharacterActor()
{
    OutlineMesh=CreateDefaultSubobject<USkeletalMeshComponent>(TEXT("OutlineMesh"));
    OutlineMesh->SetupAttachment(GetSkeletalMeshComponent());
    OutlineMesh->SetCollisionEnabled(ECollisionEnabled::NoCollision);
    OutlineMesh->SetCastShadow(false);
    OutlineMesh->bUseAttachParentBound=true;
    OutlineMesh->bSyncAttachParentLOD=true;
    OutlineMesh->TranslucencySortPriority=-1;
    for (USkeletalMeshComponent* Mesh : {GetSkeletalMeshComponent(),OutlineMesh.Get()})
    {
        Mesh->VisibilityBasedAnimTickOption=EVisibilityBasedAnimTickOption::AlwaysTickPoseAndRefreshBones;
        Mesh->bEnableUpdateRateOptimizations=false;
        Mesh->SetUpdateAnimationInEditor(true);
    }
}

void AMMDBridgeCharacterActor::OnConstruction(const FTransform& Transform)
{
    Super::OnConstruction(Transform);
    USkeletalMeshComponent* Main=GetSkeletalMeshComponent();
    Main->SetSkeletalMeshAsset(CharacterMesh);
    OutlineMesh->SetSkeletalMeshAsset(CharacterMesh);
    Main->EmptyOverrideMaterials();
    OutlineMesh->EmptyOverrideMaterials();
    for (int32 Slot=0;Slot<MainMaterials.Num();++Slot) Main->SetMaterial(Slot,MainMaterials[Slot]);
    for (int32 Slot=0;Slot<OutlineMaterials.Num();++Slot) OutlineMesh->SetMaterial(Slot,OutlineMaterials[Slot]);
    // Both meshes evaluate their own animation; Sequencer binds them separately.
    Main->OverrideAnimationData(MainAnimation,true,true);
    OutlineMesh->OverrideAnimationData(OutlineAnimation,true,true);
}
