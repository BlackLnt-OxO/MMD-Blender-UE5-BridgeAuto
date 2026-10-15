#include "MMDBridgeFaceDirectionComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "Engine/SkeletalMesh.h"
#include "Engine/Texture2D.h"
#include "GameFramework/Actor.h"
#include "Modules/ModuleManager.h"

IMPLEMENT_MODULE(FDefaultModuleImpl, MMDBridgeCharacter)

UMMDBridgeFaceDirectionComponent::UMMDBridgeFaceDirectionComponent()
{
    PrimaryComponentTick.bCanEverTick = true;
    bTickInEditor = true;
}

void UMMDBridgeFaceDirectionComponent::BindPoseCallback()
{
    if (!TargetMesh || !TargetMesh->GetSkeletalMeshAsset() || PoseCallback.IsValid()) return;
    const FReferenceSkeleton& Ref = TargetMesh->GetSkeletalMeshAsset()->GetRefSkeleton();
    const int32 Bone = Ref.FindBoneIndex(HeadBone);
    if (Bone == INDEX_NONE) return;
    FTransform Rest = Ref.GetRefBonePose()[Bone];
    for (int32 Parent=Ref.GetParentIndex(Bone); Parent!=INDEX_NONE; Parent=Ref.GetParentIndex(Parent))
        Rest *= Ref.GetRefBonePose()[Parent];
    ReferenceInverse = Rest.GetRotation().Inverse();
    PoseCallback = TargetMesh->RegisterOnBoneTransformsFinalizedDelegate(
        FOnBoneTransformsFinalizedMultiCast::FDelegate::CreateUObject(this,&UMMDBridgeFaceDirectionComponent::UpdateDirections));
    AddTickPrerequisiteComponent(TargetMesh);
}

void UMMDBridgeFaceDirectionComponent::OnRegister()
{
    Super::OnRegister();
    if (!TargetMesh && GetOwner()) TargetMesh=GetOwner()->FindComponentByClass<USkeletalMeshComponent>();
    BindPoseCallback();
    UpdateDirections();
}

void UMMDBridgeFaceDirectionComponent::OnUnregister()
{
    if (TargetMesh && PoseCallback.IsValid()) TargetMesh->UnregisterOnBoneTransformsFinalizedDelegate(PoseCallback);
    PoseCallback.Reset();
    Super::OnUnregister();
}

void UMMDBridgeFaceDirectionComponent::TickComponent(float DeltaTime,ELevelTick TickType,FActorComponentTickFunction* TickFunction)
{
    Super::TickComponent(DeltaTime,TickType,TickFunction);
    BindPoseCallback();
    UpdateDirections();
}

void UMMDBridgeFaceDirectionComponent::UpdateDirections()
{
    if (!TargetMesh || TargetMesh->GetBoneIndex(HeadBone)==INDEX_NONE) return;
    const FQuat Rotation=TargetMesh->GetSocketQuaternion(HeadBone)*ReferenceInverse;
    const FVector F=Rotation.RotateVector(Forward).GetSafeNormal();
    const FVector R=Rotation.RotateVector(Right).GetSafeNormal();
    const FVector U=Rotation.RotateVector(Up).GetSafeNormal();
    // Reserve 20..31; source object-random data uses index 0.
    TargetMesh->SetCustomPrimitiveDataVector4(20,FVector4(F,0));
    TargetMesh->SetCustomPrimitiveDataVector4(24,FVector4(R,0));
    TargetMesh->SetCustomPrimitiveDataVector4(28,FVector4(U,0));
}

UMMDBridgeFaceDirectionComponent* UMMDBridgeCharacterLibrary::AttachFaceDirection(AActor* Actor,FName HeadBone,
    FVector Forward,FVector Right,FVector Up)
{
    if (!Actor) return nullptr;
    USkeletalMeshComponent* Mesh=Actor->FindComponentByClass<USkeletalMeshComponent>();
    if (!Mesh || Mesh->GetBoneIndex(HeadBone)==INDEX_NONE) return nullptr;
    UMMDBridgeFaceDirectionComponent* Component=Actor->FindComponentByClass<UMMDBridgeFaceDirectionComponent>();
    if (!Component)
    {
        Component=NewObject<UMMDBridgeFaceDirectionComponent>(Actor,TEXT("MMDBridgeFaceDirection"),RF_Transactional);
        Actor->AddInstanceComponent(Component);
    }
    else if (Component->IsRegistered()) Component->UnregisterComponent();
    Component->TargetMesh=Mesh; Component->HeadBone=HeadBone;
    Component->Forward=Forward; Component->Right=Right; Component->Up=Up;
    Component->RegisterComponent();
    return Component;
}

TMap<FString,int64> UMMDBridgeCharacterLibrary::TexturePlatformInfo(UTexture2D* Texture)
{
    TMap<FString,int64> Result;
    if (!Texture) return Result;
#if WITH_EDITOR
    Texture->BlockOnAnyAsyncBuild();
#endif
    if (!Texture->GetPlatformData()) return Result;
    const FTexturePlatformData* Data=Texture->GetPlatformData();
    Result.Add(TEXT("width"),Data->SizeX); Result.Add(TEXT("height"),Data->SizeY);
    Result.Add(TEXT("pixel_format"),Data->PixelFormat);
    Result.Add(TEXT("is_r16f"),Data->PixelFormat==PF_R16F ? 1 : 0);
    Result.Add(TEXT("bytes"),Texture->CalcTextureMemorySizeEnum(TMC_AllMips));
    return Result;
}

TMap<FString,FVector> UMMDBridgeCharacterLibrary::FaceDirectionState(UMMDBridgeFaceDirectionComponent* Component)
{
    TMap<FString,FVector> Result;
    if (!Component || !Component->TargetMesh) return Result;
    const TArray<float>& Data=Component->TargetMesh->GetCustomPrimitiveData().Data;
    if (Data.Num()<32) return Result;
    Result.Add(TEXT("forward"),FVector(Data[20],Data[21],Data[22]));
    Result.Add(TEXT("right"),FVector(Data[24],Data[25],Data[26]));
    Result.Add(TEXT("up"),FVector(Data[28],Data[29],Data[30]));
    const FRotator Head=Component->TargetMesh->GetSocketRotation(Component->HeadBone);
    Result.Add(TEXT("head_rotation"),FVector(Head.Pitch,Head.Yaw,Head.Roll));
    return Result;
}
