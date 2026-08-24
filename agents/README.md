# Agents

Standalone AI agents that can be registered with Metaorcha. Each agent declares its identity, protocol, and capabilities in an `emerge.yaml` manifest.

## Fleet

| Agent | Protocol | Description |
|-------|----------|-------------|
| [web-scraper](./web-scraper/) | A2A (HTTP) | URL fetching, data extraction, OAuth-authenticated scraping |
| [search-agent](./search-agent/) | MCP (SSE) | Documentation search via Serper |
| [notion-mcp](./notion-mcp/) | MCP (stdio) | Notion workspace CRUD — search, create, update pages and databases |
| [notion-research](./notion-research/) | A2A (HTTP) | Web research and structured note creation in Notion |
| [lead-gen-agent](./lead-gen-agent/) | A2A (HTTP) | Lead discovery, enrichment, and CRM export |
| [google-workspace-orchestrator](./google-workspace-orchestrator/) | A2A (HTTP) | Gmail, Calendar, Drive, and Sheets automation |
| [ecommerce-automation](./ecommerce-automation/) | A2A (HTTP) | Shopify management and social publishing |

## How Agents Work

Each agent is an independent service. At startup it exposes either:
- **A2A**: an HTTP server with `/.well-known/agent.json` and `POST /tasks/send`
- **MCP**: a stdio or SSE server implementing the Model Context Protocol

Registering an agent via `POST /v1/agents/register` to the Registry service uploads the `emerge.yaml`, harvests capabilities, and publishes a `registry.agent.registered` Kafka event. Planning & Discovery consumes this event to index the agent for search.

## emerge.yaml Structure

```yaml
identity:
  id: "did:orcha:agent:<name>"   # Unique DID
  name: "Human-readable name"
  version: "1.0.0"
  description: "What the agent does"
  tags: ["tag1", "tag2"]

protocol:
  type: "a2a" | "mcp"
  version: "1.0"
  transport:
    type: "http" | "stdio"
    endpoint: "http://host:port"      # A2A only

skills:                               # A2A only — optional skill list
  - id: "skill-id"
    name: "Skill Name"
    description: "What it does"

health_endpoint: "http://host:port/health"  # null for MCP
```

## Running an Agent Locally

**A2A agents (Python/FastAPI):**
```bash
cd agents/<name>
pip install -e .
uvicorn src.server:app --port <port>
```

**MCP agents (Node.js):**
```bash
cd agents/<name>
npm install && npm run build
node dist/index.js
```

See each agent's `README.md` for agent-specific setup and environment variables.
