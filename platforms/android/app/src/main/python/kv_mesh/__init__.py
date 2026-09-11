"""KV-cache / context mesh split -- offline prototype.

Proves the mesh protocol and memory accounting for splitting transformer KV
caches and context windows across peripheral devices, WITHOUT running real
distributed inference.  See docs/kv_cache_mesh_split.md.
"""
