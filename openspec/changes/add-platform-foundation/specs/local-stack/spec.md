# Spec Delta: local-stack

## Purpose

Provides the single local docker-compose stack (database, migrations, reranker, query service, tracing) that every component runs against, including the replay overlay used by the CI eval gate.

## ADDED Requirements

### Requirement: One-command stack startup
The repository SHALL provide a compose stack in `infra/docker-compose.yml` with the services `postgres`, `flyway`, `reranker`, `service`, `otel-collector`, `tempo` and `grafana`, using the images, container ports, default host ports and health checks defined in the foundation design contract C2.

#### Scenario: Stack comes up healthy
- **WHEN** a developer runs `make up` on a machine with Docker running and a `.env` copied from `.env.example`
- **THEN** every long-running service reaches a healthy state and the `flyway` service exits with code 0

#### Scenario: Infra subset without application images
- **WHEN** `docker compose --env-file .env -f infra/docker-compose.yml up -d --wait postgres flyway otel-collector tempo grafana` is run before the service and reranker exist
- **THEN** those five services start and become healthy (flyway completes) without requiring the application images

### Requirement: Migrations applied before the service starts
The `flyway` service SHALL apply every migration in `infra/migrations` to the `rag` database before the `service` container starts.

#### Scenario: Service waits for migrations
- **WHEN** the stack starts from empty volumes
- **THEN** the `service` container does not start until `flyway` has completed successfully

### Requirement: Host model server reachable from containers
Containers that call models SHALL reach host Ollama at `http://host.docker.internal:11434` on both Docker Desktop and Linux hosts.

#### Scenario: Linux host gateway
- **WHEN** the stack runs on a Linux Docker engine
- **THEN** `host.docker.internal` resolves inside the `service` and `reranker` containers to the host gateway

### Requirement: Traces exported to a local backend
The stack SHALL accept OTLP traces on ports 4317 (gRPC) and 4318 (HTTP) and make them viewable in Grafana through a provisioned Tempo datasource.

#### Scenario: Trace visible in Grafana
- **WHEN** a span is sent over OTLP HTTP to `localhost:4318`
- **THEN** the trace can be retrieved from Tempo at `localhost:3200` and from Grafana's Tempo datasource without manual configuration

### Requirement: Replay overlay for deterministic evaluation
The repository SHALL provide `infra/docker-compose.replay.yml` that adds a `wiremock` service and points the query service's model and reranker URLs at it, supporting a record mode (proxying to host Ollama and the reranker) and a replay mode (serving only recorded mappings), as defined in contract C9.

#### Scenario: Replay mode serves only recorded traffic
- **WHEN** the overlay runs with `WIREMOCK_MODE=replay` and the service sends a request that has no recorded mapping
- **THEN** wiremock responds 404 and no request is proxied to any real model server

#### Scenario: Record mode proxies to real dependencies
- **WHEN** the overlay runs with `WIREMOCK_MODE=record` and the service calls `/api/embed`
- **THEN** the request is forwarded to host Ollama and the exchange is available in the wiremock journal for snapshotting
