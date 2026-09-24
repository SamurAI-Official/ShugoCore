extends Control
class_name ShugoConsole

## ShugoCore operator console (Phase 3) — the Quest 3 UI-device surface.
##
## CONTRACT (Track 3 honesty rules, carried forward):
## - Pure `Control` nodes only: renders flat in desktop preview AND inside
##   a spatial panel (Viewport2Din3D pattern) in XR. No XR nodes here.
## - Every value comes from the bridge's last real payload. An absent
##   surface renders its own explicit state — never an empty list
##   pretending to be "zero items", never a fabricated zero.
## - Rows are bounded upstream by the bridge caps, so a hostile registry
##   cannot exhaust headset memory.
##
## Tabs: Node (mesh/security), Fleet, Approvals, Task, Settings.

const TITLE_FONT := 30
const BODY_FONT := 24
const SMALL_FONT := 20

const C_OK := Color(0.35, 0.85, 0.45)
const C_BAD := Color(0.95, 0.45, 0.4)
const C_MUTE := Color(0.55, 0.55, 0.55)

var _bridge: Node = null

var _node_state: Label
var _node_detail: Label
var _fleet_box: VBoxContainer
var _fleet_note: Label
var _appr_box: VBoxContainer
var _appr_note: Label
var _task_type: LineEdit
var _task_content: LineEdit
var _task_note: Label
var _set_url: LineEdit
var _set_token: LineEdit
var _set_poll: LineEdit
var _set_info: Label
var _set_saved: Label


func _ready() -> void:
	custom_minimum_size = Vector2(900, 560)
	_bridge = get_node_or_null("/root/ShugoCoreBridge")
	_build()
	if _bridge != null:
		_connect_bridge()
		_refresh_all_from_bridge()
	else:
		_note_unavailable()


func _build() -> void:
	var tabs := TabContainer.new()
	tabs.set_anchors_preset(Control.PRESET_FULL_RECT)
	tabs.add_theme_font_size_override("font_size", BODY_FONT)
	add_child(tabs)
	tabs.add_child(_build_node_tab())
	tabs.set_tab_title(0, "Node")
	tabs.add_child(_build_fleet_tab())
	tabs.set_tab_title(1, "Fleet")
	tabs.add_child(_build_approvals_tab())
	tabs.set_tab_title(2, "Approvals")
	tabs.add_child(_build_task_tab())
	tabs.set_tab_title(3, "Task")
	tabs.add_child(_build_settings_tab())
	tabs.set_tab_title(4, "Settings")


func _connect_bridge() -> void:
	_bridge.connection_changed.connect(_on_connection_changed)
	_bridge.agent_status_changed.connect(_on_agent_status)
	_bridge.fleet_changed.connect(_on_fleet)
	_bridge.approvals_changed.connect(_on_approvals)
	_bridge.approval_resolved.connect(_on_approval_resolved)
	_bridge.config_changed.connect(_on_config_changed)


func _refresh_all_from_bridge() -> void:
	_refresh_node()
	if not _bridge.last_fleet.is_empty():
		_on_fleet(_bridge.last_fleet)
	if not _bridge.last_approvals.is_empty():
		_on_approvals(_bridge.last_approvals)


func _note_unavailable() -> void:
	if _node_detail != null:
		_node_detail.text = "bridge missing: console has no data source"


# -- builders ---------------------------------------------------------------

func _big_label(parent: Control, text: String) -> Label:
	var l := Label.new()
	l.text = text
	l.add_theme_font_size_override("font_size", TITLE_FONT)
	parent.add_child(l)
	return l


func _body_label(parent: Control, text: String) -> Label:
	var l := Label.new()
	l.text = text
	l.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	l.add_theme_font_size_override("font_size", BODY_FONT)
	parent.add_child(l)
	return l


func _small_label(parent: Control, text: String) -> Label:
	var l := Label.new()
	l.text = text
	l.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	l.add_theme_font_size_override("font_size", SMALL_FONT)
	l.add_theme_color_override("font_color", C_MUTE)
	parent.add_child(l)
	return l


