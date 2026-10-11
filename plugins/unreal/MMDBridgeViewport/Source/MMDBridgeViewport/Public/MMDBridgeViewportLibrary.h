#pragma once
#include "Kismet/BlueprintFunctionLibrary.h"
#include "MMDBridgeViewportLibrary.generated.h"

UCLASS()
class MMDBRIDGEVIEWPORT_API UMMDBridgeViewportLibrary : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()
public:
    UFUNCTION(BlueprintPure, Category="MMDBridge|Editor")
    static FName GetActiveViewportPipeline();
};
