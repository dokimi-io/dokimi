#!/bin/sh
# The iperf2 server role: receive, and write a report for every connection (ADR-02-01).
#
# The server is the pod that measures packet loss and jitter: a UDP client's own summary
# carries neither. So its reports go to the results directory as `iperf -y C` lines, where
# its exporter sidecar parses them (ADR-02-02), each pod exporting its own view.
#
# It receives the warm-up pass as well as the measurement, one report each, plus a sum
# line over both. The meta file says which source port the measurement comes from, so
# the exporter picks that connection's line and ignores the rest.
#
# iperf writes each report line as the connection ends, not when it exits, so the file
# is current while the server keeps running. It never exits on its own: the pod is
# deleted with the run.
#
# Usage:
#   dokimi-iperf2-server --protocol udp|tcp --port N --source-port N \
#       --warmup SECONDS --results DIR

set -eu

usage() {
    echo "dokimi-iperf2-server: $1" >&2
    exit 64
}

protocol="" port="" source_port="" warmup="" results=""
while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || usage "$1 needs a value"
    case $1 in
        --protocol) protocol=$2 ;;
        --port) port=$2 ;;
        --source-port) source_port=$2 ;;
        --warmup) warmup=$2 ;;
        --results) results=$2 ;;
        *) usage "unknown argument $1" ;;
    esac
    shift 2
done

for value in "$port" "$source_port" "$warmup"; do
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

cat >"$results/iperf2.meta" <<META
role=server
warmup_seconds=$warmup
measurement_source_port=$source_port
META

echo "server: listening on $port/$protocol, reports in $results/iperf2.csv" >&2
# exec, so that iperf receives the pod's SIGTERM itself and deletion is not held up for
# the grace period by a shell that ignores it.
# shellcheck disable=SC2086 # $udp is deliberately empty or one word
exec iperf -s -p "$port" $udp -y C >"$results/iperf2.csv"