func _clear(box: Container) -> void:
	for child in box.get_children():
		box.remove_child(child)
		child.queue_free()


# -- Node tab ---------------------------------------------------------------

func _build_node_tab() -> Control:
	var root := VBoxContainer.new()
	root.add_theme_constant_override("separation", 10)
	_node_state = _big_label(root, "SHUGO — ?")
	_node_detail = _body_label(root, "waiting for first status poll")
	return root


func _refresh_node() -> void:
	if _bridge == null or _node_state == null:
		return
	if not _bridge.online:
		_node_state.text = "SHUGO — offline"
		_node_state.add_theme_color_override("font_color", C_BAD)
		if _bridge.last_error != "":
			_node_detail.text = "last error: %s" % _bridge.last_error
		return
	_node_state.text = "SHUGO — online"
	_node_state.add_theme_color_override("font_color", C_OK)
	var lines: Array[String] = []
	lines.append("url: %s (%s)" % [_bridge.base_url, _bridge.config_source])
	var role := str(_bridge.mesh_role())
	lines.append("mesh: %s" % (role if role != "" else "(unknown)"))
	var ok: Variant = _bridge.baseline_ok()
	if ok == null:
		lines.append("security: (no baseline surface)")
	else:
		lines.append("security: %s" % ("ok" if bool(ok) else "drift"))
	var loop: String = _bridge.loop_summary()
	if loop != "—":
		lines.append("loop: %s" % loop)
	_node_detail.text = "\n".join(lines)


func _on_connection_changed(_online: bool) -> void:
	_refresh_node()


func _on_agent_status(_status: Dictionary) -> void:
	_refresh_node()


func _on_config_changed() -> void:
	_refresh_node()


# -- Fleet tab --------------------------------------------------------------

func _build_fleet_tab() -> Control:
	var root := VBoxContainer.new()
	_fleet_note = _small_label(root, "polling /api/v1/fleet …")
	var scroll := ScrollContainer.new()
	scroll.custom_minimum_size = Vector2(860, 400)
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	root.add_child(scroll)
	_fleet_box = VBoxContainer.new()
	_fleet_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_fleet_box.add_theme_constant_override("separation", 6)
	scroll.add_child(_fleet_box)
	return root


func _on_fleet(payload: Dictionary) -> void:
	if _fleet_box == null:
		return
	_clear(_fleet_box)
	if not bool(payload.get("enabled", false)):
		_fleet_note.text = "fleet registry unavailable on this engine"
		return
	var nodes: Variant = payload.get("nodes", [])
	if not (nodes is Array) or nodes.is_empty():
		_fleet_note.text = "registry present, no paired nodes"
		return
	var where: String = _bridge.base_url if _bridge != null else "?"
	_fleet_note.text = "%d paired node(s) — live on %s" % [nodes.size(), where]
	for node in nodes:
		if node is Dictionary:
			_fleet_box.add_child(_fleet_row(node))


func _fleet_row(node: Dictionary) -> Control:
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 12)
	var dot := ColorRect.new()
	dot.custom_minimum_size = Vector2(18, 18)
	dot.color = C_OK if bool(node.get("alive", false)) else C_MUTE
	row.add_child(dot)
	var label := _body_label(row, "")
	var name := str(node.get("device_id", "(unnamed)"))
	var alive := "alive" if bool(node.get("alive", false)) else "silent"
	var paired := str(node.get("paired_by", ""))
	label.text = "%s  ·  %s%s" % [name, alive,
			("  ·  paired by " + paired) if paired != "" else ""]
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var manifest: Variant = node.get("manifest", {})
	if manifest is Dictionary and not manifest.is_empty():
		var keys: Array = manifest.keys()
		var bits: Array[String] = []
		for i in mini(keys.size(), 3):
			bits.append("%s=%s" % [str(keys[i]), str(manifest[keys[i]])])
		if not bits.is_empty():
			var sub := _small_label(row, " · ".join(bits))
			sub.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	return row


# -- Approvals tab ----------------------------------------------------------

