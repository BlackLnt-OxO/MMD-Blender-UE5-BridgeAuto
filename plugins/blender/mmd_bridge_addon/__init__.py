bl_info = {"name":"MMD Bridge", "author":"MMD Bridge", "version":(0,1,0), "blender":(4,3,0),
           "category":"Import-Export", "location":"3D View > Sidebar > MMD Bridge"}
import bpy
from bpy_extras.io_utils import ImportHelper


class MMD_BRIDGE_OT_job(bpy.types.Operator,ImportHelper):
    bl_idname = "mmd_bridge.load_job"
    bl_label = "打开桌面工具任务"
    filename_ext = ".json"
    filter_glob: bpy.props.StringProperty(default="job.json",options={"HIDDEN"})
    def execute(self,context):
        import json,sys
        from pathlib import Path
        path = Path(self.filepath)
        data = json.loads(path.read_text(encoding="utf-8"))
        root = Path(data["resource_root"])
        for folder in (root,root/"vendor/blender_mmd_tools-4.5.13",root/"vendor/python"):
            sys.path.insert(0,str(folder))
        from mmd_bridge.blender_host import main
        main([str(path.parent),"character"])
        return {"FINISHED"}


class MMD_BRIDGE_PT_panel(bpy.types.Panel):
    bl_label = "MMD Bridge"
    bl_idname = "MMD_BRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MMD Bridge"
    def draw(self,context):
        self.layout.label(text="在桌面工具中选择素材与参数")
        self.layout.operator("mmd_bridge.load_job")


def register():
    bpy.utils.register_class(MMD_BRIDGE_OT_job)
    bpy.utils.register_class(MMD_BRIDGE_PT_panel)


def unregister():
    bpy.utils.unregister_class(MMD_BRIDGE_PT_panel)
    bpy.utils.unregister_class(MMD_BRIDGE_OT_job)
