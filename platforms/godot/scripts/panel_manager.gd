extends Node3D
## Grabbable agent-information panels (Phase 3).
##
## A panel exists ONLY for a real payload read from ShugoCoreBridge — the
## script never invents a row and never claims tracking the data does not
## support:
##   * "agent" panel        <- /api/v1/status  (mesh role, baseline, cycles)
##   * one panel per node   <- /api/v1/fleet   (device_id, alive, paired_by)
##
## DEVICE LINKING (honest): an Android node panel anchors at the pose that
## node ITSELF publishes in its fleet manifest under "xr_pose", formatted
## "x,y,z" or "x,y,z,yaw_deg" (metres / degrees, XR world space). Nodes that
## publish no pose get a panel on the placement arc in front of the operator
## instead. A missing pose is never guessed into a fake "over the device"
## position.
##
## GRAB: grip drives it, reusing the same action names the controller rig
## consumes. Grip near a panel picks the nearest one in reach; releasing
## leaves it where you let go. Nothing is moved without a real grip event.

signal panel_created(panel_id: String)
signal panel_removed(panel_id: String)
signal panel_grabbed(panel_id: String, hand: String)
signal panel_released(panel_id: String, hand: String)

const MAX_PANELS := 24
const MAX_PANEL_LINES := 6
const MAX_MANIFEST_ROWS := 3
const GRAB_MAX_DISTANCE := 0.85
const GRAB_HOLD_DISTANCE := 0.36
const PLACEMENT_RADIUS := 0.95
const PLACEMENT_HEIGHT := 1.35
const PLACEMENT_ARC_STEP := 0.28
const PANEL_SIZE := Vector2(0.40, 0.28)
const POSE_KEY := "xr_pose"
const AGENT_PANEL_ID := "agent"

## Resolved from the scene; falls back to a tree search so an instanced
## manager still finds the rig.
@export var controller_rig_path: NodePath = ^"../XROrigin3D/ControllerRig"

var _panels: Dictionary = {}       # panel_id -> Node3D
var _grabbed: Dictionary = {}      # hand -> panel_id
var _bridge: Node = null
var _rig: Node = null
var _placement_index: int = 0


func _ready() -> void:
	_bridge = get_node_or_null("/root/ShugoCoreBridge")
	if _bridge != null:
		_bridge.fleet_changed.connect(_on_fleet_changed)
		_bridge.agent_status_changed.connect(_on_agent_status)
	_bind_rig()
	# Seed from whatever the bridge has already observed (a late-joining
	# manager must not wait a full poll cycle to show real state).
	if _bridge != null:
		if not _bridge.last_fleet.is_empty():
			_on_fleet_changed(_bridge.last_fleet)
		if not _bridge.last_status.is_empty():
			_on_agent_status(_bridge.last_status)
	# Offline seed: the bridge may still be polling, so show an honest
	# offline agent panel rather than leaving the operator with only the
	# legacy "Shugo - Idle" label.
	if not _panels.has(AGENT_PANEL_ID):
		_upsert_panel(AGENT_PANEL_ID, "SHUGO AGENT",
				["mesh role: —", "link: offline (polling)"], null)


func _bind_rig() -> void:
	_rig = get_node_or_null(controller_rig_path)
	if _rig == null:
		for node in get_tree().get_nodes_in_group("xr_controller_rig"):
			_rig = node
			break
	if _rig == null:
		push_warning("[panels] controller rig not found — panels render but cannot be grabbed")
		return
	_rig.grip_pressed.connect(_on_grip_pressed)
	_rig.grip_released.connect(_on_grip_released)


func _exit_tree() -> void:
	if _rig != null and _rig.grip_pressed.is_connected(_on_grip_pressed):
		_rig.grip_pressed.disconnect(_on_grip_pressed)
		_rig.grip_released.disconnect(_on_grip_released)


# -- bridge callbacks -------------------------------------------------------

func _on_fleet_changed(payload: Dictionary) -> void:
	## Reconcile the panel set against the registry: nodes present in the
	## payload get/refresh a panel, nodes that vanished lose theirs. A panel
	## outliving its node would be fabricated state.
	var wanted: Dictionary = {}
	var nodes: Variant = payload.get("nodes", [])
	if nodes is Array:
		for node in nodes:
			if not (node is Dictionary):
				continue
			var device_id := str(node.get("device_id", "")).strip_edges()
			if device_id == "":
				continue
			wanted[device_id] = node
			if _panels.size() >= MAX_PANELS and not _panels.has(device_id):
				continue
			_upsert_node_panel(device_id, node)
	for existing: String in _panels.keys():
		if existing == AGENT_PANEL_ID or wanted.has(existing):
			continue
		_remove_panel(existing)


func _on_agent_status(status: Dictionary) -> void:
	_upsert_agent_panel(status)


