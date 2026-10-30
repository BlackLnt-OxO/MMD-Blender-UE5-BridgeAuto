using UnrealBuildTool;
public class MMDBridgeViewport : ModuleRules
{
    public MMDBridgeViewport(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new[] { "Core", "CoreUObject", "Engine" });
        PrivateDependencyModuleNames.AddRange(new[] { "UnrealEd", "MeshDescription", "StaticMeshDescription", "MovieScene" });
    }
}
