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

// Heartbeat (issue #32): publishes a periodic health signal via
// core NATS to `system.heartbeat.<role>`. Lives outside the
// `agents.>` JetStream wildcard so it is NOT stored in the AGENTS
// stream — keeps the stream budget clean and the manager inbox
// free of 1440 heartbeats/day. Missed heartbeats are the signal:
// the orchestrator watchdog watches for staleness and emits a
// `hub_unreachable` manager_directive when a heartbeat falls
// behind `watchdog.heartbeat_staleness_seconds`.
//
// setInterval drift note: Node's `setInterval` can drift to
// ~1.1-1.2× its nominal value under event-loop pressure (long
// tool calls, GC pauses, etc.). The watchdog side assumes the
// threshold is at least 3× HEARTBEAT_INTERVAL_MS so single-tick
// drift doesn't trigger false positives. If the threshold is ever
// tuned below ~3× the interval, expect false `hub_unreachable`
// alerts under load. Default threshold is 180s (3×), which is the
// minimum safe value.
const HEARTBEAT_SUBJECT_PREFIX = 'system.heartbeat';
const HEARTBEAT_INTERVAL_MS = 60_000; // 60s — matches watchdog cadence expectation
const HEARTBEAT_TYPE = 'health_ok';

const TOOL_CHECK_MESSAGES = 'check_messages';
const TOOL_SEND_MESSAGE = 'send_message';
const TOOL_SEND_TO_AGENT = 'send_to_agent';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function buildSubject(role, channel) {
  return `${SUBJECT_PREFIX}.${role}.${channel}`;
}

let messageCounter = 0;

