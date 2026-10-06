"""Material semantics, conversion and resource planning without host APIs.

Repetition controls resource reuse, not the meaning of a surface. A conversion
failure may change representation only through the explicit fallback plan.
"""

PLAN_VERSION = 1
DIRECT_ROUTES = {'nodes', 'shared_lookup', 'mesh_data', 'instance_data'}
LIVE_DOMAINS = {'view', 'ray', 'light', 'time'}


def field_route(capability, domains, *, native_normal=False):
    """An unsupported UE operator may still be a safe native static bake.

    BOX/UDIM image projection needs a representation adapter, unlike movies,
    ray switches or scripts whose value cannot be frozen safely.
    """
    if capability.get('route') in DIRECT_ROUTES:
        return capability['route']
    blocked=set(domains) & (LIVE_DOMAINS | {'unknown'})
    if not native_normal:blocked |= set(domains) & {'derivative'}
    return 'blocked' if blocked else 'local_cache'


def plan_material(facts, *, requested='auto', repeat_count=1, category='small'):
    facts = dict(facts)
    features = set(facts.get('features', []))
    domains = set(facts.get('domains', []))
    fields = list(facts.get('cache_fields', []))
    protected = sorted(features & {'layers', 'coat', 'subsurface', 'sheen',
                                  'anisotropy', 'transmission', 'refraction',
                                  'thin_transmission', 'displacement', 'volume'})
    resources = {
        'reuse': 'shared_semantic_fields' if repeat_count >= 8 else 'content_addressed',
        'repeat_count': int(repeat_count),
        'scope_domains': sorted(domains & {'uv', 'generated', 'object', 'world',
                                          'instance', 'island', 'vertex'}),
        'layout': 'tiled_fields' if category == 'tiled' else
                  'chunked_object_atlas' if category == 'chunked' else 'whole_object_atlas',
    }
    plan = {'version': PLAN_VERSION, 'facts': facts, 'resources': resources,
            'preserve': protected, 'cache_fields': fields, 'requested': requested,
            'losses': [], 'layout': resources['layout']}
    if facts.get('data_errors'):
        route, reason = 'blocked', '源坐标、法线或材质数据不可靠；必须修复数据后转换'
    elif 'time' in domains:
        route, reason = 'blocked', '有效图包含动画或驱动，禁止静态冻结'
    elif facts.get('blocked_fields') and not (
            not domains & {'time','light','unknown'} and
            (requested in ('bake','baked') or features & {'transmission','refraction','thin_transmission'})):
        route, reason = 'blocked', '字段依赖尚无实时适配，不能烘焙为静态贴图'
    elif requested in ('bake', 'baked'):
        route, reason = 'pbr_bake', '用户显式指定完整 PBR；有损投影必须记录'
        plan['losses'] = protected + sorted(domains & LIVE_DOMAINS)
    elif requested == 'nodes':
        route, reason = 'nodes', '用户指定节点转换；不启动烘焙兜底'
    elif facts.get('fully_adapted'):
        route, reason = 'nodes', '有效图可由已适配节点、共享查找表和源数据表达'
    elif protected or domains & LIVE_DOMAINS:
        route, reason = 'hybrid', '保留实时闭包及材质语义，只烘焙安全静态字段'
    elif category == 'tiled':
        route, reason = 'hybrid', '平铺表面保留坐标与纹理密度，复杂字段局部缓存'
    elif requested == 'hybrid' or len(fields) <= 2:
        route, reason = 'hybrid', '仅少数字段需要适配，优先局部烘焙'
    else:
        route, reason = 'pbr_bake', '单层静态不透明 PBR 的多个复杂字段整体烘焙'
    plan.update(route=route, reason=reason)
    return plan


def fallback_plan(plan, *, scope='complete_surface', local_attempted=False):
    """Never silently collapse a live/layered surface or discard coverage."""
    facts = plan.get('facts', {})
    domains = set(facts.get('domains', []))
    features = set(facts.get('features', []))
    coverage = bool(facts.get('coverage'))
    result = {'scope': scope, 'preserve_coverage': coverage, 'losses': [],
              'opaque_fallback': False, 'project_view_mix': False}
    if plan.get('requested') == 'nodes' or plan.get('route') == 'blocked':
        result.update(route='blocked', reason='强制节点或不可靠数据禁止自动整表面烘焙')
    elif not local_attempted and plan.get('route') != 'pbr_bake':
        result.update(route='local_fields', reason='先缓存安全静态字段，保留闭包、裁切与实时依赖')
    elif domains & {'time', 'light', 'unknown'}:
        result.update(route='blocked', reason='未知、动画或光照依赖不能可靠冻结')
    elif features & {'transmission', 'refraction', 'thin_transmission'}:
        result.update(route='pbr_bake', opaque_fallback=True, project_view_mix=True,
                      losses=['transmission', 'refraction'],
                      reason='透光还原不可靠，降级不透明着色；独立裁切仍保留')
    elif plan.get('preserve') or domains & LIVE_DOMAINS:
        result.update(route='blocked', reason='局部兜底失败，禁止静默固定实时混合或合并分层')
    else:
        result.update(route='pbr_bake', reason='静态普通 PBR 改用完整通道贴图')
    return result
