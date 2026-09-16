/** Build a schema-shaped emerge.yaml for a user-owned MCP (no secrets in the file). */

export type McpTransportKind = 'sse' | 'stdio'

export interface McpConnectInput {
  name: string
  description?: string
  transport: McpTransportKind
  endpoint?: string
  command?: string
  args?: string[]
  /** Vault placeholder name, e.g. MCP_TOKEN. Value is stored separately. */
  authVar?: string
}

const DID_SLUG = /[^a-z0-9._-]+/g

export function agentDidFromName(name: string): string {
  const slug =
    name.trim().toLowerCase().replace(DID_SLUG, '-').replace(/^-+|-+$/g, '').slice(0, 48) ||
    'mcp'
  return `did:orcha:agent:${slug}`
}

export function buildMcpEmergeYaml(input: McpConnectInput): string {
  const name = input.name.trim()
  if (!name) throw new Error('name is required')
  const did = agentDidFromName(name)
  const description = (input.description ?? `User MCP: ${name}`).trim()
  const authVar = input.authVar?.trim()

  let transportBlock: string
  let health: string
  if (input.transport === 'stdio') {
    const command = input.command?.trim()
    if (!command) throw new Error('command is required for stdio MCP')
    const args = (input.args ?? []).map((a) => `      - ${JSON.stringify(a)}`).join('\n')
    const envLine = authVar ? `\n    env:\n      ${authVar}: "\${${authVar}}"` : ''
    transportBlock = `  transport:
    type: stdio
    command: ${JSON.stringify(command)}${args ? `\n    args:\n${args}` : ''}${envLine}`
    health = 'http://127.0.0.1:9/health'
  } else {
    const endpoint = input.endpoint?.trim()
    if (!endpoint || !/^https?:\/\//i.test(endpoint)) {
      throw new Error('endpoint must be an http(s) URL for SSE MCP')
    }
    transportBlock = `  transport:
    type: sse
    endpoint: ${JSON.stringify(endpoint)}`
    health = endpoint
  }

  const authBlock = authVar
    ? `
  auth_strategies:
    - id: strategy_bearer
      type: http_bearer
      config:
        token_vault_ref: ${authVar}`
    : ''

  return `identity:
  id: ${JSON.stringify(did)}
  name: ${JSON.stringify(name)}
  version: "1.0.0"
  description: ${JSON.stringify(description)}
  tags:
    - mcp
    - user

protocol:
  type: mcp
  version: "1.0"
${transportBlock}

health_endpoint: ${JSON.stringify(health)}

security:
  transport_layer:
    type: none${authBlock}

payment:
  enabled: false
`
}
