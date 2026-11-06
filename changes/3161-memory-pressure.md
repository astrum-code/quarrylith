bump: minor
- The allocator also refuses new work, naming the reading, when the host reports memory pressure of warn or worse, or when its swap is nearly full (used past 8 GiB and 85% of its total) while under 40% of memory is free; `alloc show` says what the host reads
