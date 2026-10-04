extends Node

## A headless world session: an operator in the XR surface reaches the agent, and is answered.
##
##     godot --headless --path platforms/godot -- --world-session
##
## It drives the scaffold's OWN autoloads -- ShugoCoreBridge for the wire, XRBootstrap for
## presence -- so what it records is the scaffold's behaviour, not a parallel client that
## happens to speak the same protocol. The lines it prints are the world-engagement transcript
## the claims matrix judges:
##
##     [WORLD  ] xr
##     [PRESENCE] mode=desktop_preview
##     [GOAL   ] operator (in the virtual space): ...
##     [ACTION ] execute_task conversation (gated) -> status=... stages=...
##     [REPLY  ] ...
##
## With no headset the scaffold honestly runs in desktop_preview, and the mode is *read* from
## the bootstrap rather than assumed; with one it reports xr. Nothing here invents an answer:
## no reply means no [REPLY ] line, and the session exits non-zero saying so.

const UTTERANCE := "note that the charger is warm to the touch and say whether that needs attention"
const WAIT_S := 60.0
## How long to wait for the bootstrap's presence to settle before reporting it as it
## stands. The bootstrap watches for the same reason; this is the outer bound.
const PRESENCE_WAIT_FRAMES := 300

var _started := false
var _acted := false
var _replied := false
var _done := false
var _deadline := 0.0
var _file: FileAccess = null
var _presence_reported := ""
## Operator sessions do not end themselves: no deadline, no quit after the first
## exchange. Set by `--linger`, which is what `xr_session.py --interactive` passes.
var _linger := false


func _ready() -> void:
    if not OS.get_cmdline_user_args().has("--world-session"):
        return                      # an ordinary run: the scene plays as usual
    _open_session_log()
    _linger = OS.get_cmdline_user_args().has("--linger")
    _deadline = 0.0 if _linger else _now() + WAIT_S
    var url := _agent_url_arg()
    if url != "":
        _write_user_config(url)
    _emit("[WORLD  ] xr")
    ShugoCoreBridge.connection_changed.connect(_on_connection)
    ShugoCoreBridge.agent_reply.connect(_on_reply)
    ShugoCoreBridge.task_result.connect(_on_task)
    ShugoCoreBridge.request_failed.connect(_on_failed)
    ShugoCoreBridge.health()
    # Last, and awaited: the presence is *observed* and it settles late -- a Quest over
    # Quest Link reaches XR_SESSION_STATE_FOCUSED after the autoloads have run -- so
    # reading XRBootstrap.mode once here recorded "desktop_preview" while the operator was
    # wearing the headset and looking at this scene. Waiting for the observation does not
    # delay the wire above, which is why this is at the end.
    await _report_presence()


func _open_session_log() -> void:
    ## The session also writes its own log file. Godot buffers print() to stdout when it is
    ## piped rather than attached to a console, so a driver that only captured stdout lost
    ## every line here and saw an engine that "printed no world session" -- while the engine
    ## warnings on stderr came through fine.
    var path := _arg_value("--session-out=")
    if path == "":
        return
    _file = FileAccess.open(path, FileAccess.WRITE)
    if _file == null:
        print("[SESSION] could not open the session log at " + path)


func _emit(text: String) -> void:
    print(text)
    if _file != null:
        _file.store_line(text)
        _file.flush()


func _arg_value(prefix: String) -> String:
    for arg in OS.get_cmdline_user_args():
        if arg.begins_with(prefix):
            return arg.substr(prefix.length())
    return ""


func _agent_url_arg() -> String:
    return _arg_value("--agent-url=")


func _write_user_config(url: String) -> void:
    ## Use the operator's own path, not a private back door: the in-headset Settings panel
    ## writes this file, and it takes precedence over the shipped defaults. A session driver
    ## that set the bridge's URL directly would skip the configuration layer the scaffold
    ## documents, and then a real headset and this harness would disagree about where the
    ## agent is.
    var file := FileAccess.open("user://shugocore_xr.json", FileAccess.WRITE)
    if file == null:
        print("[SESSION] could not write the user config; using whatever it already has")
        return
    file.store_string(JSON.stringify({"agent_url": url, "poll_seconds": 0.5,
            "fleet_poll_seconds": 5.0}))
    file.close()
    print("[SESSION] user config agent_url=" + url)
    ShugoCoreBridge.reload_config()


