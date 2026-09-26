*** Settings ***
Documentation       worker-01-to-worker-02-udp
Resource            resources/Dokimi.resource

*** Variables ***
${SOURCE_NODE}         worker-01
${TARGET_NODE}         worker-02
${TEST_TYPE}           iperf2
${PROTOCOL}            udp
${TEST_DURATION}       30s
${TARGET_BANDWIDTH}    1G
${MONITORING_MODE}     managed
${RESULTS_DIRECTORY}   ./results

*** Test Cases ***
worker-01-to-worker-02-udp
    Run Network Test
    ...    type=${TEST_TYPE}
    ...    protocol=${PROTOCOL}
    ...    source=${SOURCE_NODE}
    ...    target=${TARGET_NODE}
    ...    bandwidth=${TARGET_BANDWIDTH}
    ...    duration=${TEST_DURATION}

    Throughput Should Be     At Least    950    megabits per second
    Packet Loss Should Be    At Most     0      percent