function buildEnvelopeMetadata(priority) {
  return {
    message_id: `${agentRole}-${Date.now()}-${++messageCounter}`,
    timestamp: new Date().toISOString(),
    from: agentRole,
    priority: priority || 'normal', // low, normal, high, urgent
  };
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
// Reconnection settings
// ---------------------------------------------------------------------------

const RECONNECT_MAX_ATTEMPTS = -1; // unlimited
const RECONNECT_INITIAL_DELAY_MS = 500;
const RECONNECT_MAX_DELAY_MS = 30000;
const PING_INTERVAL_MS = 10000; // detect dead connections faster

// ---------------------------------------------------------------------------
// NATS connection (with automatic reconnection)
// ---------------------------------------------------------------------------

async function connectNats() {
  nc = await nats.connect({
    servers: natsUrl,
    maxReconnectAttempts: RECONNECT_MAX_ATTEMPTS,
    reconnect: true,
    reconnectTimeWait: RECONNECT_INITIAL_DELAY_MS,
    reconnectJitter: 500,
    reconnectJitterTLS: 1000,
    reconnectDelayHandler: () => {
      // Exponential backoff capped at max delay
      const attempt = nc ? (nc.stats().reconnects || 0) : 0;
      const delay = Math.min(
        RECONNECT_INITIAL_DELAY_MS * Math.pow(2, attempt),
        RECONNECT_MAX_DELAY_MS,
      );
      return delay;
    },
    pingInterval: PING_INTERVAL_MS,
    maxPingOut: 3,
  });

  js = nc.jetstream();
  process.stderr.write(`MCP bridge connected to NATS as "${agentRole}"\n`);

  // Monitor connection status changes
  (async () => {
    for await (const status of nc.status()) {
      switch (status.type) {
        case 'disconnect':
          process.stderr.write(
            `[${new Date().toISOString()}] NATS disconnected: ${status.data || 'unknown reason'}\n`,
          );
          break;
        case 'reconnect':
          js = nc.jetstream(); // refresh JetStream context after reconnect
          process.stderr.write(
            `[${new Date().toISOString()}] NATS reconnected to ${status.data || natsUrl}\n`,
          );
          break;
        case 'reconnecting':
          process.stderr.write(
            `[${new Date().toISOString()}] NATS reconnecting...\n`,
          );
          break;
        case 'error':
          process.stderr.write(
            `[${new Date().toISOString()}] NATS error: ${status.data}\n`,
          );
          break;
      }
    }
  })();
}

// ---------------------------------------------------------------------------
// Background inbox subscription -- push notification channel
// ---------------------------------------------------------------------------

// Messages received via background subscription, waiting for check_messages
const inboxBuffer = [];
const MAX_INBOX_BUFFER = 500;

async function startInboxSubscription() {
  try {
    const sub = nc.subscribe(inboxSubject);
    process.stderr.write(
      `[${new Date().toISOString()}] Background subscription active on ${inboxSubject}\n`,
    );

    for await (const msg of sub) {
      try {
        const data = sc.decode(msg.data);
        const parsed = JSON.parse(data);
        if (inboxBuffer.length >= MAX_INBOX_BUFFER) {
          process.stderr.write(
            `[${new Date().toISOString()}] Inbox buffer full (${MAX_INBOX_BUFFER}), dropping oldest\n`,
          );
          inboxBuffer.shift();
        }
        inboxBuffer.push(parsed);

        const sender = parsed.from || 'unknown';
        const msgType = parsed.type || 'unknown';
        process.stderr.write(
          `[${new Date().toISOString()}] Inbox: ${msgType} from ${sender} (buffered: ${inboxBuffer.length})\n`,
        );

        // Notify Claude Code via MCP logging -- second delivery channel
        try {
          await server.sendLoggingMessage({
            level: 'info',
            data: `New message from ${sender}: call check_messages to read it.`,
          });
        } catch {
          // Server may not be connected yet -- best effort
        }
      } catch {
        // Parse error -- skip
      }
    }
  } catch (err) {
    process.stderr.write(
      `[${new Date().toISOString()}] Background subscription error: ${err.message}\n`,
    );
  }
}

// ---------------------------------------------------------------------------
// Retry helper -- retries on CONNECTION_CLOSED / transient NATS errors
// ---------------------------------------------------------------------------

async function withRetry(fn, label, maxRetries = 3) {
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (err) {
      const isTransient =
        /CONNECTION_CLOSED|DISCONNECT|TIMEOUT|reconnect/i.test(err.message);
      if (isTransient && attempt < maxRetries) {
        const delay = Math.min(1000 * Math.pow(2, attempt), 5000);
        process.stderr.write(
          `[${new Date().toISOString()}] ${label} failed (attempt ${attempt + 1}/${maxRetries + 1}): ${err.message} -- retrying in ${delay}ms\n`,
        );
        await new Promise((r) => setTimeout(r, delay));
        // Refresh JetStream context in case we reconnected
        if (nc) js = nc.jetstream();
        continue;
      }
      throw err;
    }
  }
}

// ---------------------------------------------------------------------------
// Tool: check_messages
// ---------------------------------------------------------------------------

