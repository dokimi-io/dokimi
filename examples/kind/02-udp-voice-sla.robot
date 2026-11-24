*** Settings ***
Documentation       udp-voice-sla
Resource            resources/Dokimi.resource

*** Variables ***
${SOURCE_NODE}         dokimi-worker2
${TARGET_NODE}         dokimi-worker3
${TEST_TYPE}           iperf2
${PROTOCOL}            udp
${TEST_DURATION}       20s
${TARGET_BANDWIDTH}    50M
${MONITORING_MODE}     managed
${RESULTS_DIRECTORY}   ./results

*** Test Cases ***
udp-voice-sla
    Run Network Test
    ...    type=${TEST_TYPE}
    ...    protocol=${PROTOCOL}
    ...    source=${SOURCE_NODE}
    ...    target=${TARGET_NODE}
    ...    bandwidth=${TARGET_BANDWIDTH}
    ...    duration=${TEST_DURATION}

    Throughput Should Be     At Least    49     megabits per second
    Packet Loss Should Be    At Most     0.1    percent
    Jitter Should Be         At Most     1      milliseconds
