# OpenCode upgrade investigation

The installed `1.18.32` CLI's `upgrade --method curl` implementation fetches
`https://opencode.ai/install` inside the OpenCode process, reads the response
body before launching its installer, and captures installer output while it
waits. The CLI prints the version transition before entering the spinner, so a
stalled internal request or child can look silent. The observed process tree
had no `curl` child, consistent with this in-process fetch. The CLOSE-WAIT
socket shows that the peer closed its side while OpenCode still held the
socket; it does not establish why OpenCode failed to finish. Available source
evidence does not identify the exact hang cause.

The wrapper keeps the native updater first, logs PID, elapsed time, and
observed output, emits heartbeats and process/socket diagnostics, and applies
a 10 minute hard timeout to each updater phase. Persistent CLOSE-WAIT with no
output is diagnosed earlier. Only a timeout or that precise stall can trigger
fallback, after the owned process group is stopped and the original managed
binary is still healthy. Fallback uses OpenCode's official
`https://opencode.ai/install` script and the latest tag from the official
OpenCode GitHub releases API, with TLS checks, the managed
`/home/ubuntu/.opencode/bin` directory first in `PATH`, and an exact
post-install version check. Quick native failures remain failures and do not
trigger retry. The wrapper never selects the NVM/npm installation.

The observed native hang's underlying cause remains unproven. If the official
installer or release API is unavailable, the update fails with the original
native updater status and reports whether the managed binary remains healthy.
The fixture regression matrix is in `tests/test-opencode-wrapper.sh` and does
not use the network.
