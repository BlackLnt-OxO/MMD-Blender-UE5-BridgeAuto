#pragma once
#include "Kismet/BlueprintFunctionLibrary.h"
#include "MMDBridgeViewportLibrary.generated.h"

class UStaticMeshComponent;

UCLASS()
class MMDBRIDGEVIEWPORT_API UMMDBridgeViewportLibrary : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()
public:
    UFUNCTION(BlueprintPure, Category="MMDBridge|Editor")
    static FName GetActiveViewportPipeline();

    UFUNCTION(BlueprintPure, Category="MMDBridge|Editor")
    static TArray<FVector> GetMaterialSlotVertices(UStaticMeshComponent* Component, int32 Slot);
};