func _now() -> float:
    return float(Time.get_ticks_msec()) / 1000.0


func _process(_delta: float) -> void:
    if _done or _deadline == 0.0:
        return
    if _linger:
        return      # an operator session ends when the operator ends it
    if _now() > _deadline:
        _emit("[SESSION] world session timed out (acted=%s replied=%s)" % [_acted, _replied])
        _finish()
        return
    if _acted and _replied:
        _finish()


func _on_connection(online: bool) -> void:
    if not online or _started:
        return
    _started = true
    _emit("[GOAL   ] operator (in the virtual space): " + UTTERANCE)
    # Both of the agent's own paths, because a surface needs both halves of an exchange: the
    # policy-gated execute_task submits the operator's words as a task, and the chat path
    # returns the agent's answer as data. A headless server has no loudspeaker, so a `speak`
    # action there is honestly `no_output` -- which is why the answer is read where the agent
    # publishes it rather than assumed to have been said aloud.
    ShugoCoreBridge.send_task("conversation", UTTERANCE)
    ShugoCoreBridge.chat(UTTERANCE)


func _on_task(payload: Dictionary) -> void:
    if _acted and not _linger:
        return                  # scripted: the transcript records the first exchange
    _acted = true
    _emit("[ACTION ] execute_task conversation (gated) -> status=%s stages=%s" % [
            str(payload.get("status", "unknown")),
            str(payload.get("stages", []))])
    var reply := _text_from(payload)
    if reply != "":
        _on_reply(reply)


func _on_reply(text: String) -> void:
    if text.strip_edges() == "" or (_replied and not _linger):
        return
    _replied = true
    _emit("[REPLY  ] " + text.strip_edges())


func _on_failed(route: String, code: int) -> void:
    _emit("[SESSION] request failed: %s http %d" % [route, code])


func _text_from(payload: Dictionary) -> String:
    ## The agent's answer, wherever the engine put it. Bounded and shallow on purpose: this
    ## reads a result, it does not go looking for prose to print.
    for key in ["reply", "text", "message", "spoken", "answer"]:
        var value: Variant = payload.get(key)
        if value is String and value.strip_edges() != "":
            return value
    var results: Variant = payload.get("results")
    if results is Array:
        for entry in results:
            if entry is Dictionary:
                var nested := _text_from(entry)
                if nested != "":
                    return nested
    return ""


func _report_presence() -> void:
    ## Report the mode when it settles, and again if it changes while the session runs.
    if str(XRBootstrap.mode) != "unavailable":
        _emit_presence()
        return
    XRBootstrap.mode_changed.connect(_on_presence_changed)
    for _frame in PRESENCE_WAIT_FRAMES:
        await get_tree().process_frame
        if str(XRBootstrap.mode) != "unavailable":
            _emit_presence()
            return
    # Nothing settled. The bootstrap's "unavailable" is its *initial* value -- "not yet
    # determined" -- and writing that into the transcript presents a placeholder as an
    # observation. Say what actually happened instead.
    _presence_reported = "not observed before the session ended"
    _emit("[PRESENCE] mode=" + _presence_reported)


func _on_presence_changed(_mode: String) -> void:
    _emit_presence()


func _emit_presence() -> void:
    var observed := str(XRBootstrap.mode)
    if observed == _presence_reported:
        return
    _presence_reported = observed
    _emit("[PRESENCE] mode=" + observed)


func _finish() -> void:
    if _done:
        return
    _done = true
    if _presence_reported == "":
        # The transcript carries the presence, so it is worth waiting for the observation
        # rather than closing with a placeholder in the file. Found by running the
        # scripted path and reading `mode=unavailable`: the exchange finishes in fewer
        # frames than the presence takes to settle, so the wait has to happen here.
        await _report_presence()
    _emit("[SESSION] world session complete (acted=%s replied=%s)" % [_acted, _replied])
    if _file != null:
        _file.close()
        _file = null
    get_tree().quit(0 if (_acted and _replied) else 1)
