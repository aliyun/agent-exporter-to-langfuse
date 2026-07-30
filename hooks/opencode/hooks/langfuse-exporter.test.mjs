import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { buildOtlpJson } from './langfuse-exporter.mjs';

function userMsg(text = 'hello', agent = 'opencode', ts = '2024-01-01T00:00:00Z') {
  return { time: { created: ts }, agent };
}

function userParts(text = 'hello') {
  return [{ type: 'text', text }];
}

function assistantMsg({
  text = '',
  modelID = 'gpt-4o',
  providerID = 'openai',
  tokens = null,
  cost = null,
  finish = 'stop',
  mode = 'default',
  created = '2024-01-01T00:00:01Z',
  completed = '2024-01-01T00:00:02Z',
  parts = null,
} = {}) {
  const p = parts !== null ? parts : [];
  if (text) p.push({ type: 'text', text });
  return {
    info: {
      role: 'assistant',
      modelID,
      providerID,
      tokens: tokens || undefined,
      cost: cost || undefined,
      finish,
      mode,
      time: { created, completed },
    },
    parts: p,
  };
}

function toolPart({
  tool = 'shell',
  callID = 'call-1',
  status = 'completed',
  input = { cmd: 'ls' },
  output = 'file.txt',
  error = null,
  start = '2024-01-01T00:00:01.500Z',
  end = '2024-01-01T00:00:01.900Z',
} = {}) {
  const state = { input, time: { start, end } };
  if (status !== undefined) state.status = status;
  if (output !== null) state.output = output;
  if (error !== null) state.error = error;
  return { type: 'tool', tool, callID, state };
}

function getSpans(result) {
  return result.resourceSpans[0].scopeSpans[0].spans;
}

function getAttr(span, key) {
  return span.attributes.find(a => a.key === key);
}

function getStr(span, key) {
  const attr = getAttr(span, key);
  if (!attr) return undefined;
  return attr.value.stringValue;
}

function getJson(span, key) {
  const s = getStr(span, key);
  return s !== undefined ? JSON.parse(s) : undefined;
}

function generationSpans(spans) {
  return spans.filter(s => getStr(s, 'langfuse.observation.type') === 'generation');
}

function toolSpans(spans) {
  return spans.filter(s => getStr(s, 'langfuse.observation.type') === 'tool');
}

function rootSpan(spans) {
  return spans.find(s => !s.parentSpanId);
}

function build(args) {
  return buildOtlpJson({
    langfuseSessionID: 'sess-root',
    sessionID: 'sess-1',
    turnNum: 1,
    userMsg: userMsg(),
    userParts: userParts(),
    sessionModel: null,
    isSubagent: false,
    ...args,
  });
}