func _build_approvals_tab() -> Control:
	var root := VBoxContainer.new()
	_appr_note = _small_label(root, "polling /api/v1/approvals …")
	var scroll := ScrollContainer.new()
	scroll.custom_minimum_size = Vector2(860, 400)
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	root.add_child(scroll)
	_appr_box = VBoxContainer.new()
	_appr_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_appr_box.add_theme_constant_override("separation", 8)
	scroll.add_child(_appr_box)
	return root


func _on_approvals(payload: Dictionary) -> void:
	if _appr_box == null:
		return
	_clear(_appr_box)
	if not bool(payload.get("enabled", false)):
		_appr_note.text = "no approval broker on this engine"
		return
	var items: Variant = payload.get("approvals", [])
	if not (items is Array) or items.is_empty():
		_appr_note.text = "broker present, queue empty"
		var total := int(payload.get("pending_total", 0))
		if total > 0:
			_appr_note.text += " (%d pending beyond window)" % total
		return
	_appr_note.text = "%d pending" % items.size()
	for item in items:
		if item is Dictionary:
			_appr_box.add_child(_approval_row(item))


func _approval_row(item: Dictionary) -> Control:
	var rid := str(item.get("request_id", ""))
	var desc: Variant = item.get("description", {})
	var summary := ""
	if desc is Dictionary:
		summary = str(desc.get("summary",
				desc.get("action", "(no summary)")))
	else:
		summary = str(desc)
	var row := VBoxContainer.new()
	row.add_theme_constant_override("separation", 4)
	_body_label(row, summary)
	var bar := HBoxContainer.new()
	bar.add_theme_constant_override("separation", 12)
	var id := _small_label(bar, rid)
	id.add_theme_color_override("font_color", C_MUTE)
	var yes := Button.new()
	yes.text = "APPROVE"
	yes.add_theme_font_size_override("font_size", BODY_FONT)
	yes.pressed.connect(_on_resolve_pressed.bind(rid, true))
	bar.add_child(yes)
	var no := Button.new()
	no.text = "DENY"
	no.add_theme_font_size_override("font_size", BODY_FONT)
	no.pressed.connect(_on_resolve_pressed.bind(rid, false))
	bar.add_child(no)
	row.add_child(bar)
	row.add_child(HSeparator.new())
	return row


func _on_resolve_pressed(rid: String, approve: bool) -> void:
	if _bridge == null:
		return
	_appr_note.text = "resolving %s …" % rid
	_bridge.resolve_approval(rid, approve)


func _on_approval_resolved(rid: String, decision: String, ok: bool) -> void:
	if _appr_note == null:
		return
	if ok:
		_appr_note.text = "%s: %s (recorded)" % [rid, decision]
	else:
		_appr_note.text = "%s: %s failed — see agent log" % [rid, decision]
	if _bridge != null:
		_bridge.poll_approvals()


# -- Task tab ---------------------------------------------------------------

func _build_task_tab() -> Control:
	var root := VBoxContainer.new()
	root.add_theme_constant_override("separation", 8)
	_small_label(root,
		"Dispatch via POST /api/v1/task — policy-gated, same pipeline " +
		"as a local call. Never bypasses policy.")
	_task_type = LineEdit.new()
	_task_type.placeholder_text = "type (e.g. speak, ask)"
	_task_type.add_theme_font_size_override("font_size", BODY_FONT)
	root.add_child(_task_type)
	_task_content = LineEdit.new()
	_task_content.placeholder_text = "content"
	_task_content.add_theme_font_size_override("font_size", BODY_FONT)
	root.add_child(_task_content)
	var send := Button.new()
	send.text = "DISPATCH"
	send.add_theme_font_size_override("font_size", BODY_FONT)
	send.pressed.connect(_on_task_dispatch)
	root.add_child(send)
	_task_note = _small_label(root, "")
	return root


func _on_task_dispatch() -> void:
	if _bridge == null or not _bridge.online:
		_task_note.text = "offline — no target to dispatch to"
		return
	var content := _task_content.text.strip_edges()
	if content == "":
		_task_note.text = "content required"
		return
	var kind := _task_type.text.strip_edges()
	if kind == "":
		kind = "speak"
	_task_note.text = "dispatched: %s" % kind
	_bridge.send_task(kind, content)


