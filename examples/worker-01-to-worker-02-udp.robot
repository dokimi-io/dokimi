*** Settings ***
Documentation       Directed 1 Gbit/s UDP test between two named nodes.
Resource            ../robot/resources/TestOrchestrator.resource

*** Variables ***
${SOURCE_NODE}           worker-01
${TARGET_NODE}           worker-02
${TARGET_BANDWIDTH}      1G
${TEST_DURATION}         30s

*** Test Cases ***
Directed UDP Test
    Run Network Test
    ...    type=iperf2
    ...    protocol=udp
    ...    source=${SOURCE_NODE}
    ...    target=${TARGET_NODE}
    ...    bandwidth=${TARGET_BANDWIDTH}
    ...    duration=${TEST_DURATION}

    Throughput Should Be     At Least    950    megabits per second
    Packet Loss Should Be    At Most     0      percent
