"""Versioned appearance policy; repair eligibility is independent of visual review."""
LEGACY_MAIN = 'legacy_main_v1'
SEMANTIC = 'semantic_plan_v1'


def workflow(settings):
    # Missing fields belong to an existing job, not a new default Settings.
    return settings.get('material_workflow', SEMANTIC)


def legacy_channels(channels):
    return {k: v for k, v in channels.items() if k in {
        'base_color', 'roughness', 'metallic', 'ior', 'specular', 'specular_tint',
        'normal', 'emission', 'emission_strength', 'opacity'}}


def white_materials(manifest, report):
    from .material_results import restoration_rows
    rows = {r['id']: r for r in restoration_rows(manifest, report)}
    material_failures = {}
    for item in report.get('failures', []):
        if item.get('material_id'):
            material_failures.setdefault(item['material_id'], []).append(item['error'])
    for item in report.get('checks', {}).get('material_binding_failures', []):
        if item.get('material_id'):
            material_failures.setdefault(item['material_id'], []).append(item['error'])
    assets = report.get('assets', {}).get('materials', {})
    # Compile messages are tied to exact asset paths, never a material basename.
    for line in report.get('checks', {}).get('material_compile_errors', []):
        for identity, path in assets.items():
            if path.split('.')[0] in line:
                material_failures.setdefault(identity, []).append(line)
    selected = []
    for identity, row in rows.items():
        if row['conversion_status'] in ('placeholder', 'failed') or identity in material_failures:
            selected.append(dict(row, ue_asset=assets.get(identity),
                                 failures=material_failures.get(identity, [])))
    return selected


def white_ledger(job, manifest, report):
    from .core import atomic_json, file_hash
    rows = white_materials(manifest, report)
    atomic_json(job.folder / 'white_materials.json', {
        'schema': 1, 'workflow': workflow(job.data['settings']),
        'source_sha256': job.data.get('inputs', {}).get('scene'),
        'manifest_sha256': file_hash(job.folder / 'manifest.json'),
        'materials': rows, 'experimental_repair_automatic': False})
    return rows
