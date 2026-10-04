extends Node
## XR bootstrap (Track 3 scaffold).
##
## Initializes OpenXR once and reports the OBSERVED mode:
##   "xr"               — interface initialized and the viewport uses XR
##   "desktop_preview"  — no usable OpenXR runtime; flat preview
##   "unavailable"      — XRServer has no OpenXR interface compiled in
## It never fabricates headset state: a failed init is desktop preview,
## not a fake XR session.

signal mode_changed(mode: String)

## Frames to keep watching for a session before calling it a desktop preview.
## A Quest over Quest Link is not instant: in the measured run the OpenXR instance
## was created ~300 log lines before XR_SESSION_STATE_READY arrived.
const SETTLE_FRAMES := 240

var mode: String = "unavailable"
var _xr_interface: XRInterface
var _watching := false


func _ready() -> void:
	_xr_interface = XRServer.find_interface("OpenXR")
	if _xr_interface == null:
		print("[xr] no OpenXR interface is compiled in — unavailable")
		_set_mode("unavailable")
		return
	print("[xr] interface found; initialized=%s" % [_xr_interface.is_initialized()])
	# IDEMPOTENT: this script is registered BOTH as the XRBootstrap autoload
	# and as the main scene root script, so _ready() runs twice. Calling
	# initialize() on an interface that is already initialized returns
	# false, which would falsely report "desktop_preview" even though a live
	# XR session exists. Adopt the running session instead of re-initializing.
	if _xr_interface.is_initialized():
		_adopt_running_session()
		return
	if _xr_interface.initialize():
		_adopt_running_session()
		return
	# "Not initialized yet" is not the same as "not available". On a Quest 3 over Quest
	# Link the engine reached XR_SESSION_STATE_FOCUSED well after the autoloads had run,
	# so concluding here reported desktop_preview while the operator was wearing the
	# headset and looking at this scene -- a false report in the one place this scaffold
	# promises not to make one. Keep observing instead, and still end at
	# desktop_preview when nothing arrives.
	_watch_for_session()


func _adopt_running_session() -> void:
	print("[xr] adopting the running OpenXR session — presence is xr")
	# Passthrough order matters: transparent_bg BEFORE use_xr so the
	# Alpha-blend swapchain is created transparent from the first frame.
	# Both this AND xr/openxr/environment_blend_mode=2 are required: the blend
	# mode is what makes the compositor treat alpha as real transparency, the
	# extension only adds the manifest privilege.
	get_viewport().transparent_bg = true
	get_viewport().use_xr = true
	_set_mode("xr")
	_start_passthrough()


func _watch_for_session() -> void:
	## Bounded: a session that has not begun by then is not going to.
	if _watching:
		return                  # the second _ready() must not race the first
	_watching = true
	if _xr_interface.has_signal("session_begun"):
		_xr_interface.session_begun.connect(_on_session_begun)
	for _frame in SETTLE_FRAMES:
		await get_tree().process_frame
		if _xr_interface != null and _xr_interface.is_initialized():
			_adopt_running_session()
			return
	# Never downgrade a session that was adopted meanwhile (the two-watcher case).
	if mode != "xr":
		print("[xr] no session began within %d frames — desktop preview" % SETTLE_FRAMES)
		_set_mode("desktop_preview")


func _on_session_begun() -> void:
	if _xr_interface != null and _xr_interface.is_initialized():
		_adopt_running_session()


func _start_passthrough() -> void:
	## Starts FB passthrough via the vendors GDExtension singleton.
	## Honest fallback: if the singleton/class is absent (desktop, plugin
	## missing), we log the observation and continue — never fake a mode.
	if not Engine.has_singleton("OpenXRFbPassthroughExtension"):
		print("[xr] passthrough: OpenXRFbPassthroughExtension singleton not available — not started")
		return
	var pt: Variant = Engine.get_singleton("OpenXRFbPassthroughExtension")
	if pt == null or not (pt is Object):
		print("[xr] passthrough: singleton null — not started")
		return
	var obj := pt as Object
	if obj.has_method("is_passthrough_started") and obj.call("is_passthrough_started"):
		print("[xr] passthrough: already started")
		return
	obj.call("start_passthrough")
	var started: bool = obj.call("is_passthrough_started") if obj.has_method("is_passthrough_started") else false
	print("[xr] passthrough: start_passthrough() -> observed started=%s" % [started])


func _set_mode(new_mode: String) -> void:
	if mode == new_mode:
		return
	mode = new_mode
	mode_changed.emit(mode)


func is_xr() -> bool:
	return mode == "xr"


func get_controllers() -> Array[XRController3D]:
	## Real tracked controllers only — an empty array means none seen.
	var found: Array[XRController3D] = []
	if not is_xr():
		return found
	for tracker in XRServer.get_trackers(XRServer.TRACKER_HAND):
		var node := tracker as XRController3D
		if node != null:
			found.append(node)
	return found
