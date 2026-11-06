bump: minor
- `HostPlacement` measures the host (`measure` writes one `host/measure` reading of memory, disk and the consumers' resident size, with drift from the declaration reported as observations), resolves the figures of a document's "measure" entries (`resolve`, `sample_consumer`), and `place`, `hold` and `headroom` refuse or narrow a start that would leave less than the floor of measured memory or disk, or that has no fresh measurement (`unmeasured`); the probes are `quarrylith.allocator.probes`
- the `measured` gauge ends its search for a node never measured with one full read