# -- panel construction -----------------------------------------------------

func _upsert_node_panel(device_id: String, node: Dictionary) -> void:
	## `alive` and `paired_by` are rendered verbatim — the panel states what
	## the registry says, including a node that is not alive.
	var lines: Array[String] = []
	var alive := bool(node.get("alive", false))
	lines.append("state: %s" % ("alive" if alive else "not alive"))
	var paired_by := str(node.get("paired_by", ""))
	if paired_by != "":
		lines.append("paired by: %s" % paired_by)
	var expires: Variant = node.get("expires_at")
	if expires != null and str(expires) != "":
		lines.append("expires: %s" % str(expires))
	lines.append_array(_manifest_lines(node.get("manifest", {})))
	# Explicit type: _parse_pose() returns Variant by design (null when the
	# node published no pose), and this project treats inference-from-
	# Variant as an error.
	var pose: Variant = _parse_pose(node.get("manifest", {}))
	_upsert_panel(device_id, "DEVICE %s" % _short_id(device_id),
			lines, pose)


func _upsert_agent_panel(status: Dictionary) -> void:
	var lines: Array[String] = []
	var role := str(status.get("mesh_role", ""))
	lines.append("mesh role: %s" % (role if role != "" else "—"))
	var baseline: Variant = status.get("security_baseline")
	if baseline is Dictionary:
		lines.append("security: %s" % ("ok" if bool(
				baseline.get("baseline_ok", false)) else "drift"))
	var agent_block: Variant = status.get("agent")
	if agent_block is Dictionary:
		var loop: Variant = agent_block.get("loop")
		if loop is Dictionary:
			lines.append("cycles: %s" % str(loop.get("cycles", "?")))
	var online := bool(_bridge.online) if _bridge != null else false
	lines.append("link: %s" % ("online" if online else "offline"))
	_upsert_panel(AGENT_PANEL_ID, "SHUGO AGENT", lines, null)


func _manifest_lines(manifest: Variant) -> Array[String]:
	## Bounded: at most MAX_MANIFEST_ROWS rows, each truncated, so a hostile
	## or huge manifest cannot exhaust headset memory or the panel.
	var out: Array[String] = []
	if not (manifest is Dictionary):
		return out
	for key: String in manifest.keys():
		if out.size() >= MAX_MANIFEST_ROWS:
			break
		if key == POSE_KEY:
			continue
		out.append("%s: %s" % [key.substr(0, 24),
				str(manifest[key]).substr(0, 32)])
	return out


func _parse_pose(manifest: Variant) -> Variant:
	## Returns a Transform3D when the node published a parseable pose, else
	## null. Accepts "x,y,z" or "x,y,z,yaw_deg". An unparseable value is
	## reported absent rather than defaulted to the origin.
	if not (manifest is Dictionary):
		return null
	var raw := str(manifest.get(POSE_KEY, "")).strip_edges()
	if raw == "":
		return null
	var parts := raw.split(",")
	if parts.size() < 3 or parts.size() > 4:
		return null
	var values: Array[float] = []
	for part in parts:
		var text := part.strip_edges()
		if not text.is_valid_float():
			push_warning("[panels] %s: non-numeric component '%s'" % [
					POSE_KEY, text])
			return null
		values.append(text.to_float())
	var origin := Vector3(values[0], values[1], values[2])
	var yaw := deg_to_rad(values[3]) if values.size() == 4 else 0.0
	return Transform3D(Basis(Vector3.UP, yaw), origin)


func _short_id(device_id: String) -> String:
	## Panels are small: show the tail of the id, which is the part that
	## actually distinguishes two devices of the same model.
	return device_id.substr(maxi(0, device_id.length() - 12), 12)


func _upsert_panel(panel_id: String, title: String, lines: Array[String],
		pose: Variant) -> void:
	## Create the panel on first sighting, then only refresh its text: a
	## panel the operator has grabbed and moved must not snap back just
	## because the next poll returned the same node.
	var panel: Node3D = _panels.get(panel_id)
	if panel == null:
		panel = _make_panel(panel_id)
		_panels[panel_id] = panel
		if pose is Transform3D:
			panel.global_transform = pose
			print("[panels] anchored %s at pose %s" % [panel_id, pose])
		else:
			_place_on_arc(panel)
			print("[panels] %s no pose -> arc" % [panel_id])
		panel_created.emit(panel_id)
	var label := panel.get_node_or_null("Text") as Label3D
	if label != null:
		label.text = _compose_text(title, lines)


func _compose_text(title: String, lines: Array[String]) -> String:
	var out: Array[String] = [title]
	for i in mini(lines.size(), MAX_PANEL_LINES):
		out.append(lines[i])
	return "\n".join(out)


