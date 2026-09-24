@tool
extends EditorPlugin

## ShugoCore XR editor plugin entry point (Track 3 scaffold).
## Intentionally minimal: the runtime behavior lives in autoloads
## (ShugoCoreBridge, XRBootstrap) registered by project.godot, so this
## plugin exists mainly to keep the addon discoverable in the editor.


func _enter_tree() -> void:
	print("ShugoCoreXR: addon loaded (scaffold)")


func _exit_tree() -> void:
	pass
