"""Host-independent, strict material identity validation."""
from collections import Counter


def validate_character_slots(imported_names, records):
    expected = [record['slot_name'] for record in records]
    actual = list(imported_names)
    if not expected or len(set(expected)) != len(expected):
        raise RuntimeError('原始角色材质槽清单为空或包含重复身份，不能安全分配材质')
    missing = list((Counter(expected) - Counter(actual)).elements())
    extra = list((Counter(actual) - Counter(expected)).elements())
    if missing or extra:
        raise RuntimeError(f'角色材质槽丢失、合并或重复：预期 {len(expected)}，实际 {len(actual)}；'
                           f'缺少 {missing}；额外 {extra}')
    return {'expected': len(expected), 'actual': len(actual), 'preserved': True,
            'import_route': 'FBX', 'flatten_tracks': 'not_applicable'}
