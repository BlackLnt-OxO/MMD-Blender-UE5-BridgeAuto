#pragma once

#include "CoreMinimal.h"
#include "Animation/SkeletalMeshActor.h"
#include "MMDBridgeCharacterActor.generated.h"

class UAnimSequence;

UCLASS(Blueprintable)
class MMDBRIDGECHARACTER_API AMMDBridgeCharacterActor : public ASkeletalMeshActor
{
    GENERATED_BODY()
public:
    AMMDBridgeCharacterActor();
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Character") TObjectPtr<USkeletalMesh> CharacterMesh;
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Character") TArray<TObjectPtr<UMaterialInterface>> MainMaterials;
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Character") TArray<TObjectPtr<UMaterialInterface>> OutlineMaterials;
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Animation") TObjectPtr<UAnimSequence> MainAnimation;
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Animation") TObjectPtr<UAnimSequence> OutlineAnimation;
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category="Character") TObjectPtr<USkeletalMeshComponent> OutlineMesh;
    virtual void OnConstruction(const FTransform& Transform) override;
};
