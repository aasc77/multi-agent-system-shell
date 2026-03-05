/**
 * MCP Stdio Bridge for Claude Code Agents
 *
 * Provides check_messages and send_message tools backed by NATS JetStream.
 * The agent's identity is baked in via AGENT_ROLE env var -- tools are
 * parameterless regarding identity.
 *
 * Requirements: R2 (MCP bridge per agent), R3 (Communication Flow)
 */

'use strict';

const nats = require('nats');

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** NATS subject prefix for all agent communication. */
const SUBJECT_PREFIX = 'agents';

/** Subject suffixes for inbox / outbox channels. */
const CHANNEL_INBOX = 'inbox';
const CHANNEL_OUTBOX = 'outbox';

/** Envelope type used for outbox messages (PRD R3). */
const OUTBOX_MESSAGE_TYPE = 'agent_complete';

/** MCP tool names -- single source of truth for registration & dispatch. */
const TOOL_CHECK_MESSAGES = 'check_messages';
const TOOL_SEND_MESSAGE = 'send_message';

/** Error messages. */
const ERR_MISSING_AGENT_ROLE = 'AGENT_ROLE environment variable is required';
const ERR_MISSING_NATS_URL = 'NATS_URL environment variable is required';
const ERR_NOT_CONNECTED = 'Not connected to NATS. Call connect() first.';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build a fully-qualified NATS subject.
 * @param {string} role   - Agent role (e.g. "writer").
 * @param {string} channel - Channel suffix ("inbox" or "outbox").
 * @returns {string} e.g. "agents.writer.inbox"
 */
function buildSubject(role, channel) {
  return `${SUBJECT_PREFIX}.${role}.${channel}`;
}

// ---------------------------------------------------------------------------
// McpBridge
// ---------------------------------------------------------------------------

class McpBridge {
  /**
   * Create an MCP bridge instance.
   *
   * Reads configuration from environment variables:
   * - `AGENT_ROLE` (required) -- the agent identity, e.g. "writer"
   * - `NATS_URL`   (required) -- NATS server URL, e.g. "nats://localhost:4222"
   * - `WORKSPACE_DIR` (optional) -- working directory, defaults to `process.cwd()`
   *
   * @param {object} [opts] - Optional overrides (mainly for testing).
   * @throws {Error} If AGENT_ROLE or NATS_URL are missing.
   */
  constructor(opts = {}) {
    const agentRole = process.env.AGENT_ROLE;
    const natsUrl = process.env.NATS_URL;
    const workspaceDir = process.env.WORKSPACE_DIR || process.cwd();

    if (!agentRole) {
      throw new Error(ERR_MISSING_AGENT_ROLE);
    }
    if (!natsUrl) {
      throw new Error(ERR_MISSING_NATS_URL);
    }

    /** @type {string} */
    this.agentRole = agentRole;
    /** @type {string} */
    this.natsUrl = natsUrl;
    /** @type {string} */
    this.workspaceDir = workspaceDir;

    /** @type {string} NATS subject for pulling task assignments. */
    this.inboxSubject = buildSubject(this.agentRole, CHANNEL_INBOX);
    /** @type {string} NATS subject for publishing results. */
    this.outboxSubject = buildSubject(this.agentRole, CHANNEL_OUTBOX);

    /** @private */
    this._conn = null;
    /** @private */
    this._js = null;
    /** @private */
    this._sc = nats.StringCodec();

    /**
     * Maps MCP tool names to their handler methods.
     * Adding a new tool only requires a new entry here and a corresponding
     * handler method -- no switch/case to update.
     * @private
     * @type {Map<string, function>}
     */
    this._toolHandlers = new Map([
      [TOOL_CHECK_MESSAGES, (params) => this._handleCheckMessages(params)],
      [TOOL_SEND_MESSAGE, (params) => this._handleSendMessage(params)],
    ]);
  }

  /**
   * Returns the list of MCP tool definitions.
   * @returns {Array<object>} Tool definitions with name, description, and inputSchema.
   */
  getTools() {
    return [
      {
        name: TOOL_CHECK_MESSAGES,
        description: `Pull messages from the agent inbox (${this.inboxSubject})`,
        inputSchema: {
          type: 'object',
          properties: {},
        },
      },
      {
        name: TOOL_SEND_MESSAGE,
        description: `Publish a message to the agent outbox (${this.outboxSubject})`,
        inputSchema: {
          type: 'object',
          properties: {
            content: {
              type: 'object',
              description: 'Message content to publish',
            },
          },
          required: ['content'],
        },
      },
    ];
  }

  /**
   * Connect to NATS and create a JetStream context.
   * @throws {Error} If NATS connection fails.
   */
  async connect() {
    this._conn = await nats.connect({ servers: this.natsUrl });
    this._js = this._conn.jetstream();
  }

  /**
   * Start the MCP stdio server (placeholder for full stdio implementation).
   * @throws {Error} If NATS connection fails.
   */
  async start() {
    await this.connect();
  }

  /**
   * Dispatch an MCP tool call by name.
   *
   * Uses an internal handler map so new tools can be registered without
   * modifying a switch/case block.
   *
   * @param {string} toolName - Name of the MCP tool to invoke.
   * @param {object} params   - Parameters passed to the tool.
   * @returns {Promise<any>} Tool-specific result.
   * @throws {Error} If not connected or tool name is unrecognized.
   */
  async handleToolCall(toolName, params) {
    if (!this._conn) {
      throw new Error(ERR_NOT_CONNECTED);
    }

    const handler = this._toolHandlers.get(toolName);
    if (!handler) {
      throw new Error(`Unknown tool: ${toolName}`);
    }

    return handler(params);
  }

  /**
   * Pull messages from the inbox subject via JetStream subscribe.
   *
   * Uses a durable consumer so messages persist across agent restarts
   * (PRD R3 -- durable consumers).
   *
   * @returns {Promise<Array<object>>} Parsed inbox messages.
   * @private
   */
  async _handleCheckMessages() {
    const durableName = `${this.agentRole}-${CHANNEL_INBOX}`;
    const sub = await this._js.subscribe(this.inboxSubject, {
      durable: durableName,
    });

    const messages = [];
    for await (const msg of sub) {
      const data = this._sc.decode(msg.data);
      try {
        messages.push(JSON.parse(data));
      } catch {
        messages.push({ raw: data });
      }
    }

    return messages;
  }

  /**
   * Publish a message to the outbox subject via JetStream.
   *
   * Wraps the caller-provided content in the outbox envelope schema
   * (PRD R3 -- type: "agent_complete") before publishing.
   *
   * @param {object} params           - Tool parameters.
   * @param {object} [params.content] - Message content to publish.
   * @returns {Promise<{published: boolean, seq: number}>} Publish acknowledgement.
   * @private
   */
  async _handleSendMessage(params) {
    const content = params.content || {};

    const envelope = {
      type: OUTBOX_MESSAGE_TYPE,
      ...content,
    };

    const payload = this._sc.encode(JSON.stringify(envelope));
    const ack = await this._js.publish(this.outboxSubject, payload);
    return { published: true, seq: ack.seq };
  }

  /**
   * Process a raw inbox message string.
   *
   * Used for detecting special message types like "all_done" (PRD R3).
   * Note: all_done messages are returned but do NOT trigger outbox responses.
   *
   * @param {string} raw - JSON string from the NATS inbox.
   * @returns {object} Parsed message.
   * @throws {SyntaxError} If raw is not valid JSON.
   */
  processInboxMessage(raw) {
    return JSON.parse(raw);
  }
}

module.exports = { McpBridge };
