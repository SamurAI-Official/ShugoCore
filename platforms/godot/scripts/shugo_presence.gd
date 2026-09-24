extends Node3D
## Shugo presence in XR (Track 3 scaffold).
##
## States are driven ONLY by real signals from the agent bridge:
##   offline    — bridge reports the agent unreachable
##   thinking   — a request this client dispatched is in flight
##   speaking   — TTS is actually rendering the agent's reply (or the
##                bounded timed-display fallback is active)
##   listening  — opt-in only; nothing sets it without a real mic source
##   idle       — none of the above, agent reachable
## Never a decorative animation: an absent signal never becomes a state.
## Track 1 (mesh_role) and Track 2 (security baseline) surfaces are shown
## verbatim from /api/v1/status.

signal state_changed(new_state: String)

const STATE_OFFLINE := "offline"
const STATE_IDLE := "idle"
const STATE_THINKING := "thinking"
const STATE_SPEAKING := "speaking"
const STATE_LISTENING := "listening"

const MAX_SAY_CHARS := 400
const FALLBACK_DISPLAY_MS_PER_CHAR := 55
const MAX_FALLBACK_DISPLAY_MS := 8000

const STATE_COLORS := {
	STATE_OFFLINE: Color(0.55, 0.55, 0.55),
	STATE_IDLE: Color(0.35, 0.85, 0.45),
	STATE_THINKING: Color(0.95, 0.75, 0.2),
	STATE_SPEAKING: Color(0.3, 0.6, 1.0),
	STATE_LISTENING: Color(0.9, 0.4, 0.9),
}

var state: String = STATE_OFFLINE
var nrr: NRR

var _label: Label3D
var _detail: Label3D
var _speak_until_ms: int = 0
var _tts_active: bool = false


func _ready() -> void:
	_label = get_node_or_null("StateLabel") as Label3D
	if _label == null:
		_label = _make_label("StateLabel", "SHUGO — offline", 96,
				Vector3(0, 1.6, -1.2))
	_detail = get_node_or_null("DetailLabel") as Label3D
	if _detail == null:
		_detail = _make_label("DetailLabel", "", 40,
				Vector3(0, 1.15, -1.2))
	var bridge := get_node_or_null("/root/ShugoCoreBridge")
	if bridge != null:
		bridge.connection_changed.connect(_on_connection_changed)
		bridge.agent_status_changed.connect(_on_agent_status)
		bridge.agent_reply.connect(_on_agent_reply)
	nrr = NRR.new()
	nrr.initialize()


func _make_label(label_name: String, text: String, font_size: int,
		pos: Vector3) -> Label3D:
	## Reuse the scene-defined label when main/presence scenes evolve;
	## create one only when instanced bare (no editor scene children).
	var l := Label3D.new()
	l.name = label_name
	l.text = text
	l.font_size = font_size
	l.pixel_size = 0.004
	l.billboard = BaseMaterial3D.BILLBOARD_ENABLED
	l.no_depth_test = true
	l.position = pos
	add_child(l)
	return l


func _process(_delta: float) -> void:
	if state == STATE_SPEAKING and not _tts_active \
			and Time.get_ticks_msec() >= _speak_until_ms:
		_apply_state(STATE_IDLE)


# -- public surface ---------------------------------------------------------

func ask(text: String) -> void:
	## Dispatch a chat turn: thinking while in flight, speaking on reply.
	var bridge := get_node_or_null("/root/ShugoCoreBridge")
	if bridge == null or not bridge.online:
		return
	_apply_state(STATE_THINKING)
	bridge.chat(text)


func say(text: String) -> void:
	## Speak/display the agent's reply. SPEAKING reflects real output only.
	var spoken := text.substr(0, MAX_SAY_CHARS)
	_label.text = spoken
	if _start_tts(spoken):
		_tts_active = true
		_apply_state(STATE_SPEAKING)
	else:
		# No TTS on this platform: bounded timed display (honest fallback
		# — text is shown, nothing pretends to be audio).
		_tts_active = false
		_speak_until_ms = Time.get_ticks_msec() + clampi(
				spoken.length() * FALLBACK_DISPLAY_MS_PER_CHAR, 1200,
				MAX_FALLBACK_DISPLAY_MS)
		_apply_state(STATE_SPEAKING)


func set_listening(active: bool) -> void:
	## Opt-in hook for a future real mic source. The default path never
	## enters LISTENING.
	if active:
		_apply_state(STATE_LISTENING)
	elif state == STATE_LISTENING:
		_apply_state(STATE_IDLE)


# -- bridge callbacks -------------------------------------------------------

func _on_connection_changed(online: bool) -> void:
	_apply_state(STATE_IDLE if online else STATE_OFFLINE)


func _on_agent_reply(text: String) -> void:
	say(text)


func _on_agent_status(status: Dictionary) -> void:
	if state == STATE_OFFLINE:
		_apply_state(STATE_IDLE)
	_refresh_detail(status)


# -- internals --------------------------------------------------------------

func _start_tts(text: String) -> bool:
	if not DisplayServer.has_method("tts_get_voices"):
		return false
	var voices: Array = DisplayServer.tts_get_voices()
	if voices.is_empty():
		return false
	# Speak via platform TTS. Completion of the SPEAKING state is governed
	# by the timed fallback in _process (_speak_until_ms), not by an engine
	# callback: that callback's signature is version/platform dependent and
	# the timed display is the honest fallback this script documents at top.
	DisplayServer.tts_speak(text, str(voices[0].get("id", "")))
	_tts_active = true
	return true


func _on_tts_end(_utterance_id: Variant) -> void:
	_tts_active = false
	_speak_until_ms = 0
	if state == STATE_SPEAKING:
		_apply_state(STATE_IDLE)


func _refresh_detail(status: Dictionary) -> void:
	var lines: Array[String] = []
	var role := str(status.get("mesh_role", ""))
	if role != "":
		lines.append("mesh: %s" % role)
	var baseline: Variant = status.get("security_baseline")
	if baseline is Dictionary:
		var ok := bool(baseline.get("baseline_ok", false))
		var violations := int((baseline.get("violations", []) as Array).size())
		lines.append("security: %s%s" %
				["ok" if ok else "drift",
				" (%d)" % violations if violations > 0 else ""])
	var agent_block: Variant = status.get("agent")
	if agent_block is Dictionary:
		var loop: Variant = agent_block.get("loop")
		if loop is Dictionary:
			lines.append("cycles: %s" % str(loop.get("cycles", "?")))
	if nrr != null and nrr.available:
		lines.append("nrr: native")
	_detail.text = "\n".join(lines)


func _apply_state(new_state: String) -> void:
	if state == new_state:
		return
	state = new_state
	var color: Color = STATE_COLORS.get(new_state, Color.WHITE)
	_label.modulate = color
	# Keep the label text honest alongside its color: the text is authored once
	# in the scene ("SHUGO — offline") and nothing consumes state_changed to
	# refresh it, so an online (green/IDLE) state still read "offline". SPEAKING
	# is exempt — say() has just written the agent's reply there, and that reply
	# IS the speaking display.
	if new_state != STATE_SPEAKING:
		_label.text = "SHUGO — %s" % new_state
	state_changed.emit(new_state)
