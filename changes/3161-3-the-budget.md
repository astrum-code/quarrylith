bump: minor
- `HostPlacement.place` refuses a run `budget` while its provider's budget readings hold it (a warning past `warning_max`, a stop or a rejection until the reset, the tighter of the provider's windows), and `headroom` narrows a provider's budget resource to the allowance; `budget_shorts` and `allowance` are exported by `quarrylith.allocator`