async function handleCheckMessages() {
  const durableName = `${agentRole}-${CHANNEL_INBOX}-mcp`;
  const messages = [];

  // Drain buffer first (messages received via background subscription)
  while (inboxBuffer.length > 0) {
    messages.push(inboxBuffer.shift());
  }

  // Also fetch from JetStream (catches anything the background sub missed)
  try {
    const jsm = await nc.jetstreamManager();

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

    try {
      const batch = await consumer.fetch({ max_messages: 20, expires: 2000 });
      for await (const msg of batch) {
        const data = sc.decode(msg.data);
        try {
          const parsed = JSON.parse(data);
          // Deduplicate: skip if message_id already in buffer-sourced messages
          const isDup = parsed.message_id &&
            messages.some((m) => m.message_id === parsed.message_id);
          if (!isDup) {
            messages.push(parsed);
          }
        } catch {
          messages.push({ raw: data });
        }
        msg.ack();
      }
    } catch {
      // fetch can throw if no messages available -- that's fine
    }
  } catch (err) {
    // JetStream fetch failed but we may still have buffered messages
    if (messages.length === 0) {
      return {
        content: [{ type: 'text', text: `Error checking messages: ${err.message}` }],
        isError: true,
      };
    }
  }

  // Always publish delivery ACK — tells the orchestrator "I checked my
  // inbox".  Even if empty, this clears the pending flag so the protocol
  // stops nudging.
  {
    try {
      const ackPayload = {
        type: 'delivery_ack',
        agent: agentRole,
        count: messages.length,
        timestamp: new Date().toISOString(),
      };
      const ackSubject = buildSubject(agentRole, 'ack');
      nc.publish(ackSubject, sc.encode(JSON.stringify(ackPayload)));
    } catch (ackErr) {
      process.stderr.write(
        `[${new Date().toISOString()}] delivery ACK publish failed: ${ackErr.message}\n`,
      );
    }
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
}

// ---------------------------------------------------------------------------
// Tool: send_message
// ---------------------------------------------------------------------------

async function handleSendMessage(params) {
  const content = params.content || {};

  const envelope = {
    type: OUTBOX_MESSAGE_TYPE,
    ...buildEnvelopeMetadata(content.priority),
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
  const priority = params.priority || 'normal';

  if (!targetAgent) {
    return {
      content: [{ type: 'text', text: 'Error: target_agent is required' }],
      isError: true,
    };
  }

  const targetInbox = buildSubject(targetAgent, CHANNEL_INBOX);
  const envelope = {
    type: 'agent_message',
    ...buildEnvelopeMetadata(priority),
    message,
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
  { capabilities: { tools: {}, logging: {} } },
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
      description: 'Send a direct message to another agent by name. The message lands in their inbox immediately. Messages include timestamp, message_id, and priority.',
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
          priority: {
            type: 'string',
            enum: ['low', 'normal', 'high', 'urgent'],
            description: 'Message priority. Default: normal. Use "urgent" for time-sensitive tasks, "high" for important but not immediate.',
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
      return withRetry(() => handleCheckMessages(), 'check_messages');
    case TOOL_SEND_MESSAGE:
      return withRetry(() => handleSendMessage(params || {}), 'send_message');
    case TOOL_SEND_TO_AGENT:
      return withRetry(() => handleSendToAgent(params || {}), 'send_to_agent');
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

// ---------------------------------------------------------------------------
// Heartbeat publisher (issue #32)
// ---------------------------------------------------------------------------

let heartbeatTimer = null;
const bridgeStartMs = Date.now();

async function publishHeartbeat() {
  if (nc === null) {
    return;
  }
  try {
    const payload = {
      type: HEARTBEAT_TYPE,
      agent: agentRole,
      ts: new Date().toISOString(),
      bridge_pid: process.pid,
      uptime_seconds: Math.floor((Date.now() - bridgeStartMs) / 1000),
    };
    const subject = `${HEARTBEAT_SUBJECT_PREFIX}.${agentRole}`;
    nc.publish(subject, sc.encode(JSON.stringify(payload)));
  } catch (err) {
    process.stderr.write(
      `[${new Date().toISOString()}] heartbeat publish failed: ${err.message}\n`,
    );
  }
}

function startHeartbeatPublisher() {
  // Fire one heartbeat immediately so the watchdog doesn't have to wait
  // a full interval to see this agent as healthy on boot.
  publishHeartbeat();
  heartbeatTimer = setInterval(publishHeartbeat, HEARTBEAT_INTERVAL_MS);
  process.stderr.write(
    `[${new Date().toISOString()}] Heartbeat publisher active on ${HEARTBEAT_SUBJECT_PREFIX}.${agentRole} (interval=${HEARTBEAT_INTERVAL_MS}ms)\n`,
  );
}

function stopHeartbeatPublisher() {
  if (heartbeatTimer !== null) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

async function main() {
  await connectNats();
  const transport = new StdioServerTransport();
  await server.connect(transport);

  // Start background inbox subscription (push notification channel)
  startInboxSubscription();

  // Start heartbeat publisher (issue #32) — fire-and-forget signal
  // that the bridge is alive and its NATS connection is healthy.
  startHeartbeatPublisher();

  process.stderr.write(`MCP bridge ready for "${agentRole}"\n`);
}

main().catch((err) => {
  process.stderr.write(`MCP bridge fatal error: ${err.message}\n`);
  process.exit(1);
});
