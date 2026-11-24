#!/bin/sh
# The iperf2 client role: wait to be started, warm up, measure, report.
#
# The pod starts idle, listening on the trigger port, so that both pods can pass the
# readiness gate before any traffic is sent. start() connects and sends the
# server's pod IP, which is only known once the server is Ready. There is no
# exec: this script is the whole contract between the orchestrator and the pod.
#
# The warm-up and the measurement each send from their own fixed source port. The server
# reports every connection it receives, and the source port is how its exporter tells the
# measurement from the warm-up without guessing from their order: a warm-up that never
# connected would otherwise make the measurement look like the connection to skip.
#
# Completion is this process exiting. The final `iperf -y C` summary line - the
# completion marker - goes to the termination message, where the orchestrator reads it
# from the pod's status, and the full output stays in the results directory for the
# exporter sidecar to parse.
#
# With --probe-dns NAME the client is the run-level preflight's probe: the
# same pod, started the same way, but instead of warming up and measuring it makes a
# one-second exchange with the server and resolves NAME, the two checks only a pod can
# make. It reports both as key=value lines in the termination message and exits 0 if both
# passed, 69 if no traffic reached the server, 68 if only the name did not resolve.
#
# Usage:
#   dokimi-iperf2-client --protocol udp|tcp --port N --trigger-port N \
#       --source-port N --warmup-source-port N \
#       --bandwidth BITS_PER_SECOND --duration SECONDS --warmup SECONDS --results DIR \
#       [--probe-dns NAME]

set -eu

usage() {
    echo "dokimi-iperf2-client: $1" >&2
    exit 64
}

# A dotted quad with four octets of at most 255. Digits and dots alone are not enough:
# 999.1 would pass that, and iperf would resolve it as a host name.
valid_ipv4() {
    case $1 in
        "" | *[!0-9.]* | .* | *. | *..*) return 1 ;;
    esac
    old_ifs=$IFS
    IFS=.
    # shellcheck disable=SC2086 # split on the dots, deliberately
    set -- $1
    IFS=$old_ifs
    [ $# -eq 4 ] || return 1
    for octet do
        [ ${#octet} -le 3 ] && [ "$octet" -le 255 ] || return 1
    done
}

protocol="" port="" trigger_port="" source_port="" warmup_source_port=""
bandwidth="" duration="" warmup="" results="" probe_dns=""
while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || usage "$1 needs a value"
    case $1 in
        --protocol) protocol=$2 ;;
        --port) port=$2 ;;
        --trigger-port) trigger_port=$2 ;;
        --source-port) source_port=$2 ;;
        --warmup-source-port) warmup_source_port=$2 ;;
        --bandwidth) bandwidth=$2 ;;
        --duration) duration=$2 ;;
        --warmup) warmup=$2 ;;
        --results) results=$2 ;;
        --probe-dns) probe_dns=$2 ;;
        *) usage "unknown argument $1" ;;
    esac
    shift 2
done

for value in "$port" "$trigger_port" "$source_port" "$warmup_source_port" "$bandwidth" \
    "$duration" "$warmup"; do
    case $value in
        "" | *[!0-9.]*) usage "expected a number, got '$value'" ;;
    esac
done
case $protocol in
    udp) udp="-u" ;;
    tcp) udp="" ;;
    *) usage "unknown protocol '$protocol'" ;;
esac
[ -n "$results" ] || usage "--results is required"

# What the exporter needs to read this pod's output, next to the output itself.
cat >"$results/iperf2.meta" <<META
role=client
warmup_seconds=$warmup
measurement_source_port=$source_port
META

# Blocks until start() connects. One connection, then the listener is gone: a second
# trigger cannot start a second measurement.
server=$(nc -l -p "$trigger_port" | head -n 1)
if ! valid_ipv4 "$server"; then
    echo "dokimi-iperf2-client: start sent '$server', not an IPv4 address" >&2
    exit 65
fi
# The server's address, for the exporter: the pair's target, known from now on.
printf '%s\n' "$server" >"$results/iperf2.peer"
# The readiness probe passes on this from now on: started is as ready as listening was,
# and a client reported NotReady for its whole measurement would be skipped by anything
# that selects Ready endpoints, the exporter's scrape among them.
: >/tmp/dokimi-started

if [ -n "$probe_dns" ]; then
    connectivity=failed dns=skipped
    # Low and short: the probe proves the path carries traffic, not what it carries.
    # KILL, because iperf2 sits out a dropped TCP connect for minutes and ignores TERM.
    # shellcheck disable=SC2086
    timeout -s KILL 15 iperf -c "$server" -p "$port" $udp -b 10000000 -t 1 \
        >/tmp/probe.log 2>&1 || true
    cat /tmp/probe.log >&2
    # iperf2 exits 0 even when nothing arrived, so its output decides. Over UDP only the
    # server's report, sent back at the end, proves the datagrams reached it; over TCP
    # the connection line is printed once the handshake has completed.
    case $protocol in
        udp) evidence="Server Report:" ;;
        *) evidence=" connected with " ;;
    esac
    if grep -q "$evidence" /tmp/probe.log; then
        connectivity=ok
        # Only once the pod network works: without it the cluster's DNS is unreachable
        # too, and reporting dns would point at the wrong fix.
        if timeout -s KILL 15 getent hosts "$probe_dns" >&2; then dns=ok; else dns=failed; fi
    fi
    echo "probe: connectivity=$connectivity dns=$dns" >&2
    printf 'server=%s\nconnectivity=%s\ndns=%s\n' "$server" "$connectivity" "$dns" \
        2>/dev/null >/dev/termination-log || true
    [ "$connectivity" = ok ] || exit 69
    [ "$dns" = ok ] || exit 68
    exit 0
fi

# The discarded warm-up pass: same path and parameters, output thrown away.
# Its failure is reported, not fatal - the measurement that follows is what counts.
if [ "$warmup" != "0" ]; then
    echo "warm-up: ${warmup}s against $server, discarded" >&2
    # shellcheck disable=SC2086 # $udp is deliberately empty or one word
    if ! iperf -c "$server" -p "$port" -B "0.0.0.0:$warmup_source_port" $udp \
        -b "$bandwidth" -t "$warmup" >/tmp/warmup.log 2>&1; then
        echo "warm-up: iperf exited non-zero; see below" >&2
        cat /tmp/warmup.log >&2
    fi
else
    echo "warm-up: disabled" >&2
fi

echo "measurement: ${duration}s against $server" >&2
status=0
# shellcheck disable=SC2086
iperf -c "$server" -p "$port" -B "0.0.0.0:$source_port" $udp -b "$bandwidth" \
    -t "$duration" -y C >"$results/iperf2.csv" 2>"$results/iperf2.stderr" || status=$?

# iperf2's own exit status, for the exporter: a run that ended without a measurement is
# an execution error, not merely a result that has not arrived yet.
printf '%s\n' "$status" >"$results/iperf2.status"

cat "$results/iperf2.csv"
cat "$results/iperf2.stderr" >&2
summary=$(tail -n 1 "$results/iperf2.csv")
# Silenced first, so that it already applies if the redirect after it fails: outside a
# Kubernetes pod, as in CI, there is no termination log to write.
printf '%s\n' "$summary" 2>/dev/null >/dev/termination-log || true

# iperf2 exits 0 even when it could not connect at all. A measurement without a summary
# line measured nothing, and must not look like one that did.
if [ "$status" -eq 0 ] && [ -z "$summary" ]; then
    echo "dokimi-iperf2-client: iperf exited 0 without a summary line; nothing was measured" >&2
    exit 69
fi
exit "$status"
