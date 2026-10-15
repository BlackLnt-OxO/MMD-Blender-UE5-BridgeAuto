using UnrealBuildTool;

public class MMDBridgeCharacter : ModuleRules
{
    public MMDBridgeCharacter(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new [] { "Core", "CoreUObject", "Engine" });
        PrivateDependencyModuleNames.AddRange(new [] { "RenderCore", "RHI" });
    }
}
