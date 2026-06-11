"""driftwatch.graph — decision-graph forensics (Neo4j).

ROADMAP / stub in v1alpha1. The intended exporter (decision chains → a queryable
Neo4j graph answering "why did the agent drift, and what did it do just before?",
reusing the Observability Summit neo4j_exporter + Cypher queries) is not implemented
yet. Nothing writes to Neo4j today; the demo's Neo4j container is behind a compose
`forensics` profile and off by default. This package is intentionally empty until the
exporter lands.
"""

