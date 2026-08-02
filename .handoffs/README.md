# Handoffs

Cross-machine continuity records, written by [NouGenRelay](https://github.com/who-visions/nougenrelay)
and carried by git. One file per leg, named `<UTC timestamp>__<machine>__<agent>`.

Machines are slugified the same way everywhere in the fleet — `NOUGEN_MACHINE`,
else the hostname, lowercased — so one grep finds a box's commits, its handoffs,
and its `dailies/` entries.

```bash
relay check                                    # has another machine moved?
relay claim take -s token_tracker.py -g "…"    # announce BEFORE you work
relay claim release -s token_tracker.py
relay create -g "what you did" -m "where you left off"
```

`relay check` first, always. This repo is worked from more than one box, and the
cost of finding that out afterwards is a duplicated afternoon — which is exactly
how the pricing fix in #4 came to be written twice.
