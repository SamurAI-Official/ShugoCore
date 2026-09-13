# NRR Neural Model Architecture

> Reference neural upscaling model for NRR v0.5

---

## 1. Overview

This document describes a simple neural upscaling model that demonstrates NRR's model execution capabilities. The model takes a low-resolution input frame and produces a high-resolution output.

**Model Type:** Neural Upscaler  
**Input:** 1080p frame (color + depth + motion)  
**Output:** 4K frame  
**Architecture:** Super-resolution with temporal coherence

---

## 2. Model Architecture

### 2.1 Overall Structure

```
Input Frame (1080p)
    │
    ├── Color (RGB)
    ├── Depth (R32F)
    └── Motion Vectors (RG16F)
    │
    ▼
┌─────────────────────┐
│ Feature Extraction  │
│ (3x3 Conv + ReLU)   │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ Upsampling Block    │
│ (Pixel Shuffle)     │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ Refinement Network  │
│ (Residual Blocks)   │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ Output Projection   │
│ (3x3 Conv)          │
└─────────┬───────────┘
          │
          ▼
Output Frame (4K)
```

### 2.2 Layer Details

#### Feature Extraction
```
Input: 3x1920x1080 (RGB color)
Conv2d: 3 -> 64 channels, 3x3 kernel, stride 1, padding 1
ReLU activation
Conv2d: 64 -> 64 channels, 3x3 kernel, stride 1, padding 1
ReLU activation
Output: 64x1920x1080
```

#### Depth Fusion
```
Input: 1x1920x1080 (depth)
Conv2d: 1 -> 16 channels, 3x3 kernel, stride 1, padding 1
Output: 16x1920x1080
Concat with features: 64+16 = 80 channels
```

#### Motion Fusion
```
Input: 2x1920x1080 (motion vectors)
Conv2d: 2 -> 16 channels, 3x3 kernel, stride 1, padding 1
Output: 16x1920x1080
Concat with features: 80+16 = 96 channels
```

#### Upsampling
```
Input: 96x1920x1080
Conv2d: 96 -> 256 channels, 3x3 kernel, stride 1, padding 1
PixelShuffle: upscale 2x
Output: 64x3840x2160
Conv2d: 64 -> 64 channels, 3x3 kernel, stride 1, padding 1
ReLU activation
Output: 64x3840x2160
```

#### Refinement
```
Input: 64x3840x2160
Residual Block 1:
    Conv2d: 64 -> 64, 3x3, ReLU
    Conv2d: 64 -> 64, 3x3
    Add + ReLU
    Output: 64x3840x2160

Residual Block 2:
    Conv2d: 64 -> 64, 3x3, ReLU
    Conv2d: 64 -> 64, 3x3
    Add + ReLU
    Output: 64x3840x2160
```

#### Output
```
Input: 64x3840x2160
Conv2d: 64 -> 3 channels, 3x3 kernel, stride 1, padding 1
Output: 3x3840x2160 (RGB)
```

---

## 3. ONNX Representation

### 3.1 Model Metadata

```json
{
    "format_version": 1,
    "model_type": "neural_upscaler",
    "model_name": "nrr_upscaler_v0.1",
    "model_version": "0.1.0",

    "input_spec": {
        "color": {
            "name": "color",
            "format": "RGB8",
            "shape": [1, 3, 1920, 1080],
            "dtype": "float32",
            "required": true
        },
        "depth": {
            "name": "depth",
            "format": "R32F",
            "shape": [1, 1, 1920, 1080],
            "dtype": "float32",
            "required": true
        },
        "motion_vectors": {
            "name": "motion",
            "format": "RG16F",
            "shape": [1, 2, 1920, 1080],
            "dtype": "float32",
            "required": true
        }
    },

    "output_spec": {
        "color": {
            "name": "output",
            "format": "RGB8",
            "shape": [1, 3, 3840, 2160],
            "dtype": "float32",
            "required": true
        }
    },

    "capabilities_required": {
        "fp32": true,
        "compute_shader": true,
        "minimum_vram_mb": 4096
    },

    "tags": ["upscaling", "temporal"]
}
```

### 3.2 Weights

Total parameters: ~2.5M
- Feature extraction: 0.1M
- Depth fusion: 0.05M
- Motion fusion: 0.05M
- Upsampling: 0.35M
- Refinement: 1.5M
- Output: 0.1M

---

## 4. Input/Output Tensor Mapping

### 4.1 Color Input

```
NRR Texture (RGB8) → ONNX Tensor (float32)
    │
    ├── Upload to GPU/CPU memory
    ├── Convert R8G8B8 → float32 [0, 1]
    └── Reshape to [1, 3, H, W]
```

### 4.2 Depth Input

```
NRR Texture (R32F) → ONNX Tensor (float32)
    │
    ├── Upload to GPU/CPU memory
    └── Reshape to [1, 1, H, W]
```

### 4.3 Motion Input

```
NRR Texture (RG16F) → ONNX Tensor (float32)
    │
    ├── Upload to GPU/CPU memory
    ├── Convert R16G16 → float32
    └── Reshape to [1, 2, H, W]
```

### 4.4 Output

```
ONNX Tensor (float32) → NRR Texture (RGB8)
    │
    ├── Extract float32 [0, 1] values
    ├── Clamp to [0, 1]
    ├── Convert to R8G8B8
    └── Download to GPU memory
```

---

## 5. Execution Flow

### 5.1 Synchronous Execution

```
1. Input preparation
   ├── Texture upload to memory
   ├── Format conversion
   └── Tensor creation

2. Inference
   └── ONNX Runtime execute

3. Output processing
   ├── Tensor extraction
   ├── Format conversion
   └── Texture download
```

### 5.2 Asynchronous Execution (Future)

```
1. Frame n input preparation (CPU or GPU)
2. Submit to execution queue
3. Frame n+1 input preparation (parallel)
4. Wait for frame n completion
5. Process frame n output
6. Submit frame n+1
```

---

## 6. Backends

### 6.1 CPU Backend

- Uses ONNX Runtime CPU execution provider
- Suitable for testing and low-end hardware
- Slower but always available

### 6.2 Vulkan Backend (Future)

- Uses ONNX Runtime with custom Vulkan EP
- textures stay on GPU
- Compute shaders for preprocessing/postprocessing

### 6.3 NVIDIA Backend (Phase 7)

- Uses ONNX Runtime CUDA EP
- Tensor Cores for FP16/FP8 inference
- Latest optimization

---

## 7. Testing

### 7.1 Basic Test

```
Input: Solid color frame (gray 0.5)
Expected: Scaled output (same gray 0.5)
Validation: Output values match input (scaled)
```

### 7.2 Edge Test

```
Input: Gradient pattern
Expected: Smoothly interpolated output
Validation: No artifacts, smooth transitions
```

### 7.3 Performance Test

```
Measure: Inference time
Target: < 16ms for 1080p→4K (60fps path)
```

---

*End of Model Architecture*
