#pragma once
#include "Kismet/BlueprintFunctionLibrary.h"
#include "MMDBridgeViewportLibrary.generated.h"

class UStaticMeshComponent;
class UMovieSceneSequence;

UCLASS()
class MMDBRIDGEVIEWPORT_API UMMDBridgeViewportLibrary : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()
public:
    UFUNCTION(BlueprintPure, Category="MMDBridge|Editor")
    static FName GetActiveViewportPipeline();

    UFUNCTION(BlueprintPure, Category="MMDBridge|Editor")
    static TArray<FVector> GetMaterialSlotVertices(UStaticMeshComponent* Component, int32 Slot);

    // MRQ expands every infinite-capable section back to the shot start,
    // including later hold sections. Keep finite later sections in their range.
    UFUNCTION(BlueprintCallable, Category="MMDBridge|Editor")
    static int32 ProtectBoundedSequenceSections(UMovieSceneSequence* Sequence);
};
