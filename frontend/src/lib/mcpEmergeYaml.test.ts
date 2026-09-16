import { agentDidFromName, buildMcpEmergeYaml } from './mcpEmergeYaml.ts'

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(msg)
}

assert(agentDidFromName('My Weather') === 'did:orcha:agent:my-weather', 'slug did')
assert(agentDidFromName('').startsWith('did:orcha:agent:'), 'empty name fallback')

const sse = buildMcpEmergeYaml({
  name: 'Docs MCP',
  transport: 'sse',
  endpoint: 'https://example.com/mcp',
  authVar: 'MCP_TOKEN',
})
assert(sse.includes('type: mcp'), 'protocol mcp')
assert(sse.includes('type: sse'), 'sse transport')
assert(!sse.includes('sk-'), 'no raw secret')
assert(sse.includes('token_vault_ref: MCP_TOKEN'), 'vault ref')
assert(sse.includes('did:orcha:agent:docs-mcp'), 'did')

const stdio = buildMcpEmergeYaml({
  name: 'Local',
  transport: 'stdio',
  command: 'npx',
  args: ['-y', 'demo-mcp'],
})
assert(stdio.includes('type: stdio'), 'stdio')
assert(stdio.includes('command: "npx"'), 'command')

let threw = false
try {
  buildMcpEmergeYaml({ name: 'x', transport: 'sse' })
} catch {
  threw = true
}
assert(threw, 'sse without endpoint fails')

console.log('mcpEmergeYaml ok')
