bump: patch
- A load on Henge writes in batches, about 3 puts an event where it made about 10, with one sync a batch: a load is several times faster, with the same dump, journal and resume
- On Henge, `check()` reports a load in progress, in place of a clean record with fewer events; an open redoes what a load's last batch left
