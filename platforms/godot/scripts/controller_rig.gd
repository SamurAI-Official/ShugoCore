extends Node3D
## Controller rig (Phase 0/2 driver).
##
## Phase 0: actively consumes Oculus Touch input every frame (grip/trigger/
## aim action sync) so the runtime has a live input consumer — the app
## previously bound zero controllers, and the OS reported
## "device 0 (state=Disconnected)" while the system fell back to hands mode.
## Connection/tracking transitions are printed so `logcat -s godot` shows
## exactly when a hand drops.
##
## Phase 2: exposes grip/trigger signals the panel-grab system subscribes to.

signal grip_pressed(hand: String)
signal grip_released(hand: String)
signal trigger_pressed(hand: String)
signal trigger_released(hand: String)

const HANDS := ["left", "right"]

var _controllers: Dictionary = {}   # "left"/"right" -> XRController3D
var _was_tracked: Dictionary = {}   # "left"/"right" -> bool (last frame)
var _grip_down: Dictionary = {}
var _trigger_down: Dictionary = {}


func _ready() -> void:
	# Controllers live as siblings under XROrigin3D (sample pattern: the
	# runtime tracks them reliably there; nesting under this plain Node3D
	# correlated with disconnects). Find them via parent first, then
	# children (legacy layout), then the whole tree.
	var origin := get_parent()
	if origin != null:
		_collect_from(origin)
	if _controllers.is_empty():
		_collect_from(self)
	if _controllers.is_empty():
		for node in get_tree().get_nodes_in_group("xr_controller_rig"):
			if node != self:
				_collect_from(node)
				if not _controllers.is_empty():
					break
	if _controllers.is_empty():
		print("[rig] WARNING: no XRController3D siblings bound — input will not be consumed")
	grip_pressed.connect(_on_grip_feedback)
	trigger_pressed.connect(_on_trigger_feedback)


func _collect_from(root: Node) -> void:
	for child in root.get_children():
		if child is XRController3D:
			var t := String(child.tracker)
			var hand := "left" if t.contains("left") else "right"
			_controllers[hand] = child
			_was_tracked[hand] = false


func _on_grip_feedback(hand: String) -> void:
	var ctrl := controller(hand)
	if ctrl != null:
		ctrl.trigger_haptic_pulse("haptic", 0.0, 0.4, 0.05, 0.0)


func _on_trigger_feedback(hand: String) -> void:
	var ctrl := controller(hand)
	if ctrl != null:
		ctrl.trigger_haptic_pulse("haptic", 0.0, 0.3, 0.04, 0.0)


func _process(_delta: float) -> void:
	for hand: String in _controllers:
		var ctrl: XRController3D = _controllers[hand]
		if ctrl == null:
			continue
		# Tracking-state transitions (honest observer; never fabricates state).
		var tracked: bool = ctrl.get_has_tracking_data()
		if tracked != _was_tracked.get(hand, false):
			print("[rig] hand %s tracked=%s" % [hand, tracked])
			_was_tracked[hand] = tracked
			if tracked:
				# Small pulse when a controller comes alive: user feedback +
				# keeps the input/haptic path exercised (Phase 0 disconnect fix).
				ctrl.trigger_haptic_pulse("haptic", 0.0, 0.25, 0.04, 0.0)
		if not tracked:
			continue
		# Consume analog actions every frame — this is the live input sync
		# that keeps the controllers active as an input source.
		var grip := ctrl.get_float("grip")
		var trigger := ctrl.get_float("trigger")
		_update_button(hand, "grip", grip > 0.7, grip_pressed, grip_released)
		_update_button(hand, "trigger", trigger > 0.7, trigger_pressed, trigger_released)


func _update_button(hand: String, name: String, is_down: bool,
		pressed_sig: Signal, released_sig: Signal) -> void:
	var was: bool = (_grip_down if name == "grip" else _trigger_down).get(hand, false)
	if is_down and not was:
		(_grip_down if name == "grip" else _trigger_down)[hand] = true
		pressed_sig.emit(hand)
	elif not is_down and was:
		(_grip_down if name == "grip" else _trigger_down)[hand] = false
		released_sig.emit(hand)


func controller(hand: String) -> XRController3D:
	return _controllers.get(hand)


func get_grab_state(hand: String) -> float:
	var ctrl := controller(hand)
	if ctrl == null or not ctrl.get_has_tracking_data():
		return 0.0
	return ctrl.get_float("grip")


func get_aim_transform(hand: String) -> Transform3D:
	## Aim pose if the runtime exposes it via this tracker, else grip pose.
	var ctrl := controller(hand)
	if ctrl == null:
		return Transform3D.IDENTITY
	return ctrl.global_transform