extends Node
## ShugoCore agent bridge (Track 3 scaffold).
##
## The ONLY network surface of the XR client. Talks to the running
## ShugoCore agent over its real wire contracts and publishes what it
## observes — never invents state. Bounded: one request in flight per
## endpoint family, capped text, clamped poll interval.
##
## Configuration is FILE-FIRST (see scripts/shugocore_config.gd) because
## `OS.get_environment()` returns "" on Android: a Quest build cannot be
## configured by shell env. The desktop env names
## (SHUGOCORE_XR_AGENT_URL / SHUGOCORE_SERVER_TOKEN) remain honoured as a
## fallback layer, resolved inside ShugoCoreConfig.

signal connection_changed(online: bool)
signal agent_status_changed(status: Dictionary)
signal agent_reply(text: String)
signal request_failed(route: String, code: int)
## Operator-console surfaces (Phase 3). Each fires only with real payloads.
signal fleet_changed(payload: Dictionary)
signal approvals_changed(payload: Dictionary)
signal approval_resolved(request_id: String, decision: String, ok: bool)
signal sensor_window_changed(payload: Dictionary)
signal config_changed()

const DEFAULT_BASE_URL := "http://127.0.0.1:11434"
const DEFAULT_POLL_SECONDS := 2.0
const MIN_POLL_SECONDS := 0.5
const MAX_POLL_SECONDS := 30.0
const MAX_TEXT_CHARS := 4000
const REQUEST_TIMEOUT_S := 8.0
## Operator-console rows are bounded so a hostile/huge registry cannot
## exhaust headset memory.
const MAX_FLEET_NODES := 64
const MAX_APPROVALS := 32
const MAX_MANIFEST_KEYS := 12

const ROUTE_HEALTH := "/health"
const ROUTE_STATUS := "/api/v1/status"
const ROUTE_SENSORS := "/api/v1/sensors"
const ROUTE_GENERATE := "/api/generate"
const ROUTE_TASK := "/api/v1/task"
const ROUTE_FLEET := "/api/v1/fleet"
const ROUTE_APPROVALS := "/api/v1/approvals"
const ROUTE_APPROVAL_PREFIX := "/api/v1/approvals/"

var base_url: String = DEFAULT_BASE_URL
var bearer_token: String = ""
var poll_seconds: float = DEFAULT_POLL_SECONDS
var fleet_poll_seconds: float = 5.0

var online: bool = false
var last_status: Dictionary = {}
var last_sensors: Dictionary = {}
var last_fleet: Dictionary = {}
var last_approvals: Dictionary = {}
var last_error: String = ""
## Provenance of the active config ("user://", "res://", "defaults").
var config_source: String = "defaults"

var _http_status: HTTPRequest
var _http_sensors: HTTPRequest
var _http_generate: HTTPRequest
var _http_task: HTTPRequest
var _http_fleet: HTTPRequest
var _http_approvals: HTTPRequest
var _http_resolve: HTTPRequest
var _poll_accum: float = 0.0
var _fleet_accum: float = 0.0



func _ready() -> void:
	reload_config()
	_http_status = _make_http("http_status", "status")
	_http_sensors = _make_http("http_sensors", "sensors")
	_http_generate = _make_http("http_generate", "generate")
	_http_task = _make_http("http_task", "task")
	_http_fleet = _make_http("http_fleet", "fleet")
	_http_approvals = _make_http("http_approvals", "approvals")
	_http_resolve = _make_http("http_resolve", "resolve")


## (Re)load configuration from the file layers and apply it live.
## Called at startup and whenever the Settings panel saves.
func reload_config() -> void:
	var cfg := ShugoCoreConfig.load_config()
	base_url = _clean_url(cfg.agent_url)
	bearer_token = cfg.bearer_token
	poll_seconds = clampf(cfg.poll_seconds,
			MIN_POLL_SECONDS, MAX_POLL_SECONDS)
	fleet_poll_seconds = clampf(cfg.fleet_poll_seconds,
			MIN_POLL_SECONDS, MAX_POLL_SECONDS)
	config_source = cfg.source
	config_changed.emit()


