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

class McpBridge {
  /**
   * @param {object} [opts] - Optional overrides (mainly for testing).
   */
  constructor(opts = {}) {
    const agentRole = process.env.AGENT_ROLE;
    const natsUrl = process.env.NATS_URL;
    const workspaceDir = process.env.WORKSPACE_DIR || process.cwd();

    if (!agentRole) {
      throw new Error('AGENT_ROLE environment variable is required');
    }
    if (!natsUrl) {
      throw new Error('NATS_URL environment variable is required');
    }

    this.agentRole = agentRole;
    this.natsUrl = natsUrl;
    this.workspaceDir = workspaceDir;

    this.inboxSubject = `agents.${this.agentRole}.inbox`;
    this.outboxSubject = `agents.${this.agentRole}.outbox`;

    this._conn = null;
    this._js = null;
    this._sc = nats.StringCodec();
  }

  /**
   * Returns the list of MCP tool definitions.
   * @returns {Array<object>}
   */
  getTools() {
    return [
      {
        name: 'check_messages',
        description: `Pull messages from the agent inbox (${this.inboxSubject})`,
        inputSchema: {
          type: 'object',
          properties: {},
        },
      },
      {
        name: 'send_message',
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
   */
  async connect() {
    this._conn = await nats.connect({ servers: this.natsUrl });
    this._js = this._conn.jetstream();
  }

  /**
   * Start the MCP stdio server (placeholder for full stdio implementation).
   */
  async start() {
    await this.connect();
  }

  /**
   * Dispatch an MCP tool call by name.
   * @param {string} toolName
   * @param {object} params
   * @returns {Promise<any>}
   */
  async handleToolCall(toolName, params) {
    if (!this._conn) {
      throw new Error('Not connected to NATS. Call connect() first.');
    }

    switch (toolName) {
      case 'check_messages':
        return this._handleCheckMessages(params);
      case 'send_message':
        return this._handleSendMessage(params);
      default:
        throw new Error(`Unknown tool: ${toolName}`);
    }
  }

  /**
   * Pull messages from the inbox subject via JetStream subscribe.
   * @returns {Promise<Array<object>>}
   */
  async _handleCheckMessages(_params) {
    const durableName = `${this.agentRole}-inbox`;
    const sub = await this._js.subscribe(this.inboxSubject, {
      durable: durableName,
    });

    const messages = [];
    // Drain available messages (async iterator)
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
   * Wraps content with type=agent_complete schema.
   * @param {object} params
   * @returns {Promise<object>}
   */
  async _handleSendMessage(params) {
    const content = params.content || {};

    // Wrap with outbox schema: type=agent_complete, spread content fields
    const envelope = {
      type: 'agent_complete',
      ...content,
    };

    const payload = this._sc.encode(JSON.stringify(envelope));
    const ack = await this._js.publish(this.outboxSubject, payload);
    return { published: true, seq: ack.seq };
  }

  /**
   * Process a raw inbox message string. Used for detecting special
   * message types like all_done.
   * @param {string} raw - JSON string
   * @returns {object} parsed message
   */
  processInboxMessage(raw) {
    const msg = JSON.parse(raw);
    // all_done messages are returned but do NOT trigger outbox responses
    return msg;
  }
}

module.exports = { McpBridge };