func _make_panel(panel_id: String) -> Node3D:
	var panel := Node3D.new()
	panel.name = "Panel_%s" % panel_id.replace("/", "_").substr(0, 32)
	var mesh := MeshInstance3D.new()
	mesh.name = "Backdrop"
	var quad := QuadMesh.new()
	quad.size = PANEL_SIZE
	mesh.mesh = quad
	mesh.material_override = _backdrop_material()
	panel.add_child(mesh)
	var text := Label3D.new()
	text.name = "Text"
	text.font_size = 64
	text.pixel_size = PANEL_SIZE.x * 0.018
	text.no_depth_test = true
	text.position = Vector3(0, 0, 0.002)
	panel.add_child(text)
	# Grab target: StaticBody3D with a thin box so controller ray/grip
	# proximity has a physical volume to hit. Size matches the quad.
	var body := StaticBody3D.new()
	body.name = "GrabBody"
	var shape := CollisionShape3D.new()
	shape.name = "GrabShape"
	var box := BoxShape3D.new()
	box.size = Vector3(PANEL_SIZE.x, PANEL_SIZE.y, 0.03)
	shape.shape = box
	body.add_child(shape)
	panel.add_child(body)
	add_child(panel)
	return panel


func _backdrop_material() -> StandardMaterial3D:
	## Translucent darkened panel: readable over the passthrough camera feed
	## without hiding it (alpha, unshaded, visible from both sides).
	var mat := StandardMaterial3D.new()
	mat.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mat.albedo_color = Color(0.05, 0.07, 0.10, 0.72)
	mat.cull_mode = BaseMaterial3D.CULL_DISABLED
	return mat


func _place_on_arc(panel: Node3D) -> void:
	## Deterministic placement in front of the operator, fanned so panels do
	## not stack. Used only when the node published no pose.
	var basis := Basis.IDENTITY
	var anchor := Vector3(0, PLACEMENT_HEIGHT, -PLACEMENT_RADIUS)
	var camera := get_viewport().get_camera_3d()
	if camera != null:
		var cam_basis := camera.global_transform.basis
		basis = Basis(Vector3.UP, cam_basis.get_euler().y)
		anchor = camera.global_position + basis * Vector3(0, 0, -PLACEMENT_RADIUS)
	var lane := float(_placement_index % 4) - 1.5
	_placement_index += 1
	panel.global_transform = Transform3D(basis,
			anchor + basis * Vector3(lane * PLACEMENT_ARC_STEP, 0, 0))
	_face_camera(panel)


func _face_camera(panel: Node3D) -> void:
	## Panels are read, so they face the operator unless the operator is
	## inside them (which would make look_at unstable).
	var camera := get_viewport().get_camera_3d()
	if camera == null:
		return
	var to_camera := camera.global_position - panel.global_position
	if to_camera.length_squared() < 0.0001:
		return
	panel.look_at(camera.global_position, Vector3.UP)


# -- grab handling ----------------------------------------------------------

func _on_grip_pressed(hand: String) -> void:
	if _rig == null or _panels.is_empty():
		return
	var origin: Vector3 = _rig.get_aim_transform(hand).origin
	var best_id := ""
	var best_distance := GRAB_MAX_DISTANCE
	for panel_id: String in _panels:
		var panel: Node3D = _panels[panel_id]
		if panel == null:
			continue
		var distance := origin.distance_to(panel.global_position)
		if distance < best_distance:
			best_distance = distance
			best_id = panel_id
	if best_id == "":
		return
	_grabbed[hand] = best_id
	panel_grabbed.emit(best_id, hand)


func _on_grip_released(hand: String) -> void:
	if not _grabbed.has(hand):
		return
	var panel_id: String = _grabbed[hand]
	_grabbed.erase(hand)
	panel_released.emit(panel_id, hand)


func _process(_delta: float) -> void:
	## Held panels ride the controller that grabbed them. Nothing moves
	## without a live grip + a live tracker.
	if _grabbed.is_empty() or _rig == null:
		return
	for hand: String in _grabbed.keys():
		var panel: Node3D = _panels.get(_grabbed[hand])
		if panel == null:
			continue
		var aim: Transform3D = _rig.get_aim_transform(hand)
		panel.global_position = aim.origin - aim.basis.z * GRAB_HOLD_DISTANCE
		_face_camera(panel)


func _remove_panel(panel_id: String) -> void:
	for hand: String in _grabbed.keys():
		if _grabbed[hand] == panel_id:
			_grabbed.erase(hand)
	var panel: Node3D = _panels.get(panel_id)
	if panel == null:
		return
	_panels.erase(panel_id)
	panel.queue_free()
	panel_removed.emit(panel_id)


# -- honest readers ---------------------------------------------------------

func panel_count() -> int:
	return _panels.size()


func panel_ids() -> Array:
	return _panels.keys()


func held_by(hand: String) -> String:
	return str(_grabbed.get(hand, ""))