func _make_http(name_suffix: String, tag: String) -> HTTPRequest:
	## Each request node is bound to its own tag so responses demultiplex
	## by ENDPOINT instead of by guessing from the payload shape.
	var http := HTTPRequest.new()
	http.name = name_suffix
	http.timeout = REQUEST_TIMEOUT_S
	http.use_threads = true
	add_child(http)
	http.request_completed.connect(_on_http_completed.bind(tag))
	return http


func _clean_url(url: String) -> String:
	var cleaned := url.strip_edges().trim_suffix("/")
	return cleaned if cleaned != "" else DEFAULT_BASE_URL


func _headers() -> PackedStringArray:
	if bearer_token != "":
		return PackedStringArray(["Authorization: Bearer " + bearer_token])
	return PackedStringArray()


func _process(delta: float) -> void:
	_poll_accum += delta
	if _poll_accum >= poll_seconds:
		_poll_accum = 0.0
		poll_status()
	_fleet_accum += delta
	if _fleet_accum >= fleet_poll_seconds:
		_fleet_accum = 0.0
		poll_fleet()
		poll_approvals()


# -- GET surfaces -----------------------------------------------------------

func poll_status() -> void:
	var err := _http_status.request(base_url + ROUTE_STATUS, _headers(),
			HTTPClient.METHOD_GET)
	if err == ERR_BUSY:
		# Previous request on this node still in flight: a normal
		# polling skip, never an offline condition.
		return
	if err != OK:
		_mark_offline("status request error %d" % err)


func poll_sensors() -> void:
	var err := _http_sensors.request(base_url + ROUTE_SENSORS, _headers(),
			HTTPClient.METHOD_GET)
	if err == ERR_BUSY:
		# Previous request on this node still in flight: a normal
		# polling skip, never an offline condition.
		return
	if err != OK:
		_mark_offline("sensors request error %d" % err)


func health() -> void:
	var err := _http_status.request(base_url + ROUTE_HEALTH, _headers(),
			HTTPClient.METHOD_GET)
	if err == ERR_BUSY:
		# Previous request on this node still in flight: a normal
		# polling skip, never an offline condition.
		return
	if err != OK:
		_mark_offline("health request error %d" % err)


# -- operator console surfaces (GET) ---------------------------------------

func poll_fleet() -> void:
	## GET /api/v1/fleet -> paired-node registry. Absent registry is
	## reported by the server as enabled:false (never fabricated here).
	var err := _http_fleet.request(base_url + ROUTE_FLEET, _headers(),
			HTTPClient.METHOD_GET)
	if err == ERR_BUSY:
		# Previous request on this node still in flight: a normal
		# polling skip, never an offline condition.
		return
	if err != OK:
		_mark_offline("fleet request error %d" % err)


func poll_approvals() -> void:
	## GET /api/v1/approvals -> pending side-effecting action queue.
	var err := _http_approvals.request(base_url + ROUTE_APPROVALS,
			_headers(), HTTPClient.METHOD_GET)
	if err == ERR_BUSY:
		# Previous request on this node still in flight: a normal
		# polling skip, never an offline condition.
		return
	if err != OK:
		_mark_offline("approvals request error %d" % err)


func resolve_approval(request_id: String, approve: bool) -> void:
	## POST /api/v1/approvals/<id>/approve|deny -> operator resolution.
	## The id is percent-safe-checked: an id carrying path separators or
	## query characters is refused locally rather than being turned into a
	## request the server would reject as an unknown route.
	var rid := request_id.strip_edges()
	if rid == "" or not _is_safe_approval_id(rid):
		approval_resolved.emit(rid, "rejected", false)
		return
	var action := "approve" if approve else "deny"
	var err := _http_resolve.request(
			base_url + ROUTE_APPROVAL_PREFIX + rid + "/" + action,
			_headers(), HTTPClient.METHOD_POST)
	if err == ERR_BUSY:
		# Previous request on this node still in flight: a normal
		# polling skip, never an offline condition.
		return
	if err != OK:
		_mark_offline("approval resolve error %d" % err)
		approval_resolved.emit(rid, action, false)


