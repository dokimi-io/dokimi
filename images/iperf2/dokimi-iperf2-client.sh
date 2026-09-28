#!/bin/sh
# The iperf2 client role: wait to be started, warm up, measure, report (ADR-02-01).
#
# The pod starts idle, listening on the trigger port, so that both pods can pass the
# readiness gate before any traffic is sent (ADR-05-07). start() connects and sends the
# server's pod IP, which is only known once the server is Ready (ADR-05-03). There is no
# exec: this script is the whole contract between the orchestrator and the pod.
#
# Completion is this process exiting. The final `iperf -y C` summary line - the
# completion marker - goes to the termination message, where the orchestrator reads it
# from the pod's status, and the full output stays in the results directory for the
# exporter sidecar to parse (ADR-02-02).
#
# Usage:
#   dokimi-iperf2-client --protocol udp|tcp --port N --trigger-port N \
#       --bandwidth BITS_PER_SECOND --duration SECONDS --warmup SECONDS --results DIR

set -eu

usage() {
    echo "dokimi-iperf2-client: $1" >&2
    exit 64
}

protocol="" port="" trigger_port="" bandwidth="" duration="" warmup="" results=""
while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || usage "$1 needs a value"
    case $1 in
        --protocol) protocol=$2 ;;
        --port) port=$2 ;;
        --trigger-port) trigger_port=$2 ;;
        --bandwidth) bandwidth=$2 ;;
        --duration) duration=$2 ;;
        --warmup) warmup=$2 ;;
        --results) results=$2 ;;
        *) usage "unknown argument $1" ;;
    esac
    shift 2
done

for value in "$port" "$trigger_port" "$bandwidth" "$duration" "$warmup"; do
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

# Blocks until start() connects. One connection, then the listener is gone: a second
# trigger cannot start a second measurement.
server=$(nc -l -p "$trigger_port" | head -n 1)
case $server in
    "" | *[!0-9.]*) echo "dokimi-iperf2-client: start sent '$server', not an IPv4 address" >&2
        exit 65 ;;
esac

# The discarded warm-up pass (ADR-04-04): same path and parameters, output thrown away.
# Its failure is reported, not fatal - the measurement that follows is what counts.
if [ "$warmup" != "0" ]; then
    echo "warm-up: ${warmup}s against $server, discarded" >&2
    # shellcheck disable=SC2086 # $udp is deliberately empty or one word
    if ! iperf -c "$server" -p "$port" $udp -b "$bandwidth" -t "$warmup" >/tmp/warmup.log 2>&1; then
        echo "warm-up: iperf exited non-zero; see below" >&2
        cat /tmp/warmup.log >&2
    fi
else
    echo "warm-up: disabled" >&2
fi

echo "measurement: ${duration}s against $server" >&2
status=0
# shellcheck disable=SC2086
iperf -c "$server" -p "$port" $udp -b "$bandwidth" -t "$duration" -y C \
    >"$results/iperf2.csv" 2>"$results/iperf2.stderr" || status=$?

cat "$results/iperf2.csv"
cat "$results/iperf2.stderr" >&2
tail -n 1 "$results/iperf2.csv" >/dev/termination-log 2>/dev/null || true
exit "$status"
