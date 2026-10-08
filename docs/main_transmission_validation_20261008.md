# Main material transmission and instance light validation — 2026-10-08

The main legacy workflow remains the default. Eligible transmitting surfaces may retain Glass/Principled transmission and continuous fractional opacity through Substrate colored transmittance. Other coat, metal, bake and ground rules stay unchanged. Unsupported eligible closures retain explicit approximation/review status.

Leaves, thin cutouts and characters retain their previous routes. Equivalent repeated material graphs are counted across separately named copies. Small objects with a maximum dimension of 1 m or less and at least 16 material uses keep the previous route; these thresholds are deterministic performance guards, not measured per-object frame costs. Transmission geometry disables Nanite where required to retain full supported geometry. Existing maps are not migrated.

## Harbor evidence

- Native source: `F:/MMD_Scene/Blender/SableFiSh/港口/Harbor.blend`.
- Separate trial namespace: `/Game/Harbor_Transmission_Main_20261008_01`.
- Source audit found 46 light objects, all Point. Including evaluated instances produces 64 source lights. None of the original light trees contains IES, direction vectors, angular math or distribution groups. They contain constant Emission, optionally Blackbody at 3500/4500 K. This audit examined source data, not just exported light types.
- Local `Sphere` fog uses Volume Scatter density 0.002 and anisotropy 0. A directional-looking halo in a photograph/render is not evidence of a procedural angular light shader. Lamp geometry/occlusion and scattering must be checked separately; a process-only shadow-disabled render changed the ground/lamp lighting but still did not reproduce the source halo. This rules out shadow occlusion as the sole cause of the UE difference; it does not establish the full source beam mechanism. Local volume-light response remains a review item.
- Frozen scene resume previously omitted collection/GN instance lights. Their records are now saved/restored, and old consolidated checkpoints enumerate instance lights from the source without re-freezing geometry.
- Native collection fixture verified first export, frozen resume and old-checkpoint migration each retain the same three lights.
- Native UE transmission fixture built Glass, Principled transmission and fractional Alpha with colored transmittance, an Opacity connection and no Opacity Mask. Dense small glass and leaf cases kept their previous fallback; opaque control stayed opaque.
- Harbor reanalysis compared 76 records outside eligible transmission attempts against the old main import: no strategy, channel, closure or surface-policy changes. Six eligible records were allowed to change; three Material.007 records remain approximations. Twenty-eight repeated small-prop material records and two foliage records remain on the legacy route.
- UE import and saved-level reload verified 64 PointLight actors, none missing. Material.061 still has its pre-existing unsupported SSS fallback; this is not a completed visual acceptance.

Source distribution audit: `work/world_appearance/harbor_distribution_audit.json`.
Source local fog audit: `work/world_appearance/harbor_local_fog_nodes.json`.
Trial reports: `work/world_appearance/harbor_transmission_main_20261008_01/`.

## Angular lighting contract

The current world/light workflow supports external unmodified IES through native TextureLightProfile and a restricted achromatic angular expression through Light Function, including Incoming components and supported math with node-group contexts. Arbitrary directional colors, combinations of IES and math, unsupported powers/vector expressions or complex shader closures are not fully reproduced; they retain explicit needs_review diagnostics. Counting imported lights or compiling a Light Function does not establish source-distribution equivalence. Matched runtime renders are required, particularly for volume lighting and orientation/normalization.

Restoring 54 lights changes lighting and performance independently of transmission. A comparison against the old 10-light import cannot isolate the cost of transmission. No such performance percentage is claimed.

Native 2560×1080 source-camera frame 1 completed with no GPU crash, camera binding failure or runtime material failure (`Render_20261008_135026_b32085a7`). A separate 64-light shadow-disabled diagnostic also rendered successfully (`Render_20261008_135257_22792deb`), without changing saved assets. The old MyGO/Railway 630 protected files remained hash-identical. Source unsupported SSS/approximation issues remain explicit, even though the renderer itself succeeded.
