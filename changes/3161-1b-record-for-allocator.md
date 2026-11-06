bump: minor
- `Record.replace_reservation(id, amount, why)` swaps a live reservation for one of a new amount in a single `reserve.replace` event, checked without the reservation it replaces and refused whole when the new amount does not fit; and the `measured` gauge reports a node's latest measured figures (the host's, and each holder's use)
