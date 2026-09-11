"""NRR frame descriptor schema (versioned plain-dict dataclasses).

Mirrors the NRR frame contract (SamurAI-Official/NRR specification/
frame_contract.md) at the *descriptor* level: the primary never sends raw
pixels -- it sends an NRRFrameDescriptor (resolution, pixel format, model /
reference ids, temporal state).  The peripheral worker runs nrr_render()
locally and returns an NRRRenderResult (output handle + RenderStats).

All dicts carry ``schema_version`` so the mesh can evolve without breaking
older peers.  Validation is fail-closed: malformed descriptors raise
ValueError before anything is published.
"""