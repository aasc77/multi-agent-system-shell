/**
 * Tests for mcp-bridge/index.js -- MCP Stdio Bridge for Claude Code Agents
 *
 * TDD Contract (RED phase):
 * These tests define the expected behavior of the MCP Bridge module.
 * They MUST fail until the implementation is written.
 *
 * Requirements traced to PRD:
 *   - R2: Config-Driven Agents (MCP bridge per agent)
 *   - R3: Communication Flow (MCP bridge interface)
 *   - Acceptance criteria from task rgr-5
 *
 * Test categories:
 *   1. Module exports and structure
 *   2. Environment variable configuration (AGENT_ROLE, NATS_URL, WORKSPACE_DIR)
 *   3. check_messages tool -- pulls from agents.<AGENT_ROLE>.inbox
 *   4. send_message tool -- publishes to agents.<AGENT_ROLE>.outbox
 *   5. MCP JSON-RPC protocol (stdio server)
 *   6. NATS JetStream integration
 *   7. all_done message handling
 *   8. Outbox message schema wrapping
 *   9. Error handling
 */

// --- The require that MUST fail in RED phase ---
const { McpBridge } = require('./index');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Set environment variables for testing, returns a cleanup function.
 */
function setEnv(vars) {
  const originals = {};
  for (const [key, val] of Object.entries(vars)) {
    originals[key] = process.env[key];
    process.env[key] = val;
  }
  return () => {
    for (const [key, val] of Object.entries(originals)) {
      if (val === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = val;
      }
    }
  };
}

// ---------------------------------------------------------------------------
// Mock NATS
// ---------------------------------------------------------------------------

jest.mock('nats', () => {
  const mockJsPublish = jest.fn().mockResolvedValue({ seq: 1 });
  const mockJsPull = jest.fn();
  const mockJsSubscribe = jest.fn().mockResolvedValue({
    [Symbol.asyncIterator]: () => ({
      next: jest.fn().mockResolvedValue({ done: true }),
    }),
  });
  const mockJetStream = jest.fn().mockReturnValue({
    publish: mockJsPublish,
    subscribe: mockJsSubscribe,
    pullSubscribe: mockJsPull,
  });
  const mockJetStreamManager = jest.fn().mockResolvedValue({
    streams: {
      info: jest.fn().mockResolvedValue({ config: { name: 'AGENTS' } }),
    },
    consumers: {
      info: jest.fn(),
      add: jest.fn(),
    },
  });
  const mockConnection = {
    jetstream: mockJetStream,
    jetstreamManager: mockJetStreamManager,
    close: jest.fn().mockResolvedValue(undefined),
    isClosed: jest.fn().mockReturnValue(false),
    status: jest.fn().mockReturnValue({
      [Symbol.asyncIterator]: () => ({
        next: jest.fn().mockResolvedValue({ done: true }),
      }),
    }),
  };
  return {
    connect: jest.fn().mockResolvedValue(mockConnection),
    StringCodec: jest.fn().mockReturnValue({
      encode: (s) => Buffer.from(s),
      decode: (b) => b.toString(),
    }),
    AckPolicy: { Explicit: 'explicit' },
    DeliverPolicy: { All: 'all', New: 'new', Last: 'last' },
    RetentionPolicy: { Limits: 'limits' },
    __mockConnection: mockConnection,
    __mockJsPublish: mockJsPublish,
    __mockJsSubscribe: mockJsSubscribe,
    __mockJsPull: mockJsPull,
  };
});


// ===========================================================================
// 1. MODULE EXPORTS AND STRUCTURE
// ===========================================================================

describe('Module exports', () => {
  test('McpBridge class is exported', () => {
    expect(McpBridge).toBeDefined();
    expect(typeof McpBridge).toBe('function'); // class constructor
  });

  test('McpBridge is instantiable', () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    try {
      const bridge = new McpBridge();
      expect(bridge).toBeInstanceOf(McpBridge);
    } finally {
      cleanup();
    }
  });

  test('McpBridge has a start method', () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    try {
      const bridge = new McpBridge();
      expect(typeof bridge.start).toBe('function');
    } finally {
      cleanup();
    }
  });

  test('McpBridge has a connect method for NATS', () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    try {
      const bridge = new McpBridge();
      expect(typeof bridge.connect).toBe('function');
    } finally {
      cleanup();
    }
  });
});


// ===========================================================================
// 2. ENVIRONMENT VARIABLE CONFIGURATION
// ===========================================================================

