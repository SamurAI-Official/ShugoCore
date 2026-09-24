extends Node3D
class_name ConsolePanel

## Spatial operator-console mount (Phase 3).
##
## The SAME `ShugoConsole` Control used flat in desktop preview is hosted
## inside a `SubViewport` + textured quad in XR — one UI, two renderings.
## This script uses only Godot core nodes (no vendor imports), following
## the Viewport2Din3D interaction pattern: controller rays are projected
## to viewport UVs and forwarded as mouse events.
##
## Interaction sources register themselves via add_pointer(); each frame,
## pointer owners push (origin, direction, pressed) and the panel forwards
## them as viewport input.

var viewport: SubViewport
var quad: MeshInstance3D
var console: Control

@export var console_scene: PackedScene
@export var viewport_px := Vector2i(900, 560)
@export var pixel_size := 0.0016
@export var interactable := true

var _pointers: Dictionary = {}
var _press_state: Dictionary = {}


func _ready() -> void:
	viewport = SubViewport.new()
	viewport.name = "ConsoleViewport"
	viewport.size = viewport_px
	viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	viewport.own_world_3d = true
	add_child(viewport)
	var res: PackedScene = console_scene
	if res == null:
		res = load("res://scenes/operator_console.tscn")
	console = res.instantiate()
	viewport.add_child(console)
	quad = MeshInstance3D.new()
	quad.name = "ConsoleQuad"
	var mesh := QuadMesh.new()
	mesh.size = Vector2(viewport_px) * pixel_size
	quad.mesh = mesh
	var mat := StandardMaterial3D.new()
	mat.albedo_texture = viewport.get_texture()
	mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mat.no_depth_test = false
	quad.material_override = mat
	add_child(quad)


## Register a pointer node; call push_pointer() each frame for it.
func add_pointer(key: String) -> void:
	_pointers[key] = true
	_press_state[key] = false


func remove_pointer(key: String) -> void:
	_pointers.erase(key)
	_press_state.erase(key)


## Feed one XR pointer. `pressed` follows the controller trigger state.
func push_pointer(key: String, origin: Vector3, direction: Vector3,
		pressed: bool) -> void:
	if not _pointers.has(key) or viewport == null:
		return
	var plane := Plane(Vector3(0, 0, 1).rotated(Vector3(0, 1, 0),
			global_rotation.y),
			global_position)
	var hit: Variant = plane.intersects_ray(origin, direction)
	if hit == null:
		_set_pressed(key, false)
		return
	var local: Vector3 = to_local(hit)
	var half := Vector2(viewport_px) * pixel_size * 0.5
	if absf(local.x) > half.x or absf(local.y) > half.y:
		_set_pressed(key, false)
		return
	var uv := Vector2(
		0.5 + local.x / (half.x * 2.0),
		0.5 - local.y / (half.y * 2.0))
	var pos := uv * Vector2(viewport_px)
	var motion := InputEventMouseMotion.new()
	motion.position = pos
	viewport.push_input(motion)
	_set_pressed(key, pressed)


func _set_pressed(key: String, pressed: bool) -> void:
	if _press_state.get(key, false) == pressed:
		return
	_press_state[key] = pressed
	var event := InputEventMouseButton.new()
	event.button_index = MOUSE_BUTTON_LEFT
	event.button_mask = MOUSE_BUTTON_MASK_LEFT
	event.pressed = pressed
	viewport.push_input(event)