describe('R-1: each assistant message yields one generation span', () => {
  test('2 assistant messages → exactly 2 generation spans under root, time-ordered', () => {
    const result = build({
      assistantEntries: [
        assistantMsg({ text: 'A', created: '2024-01-01T00:00:01Z', completed: '2024-01-01T00:00:02Z' }),
        assistantMsg({ text: 'B', created: '2024-01-01T00:00:03Z', completed: '2024-01-01T00:00:04Z' }),
      ],
    });
    const spans = getSpans(result);
    const gens = generationSpans(spans);
    const root = rootSpan(spans);
    assert.equal(gens.length, 2, `expected 2 generation spans, got ${gens.length}`);
    for (const g of gens) {
      assert.equal(g.parentSpanId, root.spanId, 'generation parentSpanId must equal root spanId');
    }
    const sorted = [...gens].sort((a, b) => {
      const diff = BigInt(a.startTimeUnixNano) - BigInt(b.startTimeUnixNano);
      return diff > 0n ? 1 : diff < 0n ? -1 : 0;
    });
    assert.deepEqual(sorted.map(g => g.spanId), gens.map(g => g.spanId),
      'generation spans must be in startTimeUnixNano ascending order matching assistant message order');
  });

  test('single assistant message → 1 generation span from new code path', () => {
    const result = build({
      assistantEntries: [assistantMsg({ text: 'only' })],
    });
    const gens = generationSpans(getSpans(result));
    assert.equal(gens.length, 1, `expected 1 generation span, got ${gens.length}`);
  });

  test('generation output reflects only its own assistant message text and tool_calls', () => {
    const result = build({
      assistantEntries: [
        assistantMsg({ text: 'first', parts: [toolPart({ tool: 'shell', output: 'done' })] }),
        assistantMsg({ text: 'second' }),
      ],
    });
    const gens = generationSpans(getSpans(result));
    const out0 = getJson(gens[0], 'langfuse.observation.output');
    const out1 = getJson(gens[1], 'langfuse.observation.output');
    assert.equal(out0.role, 'assistant');
    assert.equal(out0.content, 'first', 'gen[0] output content must be its own text "first"');
    assert.ok(Array.isArray(out0.tool_calls) && out0.tool_calls.length === 1,
      'gen[0] must carry its own tool_calls');
    assert.equal(out0.tool_calls[0].name, 'shell');
    assert.equal(out1.content, 'second', 'gen[1] output content must be its own text "second"');
    assert.equal(out1.tool_calls, undefined, 'gen[1] must not carry gen[0] tool_calls');
  });

  test('model.name falls back per-message then to session model then unknown', () => {
    const result = build({
      assistantEntries: [
        assistantMsg({ text: 'a', modelID: 'm1', providerID: 'p1' }),
        assistantMsg({ text: 'b', modelID: '', providerID: '' }),
      ],
      sessionModel: { modelID: 'sess-model', providerID: 'sess-prov' },
    });
    const gens = generationSpans(getSpans(result));
    assert.equal(getStr(gens[0], 'langfuse.observation.model.name'), 'p1/m1',
      'gen[0] model.name must be providerID/modelID from its own info');
    assert.equal(getStr(gens[1], 'langfuse.observation.model.name'), 'sess-prov/sess-model',
      'gen[1] with empty own modelID/providerID must fall back to session model');
  });
});

describe('R-2: each generation carries its own per-call metadata', () => {
  test('usage_details per generation hold only own tokens, no cross-message sum', () => {
    const result = build({
      assistantEntries: [
        assistantMsg({ text: 'a', tokens: { input: 10, output: 5 } }),
        assistantMsg({ text: 'b', tokens: { input: 20, output: 8 } }),
      ],
    });
    const gens = generationSpans(getSpans(result));
    const u0 = getJson(gens[0], 'langfuse.observation.usage_details');
    const u1 = getJson(gens[1], 'langfuse.observation.usage_details');
    assert.equal(u0.input, 10, 'gen[0] usage_details.input must be 10');
    assert.equal(u0.output, 5, 'gen[0] usage_details.output must be 5');
    assert.equal(u1.input, 20, 'gen[1] usage_details.input must be 20, not summed 30');
    assert.equal(u1.output, 8, 'gen[1] usage_details.output must be 8, not summed 13');
    assert.equal(u0.input + u1.input, 30, 'sanity: inputs are 10+20');
    assert.ok(!Object.prototype.hasOwnProperty.call(u0, 'input') || u0.input !== 30,
      'gen[0] input must not be the summed value 30');
  });

  test('metadata per generation holds own finish/mode/toolCount/agent and own cost', () => {
    const result = build({
      userMsg: userMsg('hello', 'myagent'),
      assistantEntries: [
        assistantMsg({ text: 'a', finish: 'stop', mode: 'first', cost: 0.01, parts: [toolPart({ tool: 't1' })] }),
        assistantMsg({ text: 'b', finish: 'length', mode: 'second', cost: 0.02 }),
      ],
    });
    const gens = generationSpans(getSpans(result));
    const m0 = getJson(gens[0], 'langfuse.observation.metadata');
    const m1 = getJson(gens[1], 'langfuse.observation.metadata');
    assert.equal(m0.finish, 'stop', 'gen[0] finish must be its own "stop"');
    assert.equal(m0.mode, 'first', 'gen[0] mode must be its own "first"');
    assert.equal(m0.toolCount, 1, 'gen[0] toolCount must be 1 (its own tools)');
    assert.equal(m0.agent, 'myagent');
    assert.equal(m0.cost, 0.01, 'gen[0] cost must be its own 0.01, not accumulated');
    assert.equal(m1.finish, 'length', 'gen[1] finish must be its own "length", not last-of-merged');
    assert.equal(m1.mode, 'second', 'gen[1] mode must be its own "second", not first-of-merged');
    assert.equal(m1.toolCount, 0, 'gen[1] toolCount must be 0 (its own tools)');
    assert.equal(m1.cost, 0.02, 'gen[1] cost must be its own 0.02, not accumulated 0.03');
  });

  test('zero-value token fields are omitted from usage_details', () => {
    const result = build({
      assistantEntries: [
        assistantMsg({ text: 'a', tokens: { input: 0, output: 0 } }),
      ],
    });
    const gens = generationSpans(getSpans(result));
    assert.equal(getStr(gens[0], 'langfuse.observation.usage_details'), undefined,
      'all-zero usage_details must be omitted entirely');
  });
});