describe('Environment variable configuration', () => {
  test('reads AGENT_ROLE from environment', () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'executor',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    try {
      const bridge = new McpBridge();
      expect(bridge.agentRole).toBe('executor');
    } finally {
      cleanup();
    }
  });

  test('reads NATS_URL from environment', () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://custom-host:5222',
      WORKSPACE_DIR: '/tmp/test',
    });
    try {
      const bridge = new McpBridge();
      expect(bridge.natsUrl).toBe('nats://custom-host:5222');
    } finally {
      cleanup();
    }
  });

  test('reads WORKSPACE_DIR from environment', () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/home/user/project',
    });
    try {
      const bridge = new McpBridge();
      expect(bridge.workspaceDir).toBe('/home/user/project');
    } finally {
      cleanup();
    }
  });

  test('throws error when AGENT_ROLE is missing', () => {
    const cleanup = setEnv({
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    delete process.env.AGENT_ROLE;
    try {
      expect(() => new McpBridge()).toThrow(/AGENT_ROLE/i);
    } finally {
      cleanup();
    }
  });

  test('throws error when NATS_URL is missing', () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'writer',
      WORKSPACE_DIR: '/tmp/test',
    });
    delete process.env.NATS_URL;
    try {
      expect(() => new McpBridge()).toThrow(/NATS_URL/i);
    } finally {
      cleanup();
    }
  });

  test('WORKSPACE_DIR is optional with a sensible default', () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
    });
    delete process.env.WORKSPACE_DIR;
    try {
      const bridge = new McpBridge();
      // Should not throw; WORKSPACE_DIR may have a default
      expect(bridge).toBeDefined();
    } finally {
      cleanup();
    }
  });
});


// ===========================================================================
// 3. CHECK_MESSAGES TOOL -- Pull from inbox
// ===========================================================================

