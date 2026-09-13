"""Context Manager and memory layer built on the self-hosted Mem0 service.

Layout follows the handoff spec (section 17): `src.memory` is the long-term
memory layer, `src.context` is the context-selection layer above it.
"""
