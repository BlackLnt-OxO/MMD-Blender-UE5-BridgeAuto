#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "MMDBridgeFaceDirectionComponent.generated.h"

class USkeletalMeshComponent;
class UTexture2D;

UCLASS(ClassGroup=(Rendering), meta=(BlueprintSpawnableComponent))
class MMDBRIDGECHARACTER_API UMMDBridgeFaceDirectionComponent : public UActorComponent
{
    GENERATED_BODY()
public:
    UMMDBridgeFaceDirectionComponent();
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Face SDF") TObjectPtr<USkeletalMeshComponent> TargetMesh;
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Face SDF") FName HeadBone;
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Face SDF") FVector Forward = FVector(1,0,0);
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Face SDF") FVector Right = FVector(0,-1,0);
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Face SDF") FVector Up = FVector(0,0,1);
    virtual void OnRegister() override;
    virtual void OnUnregister() override;
    virtual void TickComponent(float DeltaTime, ELevelTick TickType, FActorComponentTickFunction* TickFunction) override;
    UFUNCTION(BlueprintCallable, Category="Face SDF") void UpdateDirections();
private:
    void BindPoseCallback();
    FDelegateHandle PoseCallback;
    FQuat ReferenceInverse = FQuat::Identity;
};

UCLASS()
class MMDBRIDGECHARACTER_API UMMDBridgeCharacterLibrary : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()
public:
    UFUNCTION(BlueprintCallable, Category="MMD Bridge")
    static UMMDBridgeFaceDirectionComponent* AttachFaceDirection(AActor* Actor, FName HeadBone,
        FVector Forward, FVector Right, FVector Up);
    UFUNCTION(BlueprintCallable, Category="MMD Bridge")
    static TMap<FString,int64> TexturePlatformInfo(UTexture2D* Texture);
    UFUNCTION(BlueprintCallable, Category="MMD Bridge")
    static TMap<FString,FVector> FaceDirectionState(UMMDBridgeFaceDirectionComponent* Component);
};