# -- Settings tab -----------------------------------------------------------

func _build_settings_tab() -> Control:
	var root := VBoxContainer.new()
	root.add_theme_constant_override("separation", 8)
	_small_label(root,
		"SAVE persists to user://shugocore_xr.json — survives " +
		"headset restarts. TEST reports the observed result.")
	_set_url = LineEdit.new()
	_set_url.placeholder_text = "agent URL (e.g. http://192.168.1.162:11434)"
	_set_url.add_theme_font_size_override("font_size", BODY_FONT)
	root.add_child(_set_url)
	_set_token = LineEdit.new()
	_set_token.placeholder_text = "bearer token (optional)"
	_set_token.secret = true
	_set_token.add_theme_font_size_override("font_size", BODY_FONT)
	root.add_child(_set_token)
	_set_poll = LineEdit.new()
	_set_poll.placeholder_text = "poll seconds (0.5–30)"
	_set_poll.add_theme_font_size_override("font_size", BODY_FONT)
	root.add_child(_set_poll)
	var bar := HBoxContainer.new()
	bar.add_theme_constant_override("separation", 12)
	var save := Button.new()
	save.text = "SAVE"
	save.add_theme_font_size_override("font_size", BODY_FONT)
	save.pressed.connect(_on_settings_save)
	bar.add_child(save)
	var test := Button.new()
	test.text = "TEST"
	test.add_theme_font_size_override("font_size", BODY_FONT)
	test.pressed.connect(_on_settings_test)
	bar.add_child(test)
	root.add_child(bar)
	_set_info = _small_label(root, "")
	_set_saved = _small_label(root, "")
	_load_settings_fields()
	return root


func _load_settings_fields() -> void:
	if _bridge == null:
		return
	_set_url.text = _bridge.base_url
	_set_token.text = _bridge.bearer_token
	_set_poll.text = "%.1f" % _bridge.poll_seconds
	_refresh_settings_info()


func _refresh_settings_info() -> void:
	if _set_info == null or _bridge == null:
		return
	_set_info.text = "source: %s · online: %s" % [
		_bridge.config_source, "yes" if _bridge.online else "no"]


func _on_settings_save() -> void:
	if _bridge == null:
		return
	_bridge.base_url = _bridge._clean_url(_set_url.text)
	_bridge.bearer_token = _set_token.text.strip_edges()
	var p := _set_poll.text.strip_edges()
	if p.is_valid_float():
		_bridge.poll_seconds = clampf(p.to_float(),
				ShugoCoreConfig.MIN_POLL_SECONDS,
				ShugoCoreConfig.MAX_POLL_SECONDS)
	else:
		_set_saved.text = "poll invalid — kept %.1fs" % _bridge.poll_seconds
	var cfg := ShugoCoreConfig.new()
	cfg.agent_url = _bridge.base_url
	cfg.bearer_token = _bridge.bearer_token
	cfg.poll_seconds = _bridge.poll_seconds
	cfg.fleet_poll_seconds = _bridge.fleet_poll_seconds
	if not cfg.save():
		_set_saved.text = "save failed: %s" % cfg.last_error
		return
	_set_saved.text = "saved to user://shugocore_xr.json"
	_bridge.reload_config()
	_bridge.poll_status()
	_bridge.poll_fleet()
	_bridge.poll_approvals()
	_load_settings_fields()


func _on_settings_test() -> void:
	## TEST applies the typed values to this session (no persistence) and
	## triggers a real poll; the result is OBSERVED via the online/last_error
	## state the refresh shows — never claimed by the button itself.
	if _bridge == null:
		return
	_bridge.base_url = _bridge._clean_url(_set_url.text)
	_bridge.bearer_token = _set_token.text.strip_edges()
	_set_info.text = "probing %s …" % _bridge.base_url
	_set_saved.text = ""
	_bridge.poll_status()
	_bridge.poll_fleet()
	_refresh_settings_info()
