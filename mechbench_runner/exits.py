"""Exit codes: the interface between the runner and whatever supervises it.

An OS supervisor understands nothing about this process except the code
it exited with, so every "should I come back" decision has to be
expressed here. launchd's `KeepAlive{SuccessfulExit: false}` and
systemd's `Restart=on-failure` then implement the policy natively and we
write no supervision code at all.

    0   deliberate — SIGTERM, signed out, nothing to do. Stay stopped.
    1   crashed, wedged, or an unhandled error. Come back, throttled.
    75  asked to come back (an approved update, task 000296).

`75` is `EX_TEMPFAIL`. Both non-zero codes restart identically; the
distinction exists so a person reading a log can tell an intended
restart from a fault.

The subtle one is `0` for "nothing to do". A runner with no credentials
must not crash: under a supervisor a crash means restart, and a machine
that is simply not signed in would spin against the throttle forever
instead of sitting quietly until someone runs `login`.
"""

from __future__ import annotations

#: Deliberate stop. Do not restart.
EXIT_OK = 0

#: Crashed or wedged. Restart.
EXIT_CRASH = 1

#: A restart this process asked for. Restart.
EXIT_RESTART = 75