describe('R-3: tool spans parent under their own generation', () => {
  test('2 generations each with 1 tool → tool parentSpanIds match their own generation, no cross', () => {
    const result = build({
      assistantEntries: [
        assistantMsg({ text: 'a', parts: [toolPart({ tool: 'shell', callID: 'c1', output: 'o1' })] }),
        assistantMsg({ text: 'b', parts: [toolPart({ tool: 'grep', callID: 'c2', output: 'o2' })] }),
      ],
    });
    const spans = getSpans(result);
    const gens = generationSpans(spans);
    const tools = toolSpans(spans);
    assert.equal(tools.length, 2, `expected 2 tool spans, got ${tools.length}`);
    assert.equal(tools[0].parentSpanId, gens[0].spanId,
      `tool[0] parentSpanId must equal gen[0].spanId; got ${tools[0].parentSpanId} vs ${gens[0].spanId}`);
    assert.equal(tools[1].parentSpanId, gens[1].spanId,
      `tool[1] parentSpanId must equal gen[1].spanId; got ${tools[1].parentSpanId} vs ${gens[1].spanId}`);
    assert.notEqual(tools[0].parentSpanId, gens[1].spanId, 'tool[0] must not parent under gen[1]');
    assert.notEqual(tools[1].parentSpanId, gens[0].spanId, 'tool[1] must not parent under gen[0]');
  });
});

describe('R-4: non-first generation input reflects prior tool results', () => {
  test('second generation input is tool-results array JSON when first had a completed tool', () => {
    const result = build({
      assistantEntries: [
        assistantMsg({ text: 'a', parts: [toolPart({ tool: 'shell', callID: 'c1', output: 'file.txt' })] }),
        assistantMsg({ text: 'b' }),
      ],
    });
    const gens = generationSpans(getSpans(result));
    const firstInput = getJson(gens[0], 'langfuse.observation.input');
    const secondInput = getJson(gens[1], 'langfuse.observation.input');
    assert.equal(firstInput.role, 'user', 'gen[0] input must be user message');
    assert.equal(firstInput.content, 'hello');
    assert.ok(Array.isArray(secondInput), `gen[1] input must be a tool-results array; got ${typeof secondInput}`);
    assert.equal(secondInput[0].name, 'shell', 'gen[1] input[0].name must be "shell"');
    assert.equal(secondInput[0].output, 'file.txt', 'gen[1] input[0].output must be prior tool output');
    assert.ok(!secondInput[0].hasOwnProperty('error'), 'gen[1] input[0] must not have error when none');
  });

  test('second generation input omitted when first message had no qualifying tool parts', () => {
    const result = build({
      assistantEntries: [
        assistantMsg({ text: 'a' }),
        assistantMsg({ text: 'b' }),
      ],
    });
    const gens = generationSpans(getSpans(result));
    assert.equal(getStr(gens[1], 'langfuse.observation.input'), undefined,
      'gen[1] input must be omitted when prior message had no qualifying tools');
  });

  test('tool result omits output field when state.output missing, includes error when present', () => {
    const result = build({
      assistantEntries: [
        assistantMsg({
          text: 'a',
          parts: [toolPart({ tool: 'bad', output: null, error: 'boom', status: 'error' })],
        }),
        assistantMsg({ text: 'b' }),
      ],
    });
    const gens = generationSpans(getSpans(result));
    const secondInput = getJson(gens[1], 'langfuse.observation.input');
    assert.ok(Array.isArray(secondInput), 'gen[1] input must be array');
    assert.equal(secondInput[0].name, 'bad');
    assert.ok(!secondInput[0].hasOwnProperty('output'), 'output field must be omitted when state.output missing');
    assert.equal(secondInput[0].error, 'boom', 'error field must be present when state.error exists');
  });
});