static func _is_safe_approval_id(rid: String) -> bool:
	if rid.length() > 64:
		return false
	for i in rid.length():
		var c := rid[i]
		if c == "/" or c == "?" or c == "#" or c == "\\" or c == " ":
			return false
	return true


# -- POST surfaces ----------------------------------------------------------

func chat(prompt: String, model: String = "") -> void:
	## Ollama wire contract (what AndroidBackend speaks). Non-streaming;
	## the (single) reply arrives via the agent_reply signal.
	if prompt.strip_edges() == "":
		return
	var body := {"model": model, "prompt": prompt.substr(0, MAX_TEXT_CHARS),
			"stream": false}
	var err := _http_generate.request(
			base_url + ROUTE_GENERATE, _headers(), HTTPClient.METHOD_POST,
			JSON.stringify(body))
	if err == ERR_BUSY:
		# Previous request on this node still in flight: a normal
		# polling skip, never an offline condition.
		return
	if err != OK:
		_mark_offline("generate request error %d" % err)


func send_task(type: String, content: String, params: Dictionary = {}) -> void:
	## Policy-gated execute_task path — same governor/fallback pipeline a
	## local call uses; the bridge never bypasses policy.
	if content.strip_edges() == "":
		return
	var body := {"type": type, "content": content.substr(0, MAX_TEXT_CHARS)}
	if not params.is_empty():
		body["params"] = params
	var err := _http_task.request(base_url + ROUTE_TASK, _headers(),
			HTTPClient.METHOD_POST, JSON.stringify(body))
	if err == ERR_BUSY:
		# Previous request on this node still in flight: a normal
		# polling skip, never an offline condition.
		return
	if err != OK:
		_mark_offline("task request error %d" % err)


# -- response handling ------------------------------------------------------

func _on_http_completed(_result: int, code: int, _headers_in: Array,
		body: PackedByteArray, tag: String) -> void:
	## Per-endpoint demultiplexer. Every child HTTPRequest is connected
	## with its route tag bound (see _make_http), so a response is applied
	## to the surface that asked for it — never guessed from payload shape.
	var text := body.get_string_from_utf8().substr(0, MAX_TEXT_CHARS * 4)
	if code < 200 or code >= 300:
		_mark_offline("%s http %d" % [tag, code])
		request_failed.emit(tag, code)
		return
	match tag:
		"status":
			_apply_status_json(text)
		"sensors":
			_apply_sensors_json(text)
		"fleet":
			_apply_fleet_json(text)
		"approvals":
			_apply_approvals_json(text)
		"generate":
			_apply_generate_json(text)
		"resolve":
			_apply_resolve_json(text)
		_:
			# task results are surfaced through the console, not spoken.
			_mark_online()


func _apply_status_json(text: String) -> void:
	var parsed: Variant = JSON.parse_string(text)
	if parsed is Dictionary:
		_apply_status(parsed)
	else:
		# /health answers plain text in some deployments.
		_mark_online()


func _apply_sensors_json(text: String) -> void:
	var parsed: Variant = JSON.parse_string(text)
	last_sensors = parsed if parsed is Dictionary else {"window": parsed}
	_mark_online()
	sensor_window_changed.emit(last_sensors)


func _apply_fleet_json(text: String) -> void:
	var parsed: Variant = JSON.parse_string(text)
	if not (parsed is Dictionary):
		_mark_online()
		return
	last_fleet = _bound_fleet(parsed)
	_mark_online()
	fleet_changed.emit(last_fleet)


func _apply_approvals_json(text: String) -> void:
	var parsed: Variant = JSON.parse_string(text)
	if not (parsed is Dictionary):
		_mark_online()
		return
	last_approvals = _bound_approvals(parsed)
	_mark_online()
	approvals_changed.emit(last_approvals)


