# Examples

Robot and YAML are peer authoring formats. Neither is consumed directly: both `parse()`
into one canonical, in-memory test definition model, and the orchestration library
consumes only that model (ADR-04-05).

The two files here express the **same test** in both formats. They are the examples from
the repository README, kept here so they can be exercised once the format plugins land
with M1-03 (adrs#38). Nothing parses them yet.
