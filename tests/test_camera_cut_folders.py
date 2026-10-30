"""Generated camera grouping must preserve existing manual sequence organization."""
from types import SimpleNamespace as N

import pytest

from mmd_bridge import ue_camera_cut_baker as cut


class Folder:
    def __init__(self, name, bindings=()):
        self.name = name
        self.bindings = list(bindings)

    def get_folder_name(self):
        return self.name

    def get_child_object_bindings(self):
        return self.bindings[:]

    def add_child_object_binding(self, binding):
        if binding not in self.bindings:
            self.bindings.append(binding)
        return True


@pytest.mark.parametrize('existing_group', [False, True])
def test_only_new_shot_cameras_are_grouped_and_repeat_does_not_duplicate(monkeypatch, existing_group):
    source = N(get_id=lambda: 'source', get_parent=lambda: None)
    camera = N(get_id=lambda: 'shot', get_parent=lambda: None)
    component = N(get_id=lambda: 'lens', get_parent=lambda: camera)
    manual = Folder('手动镜头', [source])
    group = Folder('切分镜头')
    roots = [manual, group] if existing_group else [manual]

    def add_folder(name):
        folder = Folder(name)
        roots.append(folder)
        return folder

    actor_state = {'folder': '', 'transform': (55, 0, 120)}
    actor = N(set_folder_path=lambda name: actor_state.update(folder=name))
    record = {'name': 'AUTO_Cam_Shot_001', 'binding': camera, 'actor': actor}
    context = N(cfg={}, notes={}, sequence=N(
        get_root_folders_in_sequence=lambda: roots[:], add_root_folder_to_sequence=add_folder))
    monkeypatch.setattr(cut, 'log', lambda _: None)

    cut.organize_shot_cameras(context, [record])
    cut.organize_shot_cameras(context, [record])

    groups = [folder for folder in roots if folder.name == '切分镜头']
    assert len(groups) == 1
    assert groups[0].bindings == [camera]
    assert manual.bindings == [source]
    assert component.get_parent() is camera
    assert actor_state == {'folder': '切分镜头', 'transform': (55, 0, 120)}
    assert context.notes['camera_folder'] == {'name': '切分镜头', 'cameras': 1}


def test_empty_result_does_not_create_a_folder():
    cut.organize_shot_cameras(N(), [])


def test_rebake_removes_old_binding_references_from_nested_folders(monkeypatch):
    remaining = []

    class Guid:
        representations = 0

        def __init__(self, value):
            self.value = value

        def export_text(self):
            return self.value

        def __str__(self):
            # UE 5.8 str(Guid) contains the Python wrapper's changing address.
            Guid.representations += 1
            return f'<Struct Guid wrapper {Guid.representations}>'

    def binding(name, parent=None):
        item = N(get_name=lambda: name, get_id=lambda: Guid(name),
                 get_parent=lambda: parent, get_object_template=lambda: None)
        item.remove = lambda: remaining.remove(item)
        remaining.append(item)
        return item

    source = binding('Original')
    shot = binding('AUTO_Cam_Shot_001')
    lens = binding('CameraComponent', shot)
    nested = Folder('用户子文件夹', [source, shot, lens])
    nested.get_child_folders = lambda: []
    nested.remove_child_object_binding = nested.bindings.remove
    root = Folder('切分镜头')
    root.get_child_folders = lambda: [nested]
    context = N(cfg={'GENERATED_PREFIX': 'AUTO_Cam_Shot_', 'GENERATED_TAG': 'Generated'},
                notes={}, sequence=N(get_bindings=lambda: remaining[:],
                    get_root_folders_in_sequence=lambda: [root], get_tracks=lambda: []))
    monkeypatch.setattr(cut, '_actor_subsystem', lambda: None)
    monkeypatch.setattr(cut, 'all_level_actors', lambda: [])
    monkeypatch.setattr(cut, 'log', lambda _: None)

    cut.cleanup_generated(context)

    assert remaining == [source]
    assert nested.bindings == [source]
    assert root.get_child_folders() == [nested]