func _apply_generate_json(text: String) -> void:
	var parsed: Variant = JSON.parse_string(text)
	if parsed is Dictionary:
		var reply := str(parsed.get("response", "")).substr(0, MAX_TEXT_CHARS)
		_mark_online()
		if reply.strip_edges() != "":
			agent_reply.emit(reply)
	else:
		_mark_online()


func _apply_resolve_json(text: String) -> void:
	var parsed: Variant = JSON.parse_string(text)
	var rid := ""
	var decision := ""
	var ok := false
	if parsed is Dictionary:
		rid = str(parsed.get("request_id", ""))
		decision = str(parsed.get("decision", ""))
		ok = bool(parsed.get("resolved", false))
	_mark_online()
	approval_resolved.emit(rid, decision, ok)


func _bound_fleet(raw: Dictionary) -> Dictionary:
	## Bound the raw fleet payload: at most MAX_FLEET_NODES rows and
	## MAX_MANIFEST_KEYS manifest entries each. `enabled` is preserved
	## verbatim so the console can tell "no registry" from "no nodes".
	var out: Array = []
	var nodes: Variant = raw.get("nodes", [])
	if nodes is Array:
		for node in nodes:
			if out.size() >= MAX_FLEET_NODES or not (node is Dictionary):
				break
			out.append({
				"device_id": str(node.get("device_id", "")).substr(0, 128),
				"alive": bool(node.get("alive", false)),
				"paired_by": str(node.get("paired_by", "")).substr(0, 64),
				"expires_at": node.get("expires_at", null),
				"manifest": _bound_map(node.get("manifest", {}), 200),
			})
	return {
		"nodes": out,
		"count": out.size(),
		"enabled": bool(raw.get("enabled", false)),
	}


func _bound_approvals(raw: Dictionary) -> Dictionary:
	var out: Array = []
	var items: Variant = raw.get("approvals", [])
	if items is Array:
		for item in items:
			if out.size() >= MAX_APPROVALS or not (item is Dictionary):
				break
			out.append({
				"request_id": str(item.get("request_id", "")).substr(0, 64),
				"description": _bound_map(item.get("description", {}), 500),
				"requested_at": float(item.get("requested_at", 0.0)),
			})
	return {
		"approvals": out,
		"count": out.size(),
		"pending_total": int(raw.get("pending_total", out.size())),
		"enabled": bool(raw.get("enabled", false)),
		"ttl_seconds": raw.get("ttl_seconds", null),
	}


static func _bound_map(raw: Variant, value_chars: int) -> Dictionary:
	## Bounded string map — caps key count and per-value length so a
	## hostile payload cannot blow up headset memory.
	var out := {}
	if raw is Dictionary:
		var keys: Array = raw.keys()
		for i in mini(keys.size(), MAX_MANIFEST_KEYS):
			var k: String = str(keys[i]).substr(0, 64)
			out[k] = str(raw[keys[i]]).substr(0, value_chars)
	return out


func _apply_status(dict: Dictionary) -> void:
	last_status = dict
	_mark_online()
	agent_status_changed.emit(dict)


func _mark_online() -> void:
	last_error = ""
	if not online:
		online = true
		connection_changed.emit(true)


func _mark_offline(reason: String) -> void:
	last_error = reason
	if online:
		online = false
		connection_changed.emit(false)


# -- honest readers ---------------------------------------------------------

func mesh_role() -> String:
	## Track 1: primary / follower / standalone (or "" when unknown).
	if last_status.is_empty():
		return ""
	return str(last_status.get("mesh_role", ""))


func baseline_ok() -> Variant:
	## Track 2: true/false/null (null = absent surface).
	if last_status.is_empty():
		return null
	var baseline: Variant = last_status.get("security_baseline")
	if baseline is Dictionary:
		return baseline.get("baseline_ok")
	return null


func loop_summary() -> String:
	## Bounded one-line loop summary for the presence UI.
	var agent_block: Variant = last_status.get("agent")
	if agent_block is Dictionary:
		var loop: Variant = agent_block.get("loop")
		if loop is Dictionary:
			return "cycles %s" % str(loop.get("cycles", "?"))
	return "—"
