from mmd_bridge.scene_appearance import classify_appearance_reviews


def test_imported_fog_environment_and_light_are_not_skips():
    scene={'environment':{'surface':True},'volumes':[{'name':'World','status':'converted'},
        {'name':'Missing','status':'needs_review'}], 'needs_review':[
        {'stage':'volume','name':'World','reason':'phase approximation'},
        {'stage':'volume','name':'Missing','reason':'unsupported field'},
        {'stage':'environment','name':'World','reason':'ray approximation'},
        {'stage':'light','name':'Point','reason':'angular shader'}]}
    imported,skipped=classify_appearance_reviews(scene)
    assert [x['name'] for x in imported]==['World','World','Point']
    assert [x['name'] for x in skipped]==['Missing']


def test_failed_world_is_still_reported_as_skipped():
    scene={'environment':{'status':'needs_review'},'needs_review':[
        {'stage':'environment','name':'World','reason':'invalid HDR'}]}
    imported,skipped=classify_appearance_reviews(scene)
    assert not imported and skipped[0]['reason']=='invalid HDR'
