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

var mode: String = "unavailable"
var _xr_interface: XRInterface


func _ready() -> void:
	_xr_interface = XRServer.find_interface("OpenXR")
	if _xr_interface == null:
		_set_mode("unavailable")
		return
	# IDEMPOTENT: this script is registered BOTH as the XRBootstrap autoload
	# and as the main scene root script, so _ready() runs twice. Calling
	# initialize() on an interface that is already initialized returns
	# false, which would falsely report "desktop_preview" even though a live
	# XR session exists. Adopt the running session instead of re-initializing.
	if _xr_interface.is_initialized():
		# Passthrough order matters: transparent_bg BEFORE use_xr so the
		# Alpha-blend swapchain is created transparent from the first frame.
		get_viewport().transparent_bg = true
		get_viewport().use_xr = true
		_set_mode("xr")
		_start_passthrough()
		return
	if _xr_interface.initialize():
		# Passthrough: alpha-0 framebuffer so the Meta passthrough layer
		# (xr/openxr/extensions/meta/passthrough) shows through renders.
		# Both this AND xr/openxr/environment_blend_mode=2 are required: the
		# blend mode is what makes the compositor treat alpha as real
		# transparency, the extension only adds the manifest privilege.
		get_viewport().transparent_bg = true
		get_viewport().use_xr = true
		_set_mode("xr")
		_start_passthrough()
	else:
		_set_mode("desktop_preview")


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