describe('R-5: root trace.output is last assistant message text only', () => {
  test('root output content is "B" only, not concatenated "AB"', () => {
    const result = build({
      assistantEntries: [
        assistantMsg({ text: 'A' }),
        assistantMsg({ text: 'B' }),
      ],
    });
    const root = rootSpan(getSpans(result));
    const out = getJson(root, 'langfuse.trace.output');
    assert.equal(out.role, 'assistant');
    assert.equal(out.content, 'B', `root trace.output content must be "B" only; got ${JSON.stringify(out.content)}`);
    assert.ok(!out.content.includes('A'), 'root output must not contain first message text "A"');
  });

  test('root input is user message text', () => {
    const result = build({
      userParts: userParts('what is up'),
      assistantEntries: [assistantMsg({ text: 'nm' })],
    });
    const root = rootSpan(getSpans(result));
    const inp = getJson(root, 'langfuse.trace.input');
    assert.equal(inp.role, 'user');
    assert.equal(inp.content, 'what is up');
  });

  test('last assistant with no text part → root output content is empty string', () => {
    const result = build({
      assistantEntries: [
        assistantMsg({ text: 'A' }),
        assistantMsg({ text: '' }),
      ],
    });
    const root = rootSpan(getSpans(result));
    const out = getJson(root, 'langfuse.trace.output');
    assert.equal(out.content, '', `root output content must be empty string; got ${JSON.stringify(out.content)}`);
  });
});

describe('R-6: buildOtlpJson is a side-effect-free pure function', () => {
  test('returns OTLP JSON satisfying R-1..R-5 without network/fetch/delivery side effects', () => {
    let fetchCalled = false;
    const origFetch = globalThis.fetch;
    globalThis.fetch = () => { fetchCalled = true; return Promise.resolve({ ok: true }); };

    try {
      const result = build({
        assistantEntries: [
          assistantMsg({ text: 'A', tokens: { input: 10, output: 5 }, parts: [toolPart({ tool: 'shell', output: 'o1' })] }),
          assistantMsg({ text: 'B', tokens: { input: 20, output: 8 } }),
        ],
      });
      const spans = getSpans(result);
      const gens = generationSpans(spans);
      const root = rootSpan(spans);
      assert.equal(gens.length, 2, 'R-1: 2 generations');
      assert.equal(getJson(gens[0], 'langfuse.observation.usage_details').input, 10, 'R-2: own tokens');
      assert.equal(getJson(gens[1], 'langfuse.observation.usage_details').input, 20, 'R-2: own tokens');
      assert.equal(toolSpans(spans).length, 1, 'R-3: 1 tool span under gen[0]');
      assert.equal(toolSpans(spans)[0].parentSpanId, gens[0].spanId, 'R-3: tool under its generation');
      assert.ok(Array.isArray(getJson(gens[1], 'langfuse.observation.input')), 'R-4: gen[1] input is tool results');
      assert.equal(getJson(root, 'langfuse.trace.output').content, 'B', 'R-5: root output is last text only');
      assert.equal(fetchCalled, false,
        'R-6: no fetch/network call must occur during buildOtlpJson (deliverTrace/curlFetch not invoked)');
    } finally {
      globalThis.fetch = origFetch;
    }
  });
});
