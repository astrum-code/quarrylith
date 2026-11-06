bump: minor
- The allocator also refuses new work, naming the reading, when the host reports memory pressure of warn or worse or its swap is nearly full (used past 8 GiB and 85% of its total); `alloc show` says what the host reads