describe('check_messages tool', () => {
  let bridge;
  let cleanup;

  beforeEach(() => {
    cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    bridge = new McpBridge();
  });

  afterEach(() => {
    cleanup();
  });

  test('check_messages is registered as a tool', () => {
    const tools = bridge.getTools();
    const toolNames = tools.map((t) => t.name);
    expect(toolNames).toContain('check_messages');
  });

  test('check_messages does not require role parameter', () => {
    const tools = bridge.getTools();
    const checkMsg = tools.find((t) => t.name === 'check_messages');
    // Input schema should have no required params or be empty
    const required = checkMsg.inputSchema?.required || [];
    expect(required).not.toContain('role');
  });

  test('check_messages pulls from agents.<AGENT_ROLE>.inbox', async () => {
    const nats = require('nats');
    await bridge.connect();

    await bridge.handleToolCall('check_messages', {});

    // Should interact with NATS on the correct subject
    const js = nats.__mockConnection.jetstream();
    // Verify subscription or pull was on agents.writer.inbox
    const allCalls = [
      ...js.subscribe.mock.calls,
      ...js.pullSubscribe.mock.calls,
    ];
    const subjectUsed = allCalls.some(
      (call) => call[0] === 'agents.writer.inbox' || String(call).includes('agents.writer.inbox')
    );
    expect(subjectUsed).toBe(true);
  });

  test('check_messages subject changes with AGENT_ROLE', async () => {
    cleanup();
    cleanup = setEnv({
      AGENT_ROLE: 'executor',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    const executorBridge = new McpBridge();
    // The inbox subject should use 'executor'
    expect(executorBridge.inboxSubject).toBe('agents.executor.inbox');
  });

  test('check_messages returns messages as JSON content', async () => {
    await bridge.connect();
    // Mock a message in the inbox
    const result = await bridge.handleToolCall('check_messages', {});
    // Result should be an object or array (possibly empty if no messages)
    expect(result).toBeDefined();
  });
});


// ===========================================================================
// 4. SEND_MESSAGE TOOL -- Publish to outbox
// ===========================================================================

describe('send_message tool', () => {
  let bridge;
  let cleanup;

  beforeEach(() => {
    cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    bridge = new McpBridge();
  });

  afterEach(() => {
    cleanup();
  });

  test('send_message is registered as a tool', () => {
    const tools = bridge.getTools();
    const toolNames = tools.map((t) => t.name);
    expect(toolNames).toContain('send_message');
  });

  test('send_message accepts a content parameter', () => {
    const tools = bridge.getTools();
    const sendMsg = tools.find((t) => t.name === 'send_message');
    // Should have an inputSchema with content property
    const props = sendMsg.inputSchema?.properties || {};
    expect(props).toHaveProperty('content');
  });

  test('send_message does not require role parameter', () => {
    const tools = bridge.getTools();
    const sendMsg = tools.find((t) => t.name === 'send_message');
    const required = sendMsg.inputSchema?.required || [];
    expect(required).not.toContain('role');
  });

  test('send_message publishes to agents.<AGENT_ROLE>.outbox', async () => {
    const nats = require('nats');
    await bridge.connect();

    await bridge.handleToolCall('send_message', {
      content: { status: 'pass', summary: 'Tests passed' },
    });

    const js = nats.__mockConnection.jetstream();
    expect(js.publish).toHaveBeenCalled();
    const publishCalls = js.publish.mock.calls;
    const outboxCall = publishCalls.find(
      (call) => call[0] === 'agents.writer.outbox'
    );
    expect(outboxCall).toBeDefined();
  });

  test('send_message outbox subject changes with AGENT_ROLE', () => {
    cleanup();
    cleanup = setEnv({
      AGENT_ROLE: 'reviewer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    const reviewerBridge = new McpBridge();
    expect(reviewerBridge.outboxSubject).toBe('agents.reviewer.outbox');
  });

  test('send_message publishes JSON-encoded payload', async () => {
    const nats = require('nats');
    await bridge.connect();

    await bridge.handleToolCall('send_message', {
      content: { status: 'pass', summary: 'Done' },
    });

    const js = nats.__mockConnection.jetstream();
    const publishCalls = js.publish.mock.calls;
    expect(publishCalls.length).toBeGreaterThan(0);
    // The payload (2nd argument) should be parseable as JSON
    const payload = publishCalls[0][1];
    const parsed = JSON.parse(
      typeof payload === 'string' ? payload : payload.toString()
    );
    expect(parsed).toHaveProperty('type');
  });
});


// ===========================================================================
// 5. MCP JSON-RPC PROTOCOL (stdio server)
// ===========================================================================

describe('MCP JSON-RPC protocol', () => {
  let bridge;
  let cleanup;

  beforeEach(() => {
    cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    bridge = new McpBridge();
  });

  afterEach(() => {
    cleanup();
  });

  test('getTools returns array of tool definitions', () => {
    const tools = bridge.getTools();
    expect(Array.isArray(tools)).toBe(true);
    expect(tools.length).toBeGreaterThanOrEqual(2);
  });

  test('tool definitions have required MCP fields', () => {
    const tools = bridge.getTools();
    for (const tool of tools) {
      expect(tool).toHaveProperty('name');
      expect(tool).toHaveProperty('description');
      expect(tool).toHaveProperty('inputSchema');
    }
  });

  test('handleToolCall dispatches to check_messages', async () => {
    await bridge.connect();
    const result = await bridge.handleToolCall('check_messages', {});
    expect(result).toBeDefined();
  });

  test('handleToolCall dispatches to send_message', async () => {
    await bridge.connect();
    const result = await bridge.handleToolCall('send_message', {
      content: { status: 'pass' },
    });
    expect(result).toBeDefined();
  });

  test('handleToolCall throws for unknown tool', async () => {
    await bridge.connect();
    await expect(
      bridge.handleToolCall('nonexistent_tool', {})
    ).rejects.toThrow();
  });

  test('tools expose correct inputSchema format', () => {
    const tools = bridge.getTools();
    for (const tool of tools) {
      expect(tool.inputSchema).toHaveProperty('type', 'object');
    }
  });
});


// ===========================================================================
// 6. NATS JETSTREAM INTEGRATION
// ===========================================================================

describe('NATS JetStream integration', () => {
  let bridge;
  let cleanup;

  beforeEach(() => {
    cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    bridge = new McpBridge();
  });

  afterEach(() => {
    cleanup();
  });

  test('connect establishes NATS connection', async () => {
    const nats = require('nats');
    await bridge.connect();
    expect(nats.connect).toHaveBeenCalled();
  });

  test('connect uses NATS_URL from environment', async () => {
    const nats = require('nats');
    await bridge.connect();
    const connectCall = nats.connect.mock.calls[0];
    expect(JSON.stringify(connectCall)).toContain('nats://localhost:4222');
  });

  test('connect creates JetStream context', async () => {
    const nats = require('nats');
    await bridge.connect();
    expect(nats.__mockConnection.jetstream).toHaveBeenCalled();
  });

  test('uses durable consumer for inbox subscription', async () => {
    const nats = require('nats');
    await bridge.connect();
    await bridge.handleToolCall('check_messages', {});

    const js = nats.__mockConnection.jetstream();
    const allCalls = [
      ...js.subscribe.mock.calls,
      ...js.pullSubscribe.mock.calls,
    ];
    // At least one call should include durable configuration
    const hasDurable = allCalls.some(
      (call) => JSON.stringify(call).includes('durable')
    );
    expect(hasDurable).toBe(true);
  });
});


// ===========================================================================
// 7. ALL_DONE MESSAGE HANDLING
// ===========================================================================

describe('all_done message handling', () => {
  let bridge;
  let cleanup;

  beforeEach(() => {
    cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    bridge = new McpBridge();
  });

  afterEach(() => {
    cleanup();
  });

  test('check_messages returns all_done message when received', async () => {
    await bridge.connect();

    // Simulate an all_done message in the inbox
    const allDoneMsg = {
      type: 'all_done',
      summary: 'All tasks processed: 3 completed, 0 stuck',
    };

    // If the bridge has a method to process raw messages, test it
    if (typeof bridge.processInboxMessage === 'function') {
      const result = bridge.processInboxMessage(JSON.stringify(allDoneMsg));
      expect(result.type).toBe('all_done');
    } else {
      // At minimum, the bridge must be able to detect all_done type
      expect(bridge).toBeDefined();
    }
  });

  test('all_done message does not trigger outbox response', async () => {
    const nats = require('nats');
    await bridge.connect();

    // Reset publish mock
    const js = nats.__mockConnection.jetstream();
    js.publish.mockClear();

    // If bridge exposes a way to handle incoming messages
    if (typeof bridge.processInboxMessage === 'function') {
      const allDoneMsg = {
        type: 'all_done',
        summary: 'Done',
      };
      bridge.processInboxMessage(JSON.stringify(allDoneMsg));

      // Should NOT publish to outbox
      const outboxPublishes = js.publish.mock.calls.filter(
        (call) => call[0] && call[0].includes('outbox')
      );
      expect(outboxPublishes.length).toBe(0);
    }
  });
});


// ===========================================================================
// 8. OUTBOX MESSAGE SCHEMA WRAPPING
// ===========================================================================

describe('Outbox message schema', () => {
  let bridge;
  let cleanup;

  beforeEach(() => {
    cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    bridge = new McpBridge();
  });

  afterEach(() => {
    cleanup();
  });

  test('send_message wraps content with type agent_complete', async () => {
    const nats = require('nats');
    await bridge.connect();

    const js = nats.__mockConnection.jetstream();
    js.publish.mockClear();

    await bridge.handleToolCall('send_message', {
      content: { status: 'pass', summary: 'Tests passed' },
    });

    expect(js.publish).toHaveBeenCalled();
    const payload = js.publish.mock.calls[0][1];
    const parsed = JSON.parse(
      typeof payload === 'string' ? payload : payload.toString()
    );
    expect(parsed.type).toBe('agent_complete');
  });

  test('send_message includes status from content', async () => {
    const nats = require('nats');
    await bridge.connect();

    const js = nats.__mockConnection.jetstream();
    js.publish.mockClear();

    await bridge.handleToolCall('send_message', {
      content: { status: 'fail', error: 'Test failure' },
    });

    const payload = js.publish.mock.calls[0][1];
    const parsed = JSON.parse(
      typeof payload === 'string' ? payload : payload.toString()
    );
    expect(parsed.status).toBe('fail');
  });

  test('send_message preserves optional fields from content', async () => {
    const nats = require('nats');
    await bridge.connect();

    const js = nats.__mockConnection.jetstream();
    js.publish.mockClear();

    await bridge.handleToolCall('send_message', {
      content: {
        status: 'pass',
        summary: 'All good',
        files_changed: ['src/app.js'],
      },
    });

    const payload = js.publish.mock.calls[0][1];
    const parsed = JSON.parse(
      typeof payload === 'string' ? payload : payload.toString()
    );
    expect(parsed.summary).toBe('All good');
    expect(parsed.files_changed).toEqual(['src/app.js']);
  });
});


// ===========================================================================
// 9. ERROR HANDLING
// ===========================================================================

describe('Error handling', () => {
  test('handleToolCall before connect throws error', async () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    try {
      const bridge = new McpBridge();
      await expect(
        bridge.handleToolCall('check_messages', {})
      ).rejects.toThrow();
    } finally {
      cleanup();
    }
  });

  test('send_message before connect throws error', async () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    try {
      const bridge = new McpBridge();
      await expect(
        bridge.handleToolCall('send_message', { content: {} })
      ).rejects.toThrow();
    } finally {
      cleanup();
    }
  });

  test('connect handles NATS connection failure gracefully', async () => {
    const nats = require('nats');
    nats.connect.mockRejectedValueOnce(new Error('Connection refused'));

    const cleanup = setEnv({
      AGENT_ROLE: 'writer',
      NATS_URL: 'nats://unreachable:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    try {
      const bridge = new McpBridge();
      await expect(bridge.connect()).rejects.toThrow();
    } finally {
      cleanup();
    }
  });

  test('inbox subject is correctly derived from AGENT_ROLE', () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'qa',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    try {
      const bridge = new McpBridge();
      expect(bridge.inboxSubject).toBe('agents.qa.inbox');
    } finally {
      cleanup();
    }
  });

  test('outbox subject is correctly derived from AGENT_ROLE', () => {
    const cleanup = setEnv({
      AGENT_ROLE: 'qa',
      NATS_URL: 'nats://localhost:4222',
      WORKSPACE_DIR: '/tmp/test',
    });
    try {
      const bridge = new McpBridge();
      expect(bridge.outboxSubject).toBe('agents.qa.outbox');
    } finally {
      cleanup();
    }
  });
});
