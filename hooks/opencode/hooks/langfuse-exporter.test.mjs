import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';

const mod = await import('./langfuse-exporter.mjs');
const { buildOtlpJson, extractText, extractTools, truncate } = mod;

function _getSpans(result) {
  return result.resourceSpans[0].scopeSpans[0].spans;
}

function _getAttr(span, key) {
  for (const attr of (span.attributes || [])) {
    if (attr.key === key) return attr.value;
  }
  return null;
}

function _makeUserMsg(text = 'hello', agent = 'cli', time = '2024-01-01T00:00:00Z') {
  return {
    info: { role: 'user', agent, time: { created: time } },
    parts: [{ type: 'text', text }],
  };
}

function _makeAssistantEntry(modelID, providerID, tokens, cost, finish, mode, time, parts) {
  return {
    info: {
      role: 'assistant',
      modelID,
      providerID,
      tokens,
      cost,
      finish,
      mode,
      time,
    },
    parts: parts || [],
  };
}

function _makeTextPart(text) {
  return { type: 'text', text };
}

function _makeToolPart(tool, callID, state) {
  return { type: 'tool', tool, callID, state: state || {} };
}

describe('buildOtlpJson', () => {
  describe('R-1: N assistant messages produce N generation spans', () => {
    it('should produce 2 generation spans for 2 assistant messages with independent metadata', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', { input: 100, output: 50 }, 0.001, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeTextPart('first response'),
          _makeToolPart('read', 'call_1', { status: 'completed', input: { path: '/a' }, output: 'content_a', time: { start: '2024-01-01T00:00:01Z', end: '2024-01-01T00:00:02Z' } }),
        ]),
        _makeAssistantEntry('gpt-3.5', 'openai', { input: 200, output: 100 }, 0.002, 'stop', 'chat', { created: '2024-01-01T00:00:03Z', completed: '2024-01-01T00:00:04Z' }, [
          _makeTextPart('second response'),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);

      const genSpans = spans.filter(s => {
        const type = _getAttr(s, 'langfuse.observation.type');
        return type && type.stringValue === 'generation';
      });
      assert.strictEqual(genSpans.length, 2, 'generation span count should be 2');

      const model0 = _getAttr(genSpans[0], 'langfuse.observation.model.name');
      assert.strictEqual(model0.stringValue, 'openai/gpt-4', 'first generation model should be openai/gpt-4');

      const model1 = _getAttr(genSpans[1], 'langfuse.observation.model.name');
      assert.strictEqual(model1.stringValue, 'openai/gpt-3.5', 'second generation model should be openai/gpt-3.5');

      const usage0 = JSON.parse(_getAttr(genSpans[0], 'langfuse.observation.usage_details').stringValue);
      assert.strictEqual(usage0.input, 100, 'first generation usage input should be 100');
      assert.strictEqual(usage0.output, 50, 'first generation usage output should be 50');

      const usage1 = JSON.parse(_getAttr(genSpans[1], 'langfuse.observation.usage_details').stringValue);
      assert.strictEqual(usage1.input, 200, 'second generation usage input should be 200');
      assert.strictEqual(usage1.output, 100, 'second generation usage output should be 100');

      const meta0 = JSON.parse(_getAttr(genSpans[0], 'langfuse.observation.metadata').stringValue);
      assert.strictEqual(meta0.toolCount, 1, 'first generation toolCount should be 1');
      assert.strictEqual(meta0.cost, 0.001, 'first generation cost should be 0.001');

      const meta1 = JSON.parse(_getAttr(genSpans[1], 'langfuse.observation.metadata').stringValue);
      assert.strictEqual(meta1.toolCount, 0, 'second generation toolCount should be 0');
      assert.strictEqual(meta1.cost, 0.002, 'second generation cost should be 0.002');

      const output0 = JSON.parse(_getAttr(genSpans[0], 'langfuse.observation.output').stringValue);
      assert.strictEqual(output0.role, 'assistant', 'first generation output role should be assistant');
      assert.strictEqual(output0.content, 'first response', 'first generation output content should be first response');
      assert.ok(output0.tool_calls, 'first generation output should have tool_calls');

      const output1 = JSON.parse(_getAttr(genSpans[1], 'langfuse.observation.output').stringValue);
      assert.strictEqual(output1.role, 'assistant', 'second generation output role should be assistant');
      assert.strictEqual(output1.content, 'second response', 'second generation output content should be second response');
      assert.strictEqual(output1.tool_calls, undefined, 'second generation output should not have tool_calls');
    });

    it('should produce 1 generation span for 1 assistant message', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', { input: 50, output: 25 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeTextPart('single response'),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);

      const genSpans = spans.filter(s => {
        const type = _getAttr(s, 'langfuse.observation.type');
        return type && type.stringValue === 'generation';
      });
      assert.strictEqual(genSpans.length, 1, 'single generation span count should be 1');
    });

    it('should use session-level model fallback when per-message model is missing', () => {
      const entries = [
        _makeAssistantEntry(undefined, undefined, { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeTextPart('response'),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map([['sess', { modelID: 'claude-3', providerID: 'anthropic' }]]);

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);
      const genSpans = spans.filter(s => {
        const type = _getAttr(s, 'langfuse.observation.type');
        return type && type.stringValue === 'generation';
      });

      const model = _getAttr(genSpans[0], 'langfuse.observation.model.name');
      assert.strictEqual(model.stringValue, 'anthropic/claude-3', 'model should fall back to session-level anthropic/claude-3');
    });
  });

  describe('R-2: tool span parentSpanId belongs to correct generation', () => {
    it('should assign tool spans to their owning generation span', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeTextPart('using tool'),
          _makeToolPart('Bash', 'call_1', { status: 'completed', input: { cmd: 'ls' }, output: 'file1.txt', time: { start: '2024-01-01T00:00:01Z', end: '2024-01-01T00:00:02Z' } }),
        ]),
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:03Z', completed: '2024-01-01T00:00:04Z' }, [
          _makeTextPart('second msg'),
          _makeToolPart('Read', 'call_2', { status: 'completed', input: { path: '/b' }, output: 'content_b', time: { start: '2024-01-01T00:00:03Z', end: '2024-01-01T00:00:04Z' } }),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);

      const genSpans = spans.filter(s => {
        const type = _getAttr(s, 'langfuse.observation.type');
        return type && type.stringValue === 'generation';
      });
      const toolSpans = spans.filter(s => {
        const type = _getAttr(s, 'langfuse.observation.type');
        return type && type.stringValue === 'tool';
      });

      assert.strictEqual(genSpans.length, 2, 'generation span count should be 2');
      assert.strictEqual(toolSpans.length, 2, 'tool span count should be 2');

      assert.strictEqual(toolSpans[0].parentSpanId, genSpans[0].spanId, 'first tool span parent should be first generation span');
      assert.strictEqual(toolSpans[1].parentSpanId, genSpans[1].spanId, 'second tool span parent should be second generation span');
      assert.notStrictEqual(toolSpans[0].parentSpanId, genSpans[1].spanId, 'first tool span must not be parented to second generation');
      assert.notStrictEqual(toolSpans[1].parentSpanId, genSpans[0].spanId, 'second tool span must not be parented to first generation');
    });
  });

  describe('R-3: non-first generation input reflects previous tool results', () => {
    it('should set second generation input to previous tool results array', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeTextPart('using tool'),
          _makeToolPart('Bash', 'call_1', { status: 'completed', input: { cmd: 'ls' }, output: 'file1.txt', time: { start: '2024-01-01T00:00:01Z', end: '2024-01-01T00:00:02Z' } }),
        ]),
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:03Z', completed: '2024-01-01T00:00:04Z' }, [
          _makeTextPart('second msg'),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);

      const genSpans = spans.filter(s => {
        const type = _getAttr(s, 'langfuse.observation.type');
        return type && type.stringValue === 'generation';
      });

      const input0 = JSON.parse(_getAttr(genSpans[0], 'langfuse.observation.input').stringValue);
      assert.strictEqual(input0.role, 'user', 'first generation input role should be user');
      assert.strictEqual(input0.content, 'hello', 'first generation input content should be hello');

      const input1 = JSON.parse(_getAttr(genSpans[1], 'langfuse.observation.input').stringValue);
      assert.ok(Array.isArray(input1), 'second generation input should be an array');
      assert.strictEqual(input1.length, 1, 'second generation input should have 1 tool result');
      assert.strictEqual(input1[0].name, 'Bash', 'tool result name should be Bash');
      assert.strictEqual(input1[0].output, 'file1.txt', 'tool result output should be file1.txt');
    });

    it('should omit input attribute on second generation when previous has no tool parts', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeTextPart('just text'),
        ]),
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:03Z', completed: '2024-01-01T00:00:04Z' }, [
          _makeTextPart('second msg'),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);

      const genSpans = spans.filter(s => {
        const type = _getAttr(s, 'langfuse.observation.type');
        return type && type.stringValue === 'generation';
      });

      const input1 = _getAttr(genSpans[1], 'langfuse.observation.input');
      assert.strictEqual(input1, null, 'second generation input should be omitted when previous has no tool parts');
    });

    it('should include error in tool result when present', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeToolPart('Bash', 'call_1', { status: 'error', input: { cmd: 'bad' }, error: 'command failed', time: { start: '2024-01-01T00:00:01Z', end: '2024-01-01T00:00:02Z' } }),
        ]),
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:03Z', completed: '2024-01-01T00:00:04Z' }, [
          _makeTextPart('retry'),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);

      const genSpans = spans.filter(s => {
        const type = _getAttr(s, 'langfuse.observation.type');
        return type && type.stringValue === 'generation';
      });

      const input1 = JSON.parse(_getAttr(genSpans[1], 'langfuse.observation.input').stringValue);
      assert.strictEqual(input1[0].name, 'Bash', 'tool result name should be Bash');
      assert.strictEqual(input1[0].error, 'command failed', 'tool result should have error');
      assert.strictEqual(input1[0].output, undefined, 'tool result should not have output when error');
    });

    it('should omit output field when tool state.output is undefined', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeToolPart('Bash', 'call_1', { status: 'completed', input: { cmd: 'ls' }, time: { start: '2024-01-01T00:00:01Z', end: '2024-01-01T00:00:02Z' } }),
        ]),
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:03Z', completed: '2024-01-01T00:00:04Z' }, [
          _makeTextPart('followup'),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);

      const genSpans = spans.filter(s => {
        const type = _getAttr(s, 'langfuse.observation.type');
        return type && type.stringValue === 'generation';
      });

      const input1 = JSON.parse(_getAttr(genSpans[1], 'langfuse.observation.input').stringValue);
      assert.strictEqual(input1[0].name, 'Bash', 'tool result name should be Bash');
      assert.strictEqual(input1[0].output, undefined, 'output should be omitted when state.output is undefined');
    });
  });

  describe('R-4: root span output is last assistant text only', () => {
    it('should set root span output to last assistant message text only', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeTextPart('A'),
        ]),
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:03Z', completed: '2024-01-01T00:00:04Z' }, [
          _makeTextPart('B'),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);

      const root = spans.find(s => !s.parentSpanId);
      const output = JSON.parse(_getAttr(root, 'langfuse.trace.output').stringValue);
      assert.strictEqual(output.role, 'assistant', 'root output role should be assistant');
      assert.strictEqual(output.content, 'B', 'root output content should be B only, not A');
    });

    it('should set root span output content to empty string when last assistant has no text', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeTextPart('A'),
        ]),
        _makeAssistantEntry('gpt-4', 'openai', { input: 10, output: 5 }, 0, 'stop', 'chat', { created: '2024-01-01T00:00:03Z', completed: '2024-01-01T00:00:04Z' }, [
          _makeToolPart('Bash', 'call_1', { status: 'completed', input: { cmd: 'ls' }, output: 'files', time: { start: '2024-01-01T00:00:03Z', end: '2024-01-01T00:00:04Z' } }),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);

      const root = spans.find(s => !s.parentSpanId);
      const output = JSON.parse(_getAttr(root, 'langfuse.trace.output').stringValue);
      assert.strictEqual(output.role, 'assistant', 'root output role should be assistant');
      assert.strictEqual(output.content, '', 'root output content should be empty when last assistant has no text');
    });
  });

  describe('structure', () => {
    it('should have OTLP format with correct scope name', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', {}, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeTextPart('hi'),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);

      assert.ok(result.resourceSpans, 'result should have resourceSpans');
      assert.strictEqual(result.resourceSpans.length, 1, 'should have 1 resourceSpan');
      const scopeSpans = result.resourceSpans[0].scopeSpans;
      assert.strictEqual(scopeSpans.length, 1, 'should have 1 scopeSpan');
      assert.strictEqual(scopeSpans[0].scope.name, 'agent-exporter-to-langfuse', 'scope name should be agent-exporter-to-langfuse');
    });

    it('should have valid traceId and spanId', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', {}, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeTextPart('hi'),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);

      for (const span of spans) {
        assert.strictEqual(span.traceId.length, 32, `traceId should be 32 hex chars for span ${span.name}`);
        assert.strictEqual(span.spanId.length, 16, `spanId should be 16 hex chars for span ${span.name}`);
      }
    });

    it('should have root span with session.id and trace tags', () => {
      const entries = [
        _makeAssistantEntry('gpt-4', 'openai', {}, 0, 'stop', 'chat', { created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }, [
          _makeTextPart('hi'),
        ]),
      ];
      const userMsg = _makeUserMsg('hello');
      const sessionModels = new Map();

      const result = buildOtlpJson('lsess', 'sess', 1, userMsg, [{ type: 'text', text: 'hello' }], entries, sessionModels, false);
      const spans = _getSpans(result);

      const root = spans.find(s => !s.parentSpanId);
      assert.ok(root, 'should have a root span');

      const sessionId = _getAttr(root, 'session.id');
      assert.strictEqual(sessionId.stringValue, 'lsess', 'session.id should be lsess');

      const tags = _getAttr(root, 'langfuse.trace.tags');
      assert.ok(tags, 'should have trace tags');
    });
  });
});