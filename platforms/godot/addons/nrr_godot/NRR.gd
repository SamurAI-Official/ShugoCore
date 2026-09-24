extends RefCounted
class_name NRR

## NRR (Neural Rendering Runtime) GDScript API — Godot 4.x binding
## surface for the NRR C ABI (specification/api.md).
##
## HONEST STUB: this scaffold ships without the native NRR library
## linked. Every call therefore reports available == false and performs
## NO rendering — it never fabricates neural output. When the native
## library is linked into the engine build, the _call_native() seams
## below are where GDNative/Extension bindings slot in.

signal model_loaded(name: String)
signal render_completed(image: Image)

const LIBRARY_NAME := "nrr"

var available: bool = false
var loaded_model: String = ""

var _initialized: bool = false


func initialize() -> bool:
	## Initialize the runtime. Returns true only when the native library
	## is actually usable.
	_initialized = _library_present()
	available = _initialized
	return available


func _library_present() -> bool:
	## Real capability probe: the native NRR shared library must be
	## loadable. Absent -> false (observed absence, not an assumption).
	return OS.has_feature("nrr_native")


func load_model(path: String) -> bool:
	## Load a .nrrmodel reference-conditioned model by res:// path.
	if not available:
		return false
	loaded_model = path
	model_loaded.emit(path)
	return true


func render_frame(color: Image, depth: Image, motion: Image = null) -> Image:
	## Render one frame. Returns the input unchanged while the native
	## backend is absent — callers MUST treat a returned image with
	## nrr_applied == false as passthrough, not neural output.
	if not available:
		return color
	return _call_native_render(color, depth, motion)


func _call_native_render(color: Image, depth: Image, motion: Image) -> Image:
	## Native seam. Currently unreachable (available is false without
	## the library); kept so the binding point is explicit.
	return color


func shutdown() -> void:
	_initialized = false
	available = false
	loaded_model = ""
