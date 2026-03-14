/**
 * MCP Stdio Bridge for Claude Code Agents
 *
 * Provides check_messages and send_message tools backed by NATS JetStream.
 * The agent's identity is baked in via AGENT_ROLE env var.
 *
 * Requirements: R2 (MCP bridge per agent), R3 (Communication Flow)
 *
 * Usage:
 *   AGENT_ROLE=writer NATS_URL=nats://localhost:4222 node index.js
 */

'use strict';

const { Server } = require('@modelcontextprotocol/sdk/server/index.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} = require('@modelcontextprotocol/sdk/types.js');
const nats = require('nats');

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SUBJECT_PREFIX = 'agents';
const CHANNEL_INBOX = 'inbox';
const CHANNEL_OUTBOX = 'outbox';
const STREAM_NAME = 'AGENTS';
const OUTBOX_MESSAGE_TYPE = 'agent_complete';

const TOOL_CHECK_MESSAGES = 'check_messages';
const TOOL_SEND_MESSAGE = 'send_message';
const TOOL_SEND_TO_AGENT = 'send_to_agent';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function buildSubject(role, channel) {
  return `${SUBJECT_PREFIX}.${role}.${channel}`;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const agentRole = process.env.AGENT_ROLE;
const natsUrl = process.env.NATS_URL;

if (!agentRole) {
  process.stderr.write('Error: AGENT_ROLE environment variable is required\n');
  process.exit(1);
}
if (!natsUrl) {
  process.stderr.write('Error: NATS_URL environment variable is required\n');
  process.exit(1);
}

const inboxSubject = buildSubject(agentRole, CHANNEL_INBOX);
const outboxSubject = buildSubject(agentRole, CHANNEL_OUTBOX);
const sc = nats.StringCodec();

let nc = null;
let js = null;

// ---------------------------------------------------------------------------
// NATS connection
// ---------------------------------------------------------------------------

async function connectNats() {
  nc = await nats.connect({ servers: natsUrl });
  js = nc.jetstream();
  process.stderr.write(`MCP bridge connected to NATS as "${agentRole}"\n`);
}

// ---------------------------------------------------------------------------
// Tool: check_messages
// ---------------------------------------------------------------------------

async function handleCheckMessages() {
  const durableName = `${agentRole}-${CHANNEL_INBOX}-mcp`;

  try {
    const jsm = await nc.jetstreamManager();

    // Ensure consumer exists
    try {
      await jsm.consumers.info(STREAM_NAME, durableName);
    } catch {
      await jsm.consumers.add(STREAM_NAME, {
        durable_name: durableName,
        filter_subject: inboxSubject,
        ack_policy: nats.AckPolicy.Explicit,
      });
    }

    const consumer = await js.consumers.get(STREAM_NAME, durableName);
    const messages = [];

    try {
      const batch = await consumer.fetch({ max_messages: 20, expires: 2000 });
      for await (const msg of batch) {
        const data = sc.decode(msg.data);
        try {
          messages.push(JSON.parse(data));
        } catch {
          messages.push({ raw: data });
        }
        msg.ack();
      }
    } catch {
      // fetch can throw if no messages available -- that's fine
    }

    if (messages.length === 0) {
      return { content: [{ type: 'text', text: 'No new messages.' }] };
    }

    return {
      content: [{
        type: 'text',
        text: JSON.stringify(messages, null, 2),
      }],
    };
  } catch (err) {
    return {
      content: [{ type: 'text', text: `Error checking messages: ${err.message}` }],
      isError: true,
    };
  }
}

// ---------------------------------------------------------------------------
// Tool: send_message
// ---------------------------------------------------------------------------

async function handleSendMessage(params) {
  const content = params.content || {};

  const envelope = {
    type: OUTBOX_MESSAGE_TYPE,
    ...content,
  };

  try {
    const payload = sc.encode(JSON.stringify(envelope));
    const ack = await js.publish(outboxSubject, payload);
    return {
      content: [{
        type: 'text',
        text: `Message published to ${outboxSubject} (seq: ${ack.seq})`,
      }],
    };
  } catch (err) {
    return {
      content: [{ type: 'text', text: `Error publishing message: ${err.message}` }],
      isError: true,
    };
  }
}

// ---------------------------------------------------------------------------
// Tool: send_to_agent
// ---------------------------------------------------------------------------

async function handleSendToAgent(params) {
  const targetAgent = params.target_agent;
  const message = params.message || '';

  if (!targetAgent) {
    return {
      content: [{ type: 'text', text: 'Error: target_agent is required' }],
      isError: true,
    };
  }

  const targetInbox = buildSubject(targetAgent, CHANNEL_INBOX);
  const envelope = {
    type: 'agent_message',
    from: agentRole,
    message,
    timestamp: new Date().toISOString(),
  };

  try {
    const payload = sc.encode(JSON.stringify(envelope));
    const ack = await js.publish(targetInbox, payload);
    return {
      content: [{
        type: 'text',
        text: `Message sent to ${targetAgent} via ${targetInbox} (seq: ${ack.seq})`,
      }],
    };
  } catch (err) {
    return {
      content: [{ type: 'text', text: `Error sending to ${targetAgent}: ${err.message}` }],
      isError: true,
    };
  }
}

// ---------------------------------------------------------------------------
// MCP Server
// ---------------------------------------------------------------------------

const server = new Server(
  { name: 'mas-bridge', version: '0.1.0' },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: TOOL_CHECK_MESSAGES,
      description: `Pull messages from the agent inbox (${inboxSubject}). Call this when nudged or to check for new task assignments.`,
      inputSchema: {
        type: 'object',
        properties: {},
      },
    },
    {
      name: TOOL_SEND_TO_AGENT,
      description: 'Send a direct message to another agent by name. The message lands in their inbox immediately.',
      inputSchema: {
        type: 'object',
        properties: {
          target_agent: {
            type: 'string',
            description: 'Name of the target agent (e.g. "hub", "dgx", "macmini")',
          },
          message: {
            type: 'string',
            description: 'The message to send',
          },
        },
        required: ['target_agent', 'message'],
      },
    },
    {
      name: TOOL_SEND_MESSAGE,
      description: `Publish a message to the agent outbox (${outboxSubject}). Use this to send task results back to the orchestrator.`,
      inputSchema: {
        type: 'object',
        properties: {
          content: {
            type: 'object',
            description: 'Message content. Must include "status" (pass/fail) and "summary" fields.',
            properties: {
              status: { type: 'string', enum: ['pass', 'fail'] },
              summary: { type: 'string' },
            },
            required: ['status', 'summary'],
          },
        },
        required: ['content'],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: params } = request.params;

  switch (name) {
    case TOOL_CHECK_MESSAGES:
      return handleCheckMessages();
    case TOOL_SEND_MESSAGE:
      return handleSendMessage(params || {});
    case TOOL_SEND_TO_AGENT:
      return handleSendToAgent(params || {});
    default:
      return {
        content: [{ type: 'text', text: `Unknown tool: ${name}` }],
        isError: true,
      };
  }
});

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  await connectNats();
  const transport = new StdioServerTransport();
  await server.connect(transport);
  process.stderr.write(`MCP bridge ready for "${agentRole}"\n`);
}

main().catch((err) => {
  process.stderr.write(`MCP bridge fatal error: ${err.message}\n`);
  process.exit(1);
});
