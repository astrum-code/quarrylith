bump: minor
- A record's `repo:` and `legacy:` labels may repeat, each value once (`schema.MULTI_VALUED_LABELS`), so an item in two repos carries both; every other key stays one label per record, and no label may be there twice. A task's own labels of a key replace all of its item's of that key in `effective_labels`
