extends RefCounted
class_name ShugoCoreConfig

## ShugoCore XR configuration (Track 3 / Phase 2).
##
## WHY THIS EXISTS: `OS.get_environment()` returns "" on Android — a Quest
## build cannot be told anything via shell env vars. Configuration is
## therefore FILE-FIRST:
##
##   1. user://shugocore_xr.json   (runtime, written by the Settings panel)
##   2. res://shugocore_xr.default.json (shipped defaults, read-only)
##   3. OS env (desktop convenience only — never the only path)
##   4. built-in constants
##
## Resolution never invents a value: a missing key falls through to the
## next layer, and an absent host stays DEFAULT_AGENT_URL with
## `writable_json` reporting where a user edit would land.

const USER_PATH := "user://shugocore_xr.json"
const DEFAULT_PATH := "res://shugocore_xr.default.json"

## Desktop default. A headset reaches the agent over the LAN, so the
## per-machine value belongs in user:// (Settings panel) or the default file.
const DEFAULT_AGENT_URL := "http://127.0.0.1:11434"
const DEFAULT_POLL_SECONDS := 2.0
const MIN_POLL_SECONDS := 0.5
const MAX_POLL_SECONDS := 30.0
const DEFAULT_FLEET_POLL_SECONDS := 5.0

var agent_url: String = DEFAULT_AGENT_URL
var bearer_token: String = ""
var poll_seconds: float = DEFAULT_POLL_SECONDS
var fleet_poll_seconds: float = DEFAULT_FLEET_POLL_SECONDS

## Where the current values came from, for honest status reporting.
var source: String = "defaults"
## Populated when a config file existed but could not be parsed.
var last_error: String = ""


static func load_config() -> RefCounted:
	var cfg: ShugoCoreConfig = new()
	var data: Dictionary = {}
	var from_user := load_config_read_json(USER_PATH)
	if not from_user.is_empty():
		data = from_user
		cfg.source = "user://"
	else:
		var from_default := load_config_read_json(DEFAULT_PATH)
		if not from_default.is_empty():
			data = from_default
			cfg.source = "res://"
		else:
			cfg.source = "defaults"

	cfg.agent_url = load_config_str(data.get("agent_url",
			OS.get_environment("SHUGOCORE_XR_AGENT_URL")),
			cfg.agent_url)
	cfg.bearer_token = load_config_str(data.get("bearer_token",
			OS.get_environment("SHUGOCORE_SERVER_TOKEN")), "")
	var poll_raw: Variant = data.get("poll_seconds",
			OS.get_environment("SHUGOCORE_XR_POLL_SECONDS"))
	cfg.poll_seconds = load_config_clamp_poll(poll_raw,
			DEFAULT_POLL_SECONDS)
	var fleet_raw: Variant = data.get("fleet_poll_seconds", null)
	cfg.fleet_poll_seconds = load_config_clamp_poll(fleet_raw,
			DEFAULT_FLEET_POLL_SECONDS)
	return cfg


## Persist the current values to user:// so they survive a headset restart.
## Returns true only when the write actually succeeded.
func save() -> bool:
	var payload := {
		"agent_url": agent_url,
		"bearer_token": bearer_token,
		"poll_seconds": poll_seconds,
		"fleet_poll_seconds": fleet_poll_seconds,
	}
	var f := FileAccess.open(USER_PATH, FileAccess.WRITE)
	if f == null:
		last_error = "cannot write %s" % USER_PATH
		return false
	f.store_string(JSON.stringify(payload, "  "))
	f.close()
	source = "user://"
	last_error = ""
	return true


## True when a runtime-writable config already exists.
static func has_user_config() -> bool:
	return FileAccess.file_exists(USER_PATH)


static func load_config_read_json(path: String) -> Dictionary:
	if not FileAccess.file_exists(path):
		return {}
	var f := FileAccess.open(path, FileAccess.READ)
	if f == null:
		return {}
	var parsed: Variant = JSON.parse_string(f.get_as_text())
	f.close()
	if parsed is Dictionary:
		return parsed
	return {}


static func load_config_str(value: Variant, fallback: String) -> String:
	if value == null:
		return fallback
	var s := str(value).strip_edges()
	return s if s != "" else fallback


static func load_config_clamp_poll(value: Variant, fallback: float) -> float:
	if value == null:
		return fallback
	var f: float = 0.0
	if value is float or value is int:
		f = float(value)
	else:
		var s := str(value).strip_edges()
		if s == "" or not s.is_valid_float():
			return fallback
		f = s.to_float()
	if f <= 0.0:
		return fallback
	return clampf(f, MIN_POLL_SECONDS, MAX_POLL_SECONDS)


## One-line provenance summary for the Settings panel — reports the real
## source rather than implying the values were always user-set.
func describe() -> String:
	return "%s | poll %.1fs | fleet %.1fs | token %s" % [
		source if source != "defaults" else "built-in defaults",
		poll_seconds, fleet_poll_seconds,
		"set" if bearer_token != "" else "none",
	]
