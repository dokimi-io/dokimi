# test-orchestrator

## Overview
This is a Test Orchestration Framework for orchestrating Tests inside of Kubernetes between nodes and clusters. 
For this we define which tools we are using for the tests, how the tests are orchestrated and which metrics we are using for the evulation and validation. 


## Prerequisites

## Testing Tools

### iperf2


## Metrics

### Metrics Collection 
The metrics are collected inside of prometheus, either through prometheus node exporter oder special exporters of the used tools which are running as sidecar alongside the tool container inside of the pods. 

### Type of Metrics
There Are Pod and Node metrics.
* Pod metrics are coming from the sidecar container that are exposed and collected with prometheus
* Node metrics are cimong from the nodes exporter which is running on each node inside of the cluster and are also collected inside of prometheus

## Technical

### Orchestration

### Testrun

### Verifications

### Output

