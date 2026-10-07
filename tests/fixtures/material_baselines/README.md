# Material output regression evidence

These files are output contracts, never scene-specific conversion recipes.
The common main converter does not read them to select a material route.

- `mygo_main_20261007.json`: technically validated clean main import at code
  `b46cdc8`; visual review remains pending. Includes existing placeholders.
- `railway_historical_fullscenev3.json`: historical comparison, using its own
  recorded settings and engine version. Do not assume the new common main
  reproduces it; compare the actual outputs and review any differences.
- `railway_main_before_translucent_20261007.json`: first clean common-main
  import, with 34 unsupported thin-sheet material records.
- `mygo_main_20261007_schema4.json` and
  `mygo_main_thin_candidate_20261007_schema4.json`: independent original/new
  clean outputs. Full USD layer semantics are compared after normalizing
  only the import namespace; receipts bind semantic hashes to current bytes.
  The original schema 3 snapshots remain archival evidence.
- `railway_historical_fullscenev3_schema4.json` and
  `railway_main_before_translucent_20261007_schema4.json`: schema 4 captures
  of those two original jobs; their geometry uses raw file-byte checks and
  remains conservative when the new import namespace changes.
- `railway_main_thin_candidate_20261007_schema4.json`: fresh common-main
  candidate, 20 converted and 38 approximate materials, zero placeholders.
  The 24 previously successful material outputs match the first clean main
  import, including a native saved-asset audit. 34 previously unsupported
  thin variants now have Masked coverage and legacy_diffusion. Glass-logo
  and volume-lighting differences remain pending visual review.

Record fresh candidate imports with the same source and effective settings.
Run `scripts/check_material_baseline.py <candidate-job> --baseline <file>`.
The gate checks texture bytes, material semantics, coordinate and normal
payloads, bindings, software versions and runtime code. A new job directory
and a new UE root are required. Rechecking the original job is integrity
evidence only. Native renderer changes require matched-frame visual review.

Technical baseline registration does not imply visual acceptance. Never
replace a baseline merely to make an optimization pass; retain the original
evidence and obtain review of the concrete differences.

The MyGO export contracts match for all 246 records and 101 texture files.
Native checks cover 186 successful/approximate materials: 183 saved originals
match directly. Three saved originals were edited after import, including
the floor's Source_59 scalar. Do not overwrite them or call those edits a
converter regression. Fresh controls built with the frozen b46cdc8 renderer
match the new native outputs for all three. See the work regression directory
for the read-only audit, asset-ledger drift and frozen-control receipts.
Native code changed, so the CLI still requires separate native visual review.
