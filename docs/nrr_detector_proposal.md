# NRR detector extension proposal (cross-repo, non-blocking)

NRR ([SamurAI-Official/NRR](https://github.com/SamurAI-Official/NRR)) is a
neural *rendering* runtime: pixels in, pixels out. It has no detection,
classification, or text path today, so ShugoCore's peripheral visual
perception ships as a ShugoCore-local contract (structured scene context
over the mobile mesh) with the NRR rendering worker contract (`nrr/`
package) handling pixel work. This doc proposes the NRR-side extension
that would let the repos converge later: a `neural_detector` model type
plus a structured detection output contract.

Nothing here blocks current work: the proposal is additive (new model
type, new output struct) and the ShugoCore side degrades to its local
contract when no peer advertises detector support.

## 1. Proposed model type: `neural_detector`

Add to the NRR model-type registry (`specification/model_format.md`):

| Type | Description |
|------|-------------|
| `neural_detector` | Structured perception model: image in, detections out |

Model metadata sketch (follows the existing `.nrrmodel` schema):

```json
{
  "format_version": 1,
  "model_type": "neural_detector",
  "model_name": "person_detector_v1",
  "input_spec": {
    "color": {"format": "RGB8", "resolution": "any", "required": true}
  },
  "output_spec": {
    "detections": {"format": "NRRDetectionArray", "required": true}
  },
  "capabilities_required": {
    "int8_quantization": true,
    "minimum_vram_mb": 256
  },
  "tags": ["perception", "person_detection"]
}
```

## 2. Proposed output contract: `NRRDetectionArray`

New output struct alongside `NRRFrameOutput` (`specification/api.md`):

```c
typedef struct {
  uint32_t   object_id;      // stable within a session when tracked
  char       label[32];      // e.g. "person", "face", "cat"
  float      confidence;     // 0..1
  float      bbox[4];        // normalized x, y, w, h (0..1)
  float      gaze_toward_camera; // -1 unknown, 0 away, 1 toward
} NRRDetection;

typedef struct {
  uint64_t      frame_index;   // matches the input TemporalState
  uint32_t      detection_count;
  NRRDetection* detections;
  char          scene_verdict[64]; // cf. ShugoCore SpeechSource taxonomy
} NRRDetectionArray;
```

Design notes:

- **No pixel bytes in the output.** Detections are coordinates + labels;
  the input frame never leaves the device. This matches the ShugoCore
  posture (faces are PII, `privacy_scope: local`).
- **`frame_index` binds input to output** so the host can expire stale
  detections (same role as `TemporalState.frame_index` in rendering).
- **`scene_verdict`** is an optional single-string summary using the
  ShugoCore scene taxonomy (`verified_person`, `person_talking`,
  `person_present_silent`, `unattributed_audio`, `ambient_noise`, ...),
  letting a thin host skip per-box reasoning when the peer already
  classified the scene.

## 3. Proposed capability flags

Extend the capability matrix (`specification/capability_matrix.md`):

| Capability | Description | Required for |
|------------|-------------|--------------|
| `int8_quantization` | 8-bit integer inference | Low-memory detector models |
| `perception_output` | Structured detection output support | `neural_detector` execution |
| `face_landmarks` | Facial landmark output (opt-in, PII) | Gaze/identity features |

## 4. ShugoCore-side convergence path

When (not if) NRR gains this contract:

1. The ShugoCore peripheral detector publishes `NRRDetectionArray`
   natively instead of the local `SceneContext` JSON -- the fields map
   1:1 (`label`/`confidence`/`bbox`/`frame_index`/`scene_verdict`).
2. `nrr/protocol.py` gains `make_detection_result()`; the transport
   adapter accepts both envelope types during migration.
3. Capability advertisement (`compute_caps.workloads`) gains a
   `"nrr_detect"` workload alongside `"nrr_render"`, routed by the same
   `nodes_for_workload()` path.
4. The local `SceneContext` contract stays as the fallback for peers
   without NRR detector support (fail-open, audited).

## 5. What is explicitly out of scope

- Face *identity* output (embeddings, recognition IDs). Detection
  answers "a person is there"; identity stays behind explicit consent
  and the existing `.nrrref` provenance mechanism.
- Training or reference harvesting on-device. The detector is
  inference-only; `prohibited_uses: ["model_training"]` from the
  reference format applies by analogy.
- Any change to the rendering frame contract. This proposal is purely
  additive: new model type, new output struct, new capability flags.
