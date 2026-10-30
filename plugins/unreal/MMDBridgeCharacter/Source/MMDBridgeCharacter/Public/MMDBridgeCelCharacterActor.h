#pragma once

#include "MMDBridgeCharacterActor.h"
#include "MMDBridgeCelCharacterActor.generated.h"

/** Three independently animated surfaces, sharing one editable character mesh. */
UCLASS(Blueprintable)
class MMDBRIDGECHARACTER_API AMMDBridgeCelCharacterActor : public AMMDBridgeCharacterActor
{
    GENERATED_BODY()
public:
    AMMDBridgeCelCharacterActor();
    virtual void OnConstruction(const FTransform& Transform) override;
    /** Evaluate the current animation inputs without advancing playback time.
     * Useful for deterministic editor automation without a realtime viewport. */
    UFUNCTION(BlueprintCallable, Category="Animation") int32 RefreshPose();

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category="Character") TObjectPtr<USkeletalMeshComponent> FilterMesh;
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Character") TArray<TObjectPtr<UMaterialInterface>> FilterMaterials;
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Animation") TObjectPtr<UAnimSequence> FilterAnimation;
};
