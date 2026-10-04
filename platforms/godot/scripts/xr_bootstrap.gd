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

## ``XRInterface.EnvironmentBlendMode``, by the value order the engine defines
## (project.godot states the same order for its openxr/environment_blend_mode
## setting): 0 = Opaque, 1 = Additive, 2 = Alpha Blend. Numeric here because the
## value is *read back* from the runtime and compared, and the measured run below
## confirms the numbering against the engine's own "Found environmental blend
## mode XR_ENVIRONMENT_BLEND_MODE_OPAQUE" line rather than trusting a recall.
const BLEND_OPAQUE := 0
const BLEND_ADDITIVE := 1
const BLEND_ALPHA := 2

var mode: String = "unavailable"
var _xr_interface: XRInterface
var _watching := false


func _ready() -> void:
	_xr_interface = XRServer.find_interface("OpenXR")
	if _xr_interface == null:
		print("[xr] no OpenXR interface is compiled in — unavailable")
		_set_mode("unavailable")
		return
	if DisplayServer.get_name() == "headless":
		# A headless engine has no swapchain to hand the compositor, so no XR session can
		# begin however present the runtime is: measured, session creation fails with
		# XR_ERROR_GRAPHICS_REQUIREMENTS_CALL_MISSING under --headless, while the same scene
		# without the flag reaches XR_SESSION_STATE_FOCUSED and renders to the headset.
		# Saying so at once is both the truth and cheaper than watching for a session that
		# cannot start -- and it is what lets the scripted transcript settle instead of
		# recording the placeholder this started as.
		print("[xr] headless display: no XR session is possible — desktop preview")
		_set_mode("desktop_preview")
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
	var blend := _observed_blend_mode()
	print("[xr] adopting the running OpenXR session — presence is xr (blend=%s)"
			% _blend_name(blend))
	# Transparency only where the runtime actually granted it. The compositor blends
	# alpha in ALPHA_BLEND and does nothing with it otherwise, so asking for a
	# transparent background under OPAQUE leaves the framebuffer alpha-0 with nothing
	# to composite it: the desktop mirror renders black while the headset still shows
	# the Link environment behind the scene. That difference reads like a scene bug
	# and is really a mode the runtime never offered -- so it is read, not assumed.
	# Order still matters (transparent_bg BEFORE use_xr) for the case where alpha *is*
	# granted: the swapchain must be created transparent from the first frame.
	var cfg: ShugoCoreConfig = ShugoCoreConfig.load_config()
	# Transparency is a choice, not a default. It is what lets the headset show the room
	# through the scene, and it is what makes the *desktop* mirror window black -- a
	# mirror has nothing behind it to blend that alpha against. Measured with a headset
	# on Quest Link: the runtime reports alpha_blend, the headset showed the room, and
	# the desktop window was black. `transparent_background` decides which reading you
	# get, and the decision is printed so the window is never unexplained.
	var transparency := blend == BLEND_ALPHA and cfg.transparent_background
	print("[xr] background: transparent=%s (blend=%s, transparent_background=%s)"
			% [transparency, _blend_name(blend), cfg.transparent_background])
	get_viewport().transparent_bg = transparency
	get_viewport().use_xr = true
	_set_mode("xr")
	_start_passthrough()


func _observed_blend_mode() -> int:
	## What the runtime will do with the framebuffer's alpha, or -1 when it says
	## nothing. Read from the interface, never assumed from the project setting: the
	## setting is what we *ask* for, and over Quest Link the answer has been "opaque".
	if _xr_interface == null:
		return -1
	if not ("environment_blend_mode" in _xr_interface):
		return -1
	return int(_xr_interface.environment_blend_mode)


func _blend_name(blend: int) -> String:
	match blend:
		BLEND_OPAQUE:
			return "opaque"
		BLEND_ADDITIVE:
			return "additive"
		BLEND_ALPHA:
			return "alpha_blend"
		_:
			return "not reported"


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
	if not obj.has_method("start_passthrough"):
		# The singleton exists but does not expose the call. Measured on the Meta
		# runtime over Quest Link: `Invalid call. Nonexistent function
		# 'start_passthrough (via call)' in base 'OpenXRFbPassthroughExtension'` --
		# raised as a SCRIPT ERROR on every session, which said "something went
		# wrong" without saying that passthrough was unavailable. An extension that
		# cannot be started is a fact about this runtime, and is reported as one.
		print("[xr] passthrough: singleton has no start_passthrough() — not started")
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
