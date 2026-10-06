"""Shared restoration semantics and truthful results, independent of renderers."""
import hashlib
from pathlib import Path


def asset_name_for_path(folder, name, content_dir):
    """Keep Windows asset filenames below native importer's truncation limit."""
    directory = Path(content_dir).resolve() / folder.removeprefix('/Game/')
    # Interchange also reserves space for cooked package paths, not just the
    # Content filename. A short package path prevents its middle-name elision.
    budget = min(120, 240 - len(str(directory)) - 1 - len('.uasset'), 144 - len(folder) - 1)
    if len(name) <= budget:
        return name
    if budget < 24:
        raise ValueError('UE 输出目录过长，无法保留可靠资产身份: ' + folder)
    suffix = '_' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:16]
    return name[:budget-len(suffix)] + suffix

PRINCIPLED_CHANNELS = {
    'base_color': 'Base Color', 'roughness': 'Roughness', 'metallic': 'Metallic',
    'ior': 'IOR', 'specular': 'Specular IOR Level', 'specular_tint': 'Specular Tint',
    'normal': 'Normal', 'emission': 'Emission Color', 'emission_strength': 'Emission Strength',
    'opacity': 'Alpha', 'transmission': 'Transmission Weight',
    'coat': 'Coat Weight', 'coat_roughness': 'Coat Roughness', 'coat_ior': 'Coat IOR',
    'coat_tint': 'Coat Tint', 'coat_normal': 'Coat Normal',
    'subsurface': 'Subsurface Weight', 'subsurface_radius': 'Subsurface Radius',
    'subsurface_scale': 'Subsurface Scale', 'subsurface_ior': 'Subsurface IOR',
    'subsurface_anisotropy': 'Subsurface Anisotropy',
}


def active_field(field):
    return bool(field and (field.get('op') != 'constant' or field.get('value', 0) != 0))


def closure_features(graph):
    """Walk expanded closure IR; functions must be expanded by the caller."""
    found = set()
    if not graph:
        return found
    if graph['kind'] in ('mix', 'add'):
        return closure_features(graph['a']) | closure_features(graph['b'])
    if 'features' in graph:
        return set(graph['features'])
    if graph['kind'] == 'bsdf_glass':
        found.add('transmission')
    for key in ('transmission', 'coat', 'subsurface'):
        if active_field(graph.get('channels', {}).get(key)):
            found.add(key)
    return found


def closure_refraction(graph):
    """A material refraction input is scalar; retain live mixtures explicitly."""
    if graph['kind'] in ('mix','add'):
        a,b=closure_refraction(graph['a']),closure_refraction(graph['b'])
        if a is None:return b
        if b is None:return a
        if a == b:return a
        if graph['kind']=='mix':
            return {'op':'mix','mode':'MIX','a':a,'b':b,'factor':graph['factor'],
                    'clamp':False,'clamp_factor':True}
        return {'op':'math','mode':'MULTIPLY','a':{'op':'math','mode':'ADD','a':a,'b':b,'clamp':False},
                'b':{'op':'constant','value':.5},'clamp':False}
    if 'transmission' not in closure_features(graph):return None
    return graph['channels'].get('ior',{'op':'constant','value':1.5})


def add_issue(record, code, reason, *, severity='review', channel=None):
    issue = {'code': code, 'reason': reason, 'severity': severity,
             'source_material': record.get('source_name', record.get('name')),
             'source_objects': list(record.get('source_objects', []))}
    if channel:
        issue['channel'] = channel
    issues = record.setdefault('issues', [])
    if issue not in issues:
        issues.append(issue)
    record['requires_visual_review'] = True
    return issue


def finalize_restoration(record, source_object=None):
    """Visual acceptance and conversion correctness are separate dimensions."""
    if source_object:
        objects = record.setdefault('source_objects', [])
        if source_object not in objects:
            objects.append(source_object)
    record.setdefault('issues', [])
    for issue in record['issues']:
        issue['source_objects'] = list(record.get('source_objects', []))
    failed = (record.get('material_error') or record.get('surface') == 'unsupported' or
              record.get('basic_pbr_bake', {}).get('status') == 'failed')
    if failed:
        record['conversion_status'] = 'placeholder' if record.get('surface') == 'pbr' else 'failed'
        add_issue(record, 'source_conversion_failed', str(record.get('material_error') or
                  record.get('basic_pbr_bake', {}).get('error') or 'unsupported surface'), severity='error')
    else:
        if (record.get('basic_pbr_bake', {}).get('status') == 'baked' and
                record['basic_pbr_bake'].get('closure_projection',True)):
            add_issue(record, 'pbr_closure_projection',
                      '源闭包投影到单一 PBR；高光分布及分层法线需要复核', severity='approximation')
        for effect in record.get('basic_pbr_bake', {}).get('omitted_effects', []):
            add_issue(record, 'pbr_effect_omitted', effect, severity='approximation', channel=effect)
        record['conversion_status'] = ('approximated' if any(
            item['severity'] == 'approximation' for item in record['issues']) else 'converted')
    record['visual_status'] = 'needs_review' if record.get('requires_visual_review') else 'not_run'
    return record
