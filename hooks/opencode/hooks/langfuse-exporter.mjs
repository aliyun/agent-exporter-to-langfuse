import { readFileSync, mkdirSync, appendFileSync, statSync, renameSync, unlinkSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { homedir, userInfo } from 'node:os';
import { createHash, randomBytes } from 'node:crypto';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Import deliverTrace from the langstash-deliver package.
// In dev: relative path within the repo. Installed: co-located dist.
let deliverTrace;
const __dirname_hook = dirname(fileURLToPath(import.meta.url));
try {
  const mod = await import(join(__dirname_hook, '..', '..', 'langstash-deliver', 'typescript', 'dist', 'index.js'));
  deliverTrace = mod.deliverTrace;
} catch {
  try {
    const mod = await import(join(__dirname_hook, 'langstash-deliver', 'index.js'));
    deliverTrace = mod.deliverTrace;
  } catch {
    // deliverTrace unavailable — will be checked at runtime
  }
}

// OpenCode uses Bun runtime which blocks outbound HTTP from plugins.
// This curl-based fetch bypasses Bun's network restrictions.
const curlFetch = (url, options = {}) => {
  const method = (options.method || 'GET').toUpperCase();
  const headers = options.headers || {};
  const body = options.body;

  const args = ['curl', '-s', '-w', '\\n%{http_code}', '--max-time', '10', '-X', method];
  if (typeof headers === 'object') {
    const entries = headers instanceof Headers ? [...headers.entries()] : Object.entries(headers);
    for (const [k, v] of entries) {
      args.push('-H', `${k}: ${v}`);
    }
  }
  if (body) {
    args.push('--data-binary', '@-');
  }
  args.push(url.toString());

  try {
    const cmd = args.map(a => `'${String(a).replace(/'/g, "'\\''")}'`).join(' ');
    const output = execSync(cmd, {
      encoding: 'utf8',
      timeout: 15000,
      input: body ? String(body) : undefined,
    });
    const lines = output.trimEnd().split('\n');
    const statusCode = parseInt(lines.pop(), 10) || 0;
    const responseBody = lines.join('\n');

    return Promise.resolve({
      status: statusCode,
      ok: statusCode >= 200 && statusCode < 300,
      text: () => Promise.resolve(responseBody),
      json: () => Promise.resolve(JSON.parse(responseBody)),
      headers: new Headers(),
    });
  } catch (e) {
    writeLogFile('WARN', `curlFetch ${method} ${url}: ${e.message}`);
    return Promise.resolve({
      status: 0,
      ok: false,
      text: () => Promise.resolve(''),
      json: () => Promise.resolve({}),
      headers: new Headers(),
    });
  }
};

// Load env file as fallback when shell profile hasn't been sourced
const ENV_FILE = join(homedir(), '.agent-exporter-to-langfuse', 'config', 'opencode.env');
try {
  const content = readFileSync(ENV_FILE, 'utf8');
  for (const line of content.split('\n')) {
    const m = line.match(/^export\s+([A-Za-z_][A-Za-z0-9_]*)="(.*)"\s*$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
  }
} catch {}

const env = (name) => process.env[name] || '';
const MAX_CHARS = parseInt(env('LANGFUSE_MAX_CHARS') || '800000', 10) || 800000;
const DEBUG = env('LANGFUSE_DEBUG').toLowerCase() !== 'false';
const TAGS = (env('LANGFUSE_TAGS') || 'opencode').split(',').map(t => t.trim()).filter(Boolean);

const LOG_DIR = join(homedir(), '.config', 'opencode', 'logs', 'langfuse-exporter');
const LOG_FILE = join(LOG_DIR, 'langfuse_hook.log');
const LOG_MAX_BYTES = 200_000_000;
const LOG_BACKUP_COUNT = 3;
let logDirReady = false;
let logRotationChecked = false;

const pad = (n) => String(n).padStart(2, '0');
const logTimestamp = () => {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

const rotateIfNeeded = () => {
  if (logRotationChecked) return;
  logRotationChecked = true;
  try {
    const stat = statSync(LOG_FILE);
    if (stat.size < LOG_MAX_BYTES) return;
    for (let i = LOG_BACKUP_COUNT - 1; i >= 1; i--) {
      try { renameSync(`${LOG_FILE}.${i}`, `${LOG_FILE}.${i + 1}`); } catch {}
    }
    try { renameSync(LOG_FILE, `${LOG_FILE}.1`); } catch {}
    try { unlinkSync(`${LOG_FILE}.${LOG_BACKUP_COUNT + 1}`); } catch {}
  } catch {}
};

const writeLogFile = (level, msg) => {
  try {
    if (!logDirReady) { mkdirSync(LOG_DIR, { recursive: true }); logDirReady = true; }
    rotateIfNeeded();
    const line = `${logTimestamp()} [${level}] ${msg}\n`;
    appendFileSync(LOG_FILE, line, 'utf8');
  } catch {}
};

const debug = (msg) => { if (DEBUG) writeLogFile('DEBUG', msg); };
const info = (msg) => writeLogFile('INFO', msg);
const warn = (msg) => writeLogFile('WARN', msg);

const truncate = (s, max = MAX_CHARS) => {
  if (!s) return ['', { truncated: false, origLen: 0 }];
  const origLen = s.length;
  if (origLen <= max) return [s, { truncated: false, origLen }];
  return [
    s.slice(0, max),
    { truncated: true, origLen, keptLen: max, sha256: createHash('sha256').update(s).digest('hex') },
  ];
};

const resolveUserId = () => {
  const explicit = env('LANGFUSE_USER_ID');
  if (explicit) return explicit;
  try { return userInfo().username; } catch {}
  return env('USER') || env('LOGNAME') || env('USERNAME') || undefined;
};

const extractText = (parts) => {
  if (!Array.isArray(parts)) return '';
  return parts
    .filter(p => p.type === 'text' && !p.synthetic && !p.ignored)
    .map(p => p.text || '')
    .join('\n');
};

const extractTools = (parts) => {
  if (!Array.isArray(parts)) return [];
  return parts.filter(p => p.type === 'tool');
};

// ----------------- OTLP JSON builder -----------------

const toNanoStr = (date) => String(BigInt(date.getTime()) * 1_000_000n);

export const buildOtlpJson = ({
  langfuseSessionID, sessionID, turnNum,
  userMsg, userParts, assistantEntries, sessionModel, isSubagent,
}) => {
  const traceId = randomBytes(16).toString('hex');
  const rootSpanId = randomBytes(8).toString('hex');

  const traceName = isSubagent
    ? `OpenCode - Subagent Turn ${turnNum}`
    : `OpenCode - Turn ${turnNum}`;

  const userId = resolveUserId();

  const userTextRaw = extractText(userParts);
  const [userText] = truncate(userTextRaw);

  const userTime = userMsg?.time?.created ? new Date(userMsg.time.created) : new Date();

  const lastEntry = assistantEntries[assistantEntries.length - 1] || {};
  const lastInfo = lastEntry.info || {};
  const lastTextRaw = extractText(lastEntry.parts || []);
  const [lastAssistantText] = truncate(lastTextRaw);
  const lastAssistantEndTime = lastInfo.time?.completed
    ? new Date(lastInfo.time.completed)
    : (lastInfo.time?.created ? new Date(lastInfo.time.created) : userTime);

  const rootAttrs = [
    { key: 'langfuse.trace.name', value: { stringValue: traceName } },
    { key: 'session.id', value: { stringValue: langfuseSessionID } },
    { key: 'langfuse.trace.tags', value: { stringValue: JSON.stringify(TAGS) } },
    { key: 'langfuse.trace.input', value: { stringValue: JSON.stringify({ role: 'user', content: userText }) } },
    { key: 'langfuse.trace.output', value: { stringValue: JSON.stringify({ role: 'assistant', content: lastAssistantText }) } },
    { key: 'langfuse.trace.metadata', value: { stringValue: JSON.stringify({
      source: 'opencode', turn_number: turnNum, is_subagent: isSubagent,
      sessionId: sessionID, agent: userMsg?.agent,
    }) } },
  ];
  if (userId) {
    rootAttrs.push({ key: 'user.id', value: { stringValue: userId } });
  }

  const rootSpan = {
    traceId,
    spanId: rootSpanId,
    name: traceName,
    startTimeUnixNano: toNanoStr(userTime),
    endTimeUnixNano: toNanoStr(lastAssistantEndTime),
    attributes: rootAttrs,
  };

  const spans = [rootSpan];

  let prevToolResults = null;

  for (let i = 0; i < assistantEntries.length; i++) {
    const entry = assistantEntries[i];
    const info = entry.info || {};
    const parts = entry.parts || [];

    const genSpanId = randomBytes(8).toString('hex');

    const modelID = info.modelID || sessionModel?.modelID || 'unknown';
    const providerID = info.providerID || sessionModel?.providerID || '';
    const modelName = providerID ? `${providerID}/${modelID}` : modelID;

    const genTextRaw = extractText(parts);
    const [genText] = truncate(genTextRaw);
    const msgTools = extractTools(parts);
    const genToolCalls = msgTools.map(tp => {
      const state = tp.state || {};
      const inputStr = typeof state.input === 'object' ? JSON.stringify(state.input) : String(state.input || '');
      const [truncInput] = truncate(inputStr);
      return { id: tp.callID, name: tp.tool, input: truncInput };
    });
    const genOutput = { role: 'assistant' };
    if (genText) genOutput.content = genText;
    if (genToolCalls.length) genOutput.tool_calls = genToolCalls;

    const tokens = info.tokens || {};
    const usage = {};
    if (tokens.input > 0) usage.input = tokens.input;
    if (tokens.output > 0) usage.output = tokens.output;
    const cacheRead = tokens.cache?.read;
    const cacheWrite = tokens.cache?.write;
    if (cacheRead > 0) usage.cache_read_input_tokens = cacheRead;
    if (cacheWrite > 0) usage.cache_creation_input_tokens = cacheWrite;

    const genMeta = {
      toolCount: msgTools.length,
      finish: info.finish,
      mode: info.mode,
      agent: userMsg?.agent,
    };
    if (info.cost) genMeta.cost = info.cost;

    const genStart = info.time?.created ? new Date(info.time.created) : userTime;
    const genEnd = info.time?.completed ? new Date(info.time.completed) : genStart;

    const genAttrs = [
      { key: 'langfuse.observation.type', value: { stringValue: 'generation' } },
      { key: 'langfuse.observation.name', value: { stringValue: 'Generation' } },
      { key: 'langfuse.observation.model.name', value: { stringValue: modelName } },
    ];

    if (i === 0) {
      genAttrs.push({ key: 'langfuse.observation.input', value: { stringValue: JSON.stringify({ role: 'user', content: userText }) } });
    } else if (prevToolResults !== null) {
      genAttrs.push({ key: 'langfuse.observation.input', value: { stringValue: JSON.stringify(prevToolResults) } });
    }

    genAttrs.push({ key: 'langfuse.observation.output', value: { stringValue: JSON.stringify(genOutput) } });
    genAttrs.push({ key: 'langfuse.observation.metadata', value: { stringValue: JSON.stringify(genMeta) } });
    if (Object.keys(usage).length) {
      genAttrs.push({ key: 'langfuse.observation.usage_details', value: { stringValue: JSON.stringify(usage) } });
    }

    spans.push({
      traceId,
      spanId: genSpanId,
      parentSpanId: rootSpanId,
      name: 'Generation',
      startTimeUnixNano: toNanoStr(genStart),
      endTimeUnixNano: toNanoStr(genEnd),
      attributes: genAttrs,
    });

    const qualifiedTools = msgTools.filter(tp => {
      const s = tp.state || {};
      return !s.status || s.status === 'completed' || s.status === 'error';
    });
    for (const tp of qualifiedTools) {
      const state = tp.state || {};
      const toolInput = state.input || {};
      const inputStr = typeof toolInput === 'object' ? JSON.stringify(toolInput) : String(toolInput);
      const [truncInput] = truncate(inputStr);
      let toolOutput = state.output || state.error || '';
      if (typeof toolOutput !== 'string') toolOutput = JSON.stringify(toolOutput);
      const [truncOutput] = truncate(toolOutput);
      const toolStart = state.time?.start ? new Date(state.time.start) : genStart;
      const toolEnd = state.time?.end ? new Date(state.time.end) : genEnd;
      const toolSpanId = randomBytes(8).toString('hex');

      spans.push({
        traceId,
        spanId: toolSpanId,
        parentSpanId: genSpanId,
        name: `Tool: ${tp.tool || 'unknown'}`,
        startTimeUnixNano: toNanoStr(toolStart),
        endTimeUnixNano: toNanoStr(toolEnd),
        attributes: [
          { key: 'langfuse.observation.type', value: { stringValue: 'tool' } },
          { key: 'langfuse.observation.name', value: { stringValue: `Tool: ${tp.tool || 'unknown'}` } },
          { key: 'langfuse.observation.input', value: { stringValue: truncInput } },
          { key: 'langfuse.observation.output', value: { stringValue: truncOutput } },
          { key: 'langfuse.observation.metadata', value: { stringValue: JSON.stringify({
            tool_name: tp.tool, callID: tp.callID, status: state.status,
          }) } },
        ],
      });
    }

    if (qualifiedTools.length > 0) {
      prevToolResults = qualifiedTools.map(tp => {
        const state = tp.state || {};
        const item = { name: tp.tool };
        if (state.output != null) {
          let out = state.output;
          if (typeof out !== 'string') out = JSON.stringify(out);
          const [truncOut] = truncate(out);
          item.output = truncOut;
        }
        if (state.error != null) {
          let err = state.error;
          if (typeof err !== 'string') err = JSON.stringify(err);
          const [truncErr] = truncate(err);
          item.error = truncErr;
        }
        return item;
      });
    } else {
      prevToolResults = null;
    }
  }

  return {
    resourceSpans: [{
      scopeSpans: [{
        scope: { name: 'agent-exporter-to-langfuse' },
        spans,
      }],
    }],
  };
};

// ----------------- Plugin -----------------

const LangfuseExporterPlugin = async (ctx) => {
  try { return await _initPlugin(ctx); } catch (e) {
    await warn(`Plugin init error: ${e.message}`).catch(() => {});
    return {};
  }
};

const _initPlugin = async (ctx) => {
  const publicKey = env('LANGFUSE_PUBLIC_KEY');
  const secretKey = env('LANGFUSE_SECRET_KEY');
  const baseUrl = env('LANGFUSE_BASE_URL') || 'https://us.cloud.langfuse.com';

  if (!publicKey || !secretKey) {
    await warn('LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY not set, plugin disabled');
    return {};
  }

  if (!deliverTrace) {
    await warn('deliverTrace not available (langstash-deliver not found), plugin disabled');
    return {};
  }

  await info('Plugin initialized (OTLP + langstash-deliver)');
  await debug(`Langfuse config: host=${baseUrl}, public_key=${publicKey.slice(0, 12)}...`);

  const client = ctx.client;

  const processedCounts = new Map();
  const sessionModels = new Map();
  const sessionParents = new Map();

  // Resolve subagent sessions to their parent for Langfuse session grouping
  const resolveRootSessionID = async (sessionID) => {
    if (sessionParents.has(sessionID)) return sessionParents.get(sessionID);
    try {
      const sessResp = await client.session.get({ path: { id: sessionID } });
      const parentID = sessResp.data?.parentID;
      if (parentID) {
        await debug(`Session ${sessionID} is subagent of ${parentID}`);
        sessionParents.set(sessionID, parentID);
        return parentID;
      }
    } catch {}
    sessionParents.set(sessionID, sessionID);
    return sessionID;
  };

  // ----------------- Session processing -----------------

  const processSession = async (sessionID) => {
    const start = Date.now();
    try {
      const langfuseSessionID = await resolveRootSessionID(sessionID);
      const resp = await client.session.messages({ path: { id: sessionID } });
      if (!resp.data) {
        await debug(`No messages for session ${sessionID}`);
        return;
      }
      const allMessages = resp.data;
      const prevCount = processedCounts.get(sessionID) || 0;

      // Log message summary
      if (allMessages.length > 0) {
        const summary = allMessages.map((m, i) => {
          const r = m.info.role;
          const parts = (m.parts || []).map(p => p.type === 'tool' ? `tool:${p.tool}` : p.type).join(',');
          return `[${i}]${r}(${parts})`;
        }).join(' ');
        await debug(`Session ${sessionID}: ${allMessages.length} messages: ${summary}`);
      }

      if (allMessages.length <= prevCount) {
        await debug(`Session ${sessionID}: no new messages (${allMessages.length} total, ${prevCount} processed)`);
        return;
      }

      const newMessages = allMessages.slice(prevCount);

      // Count existing turns
      let turnNum = 0;
      for (let j = 0; j < prevCount; j++) {
        if (allMessages[j].info.role === 'user') turnNum++;
      }

      // Build turns: pair user message with ALL consecutive assistant messages
      let emitted = 0;
      for (let i = 0; i < newMessages.length; i++) {
        if (newMessages[i].info.role !== 'user') continue;

        const userEntry = newMessages[i];
        const assistantEntries = [];
        let j = i + 1;
        while (j < newMessages.length && newMessages[j].info.role === 'assistant') {
          assistantEntries.push(newMessages[j]);
          j++;
        }
        if (assistantEntries.length === 0) {
          const fullIdx = prevCount + i;
          let fj = fullIdx + 1;
          while (fj < allMessages.length && allMessages[fj].info.role === 'assistant') {
            assistantEntries.push(allMessages[fj]);
            fj++;
          }
        }

        if (assistantEntries.length === 0) continue;
        i += assistantEntries.length;

        turnNum++;
        emitted++;
        try {
          await emitTurn(langfuseSessionID, sessionID, turnNum, userEntry.info, userEntry.parts || [], assistantEntries, sessionModels.get(sessionID));
        } catch (e) {
          await info(`emit_turn failed: ${e.message} (session=${sessionID}, turn=${turnNum})`);
        }
      }

      processedCounts.set(sessionID, allMessages.length);

      const dur = ((Date.now() - start) / 1000).toFixed(2);
      await info(`Processed ${emitted} turns in ${dur}s (session=${sessionID})`);
    } catch (e) {
      await warn(`processSession failed: ${e.message} (session=${sessionID})`);
    }
  };

  // ----------------- Turn emit -----------------

  const emitTurn = async (langfuseSessionID, sessionID, turnNum, userMsg, userParts, assistantEntries, sessionModel) => {
    const isSubagent = langfuseSessionID !== sessionID;

    const totalTools = assistantEntries.reduce(
      (n, e) => n + extractTools(e.parts || []).length, 0);
    const allText = assistantEntries.map(e => extractText(e.parts || [])).join('');
    if (!allText && totalTools === 0) {
      await warn(`turn ${turnNum}: empty output (no text, no tools)`);
    }

    const firstInfo = assistantEntries[0].info || {};
    const modelID = firstInfo.modelID || sessionModel?.modelID || 'unknown';
    const providerID = firstInfo.providerID || sessionModel?.providerID || '';
    const modelName = providerID ? `${providerID}/${modelID}` : modelID;

    // --- Build OTLP JSON and deliver via langstash-deliver ---
    try {
      const otlpJson = buildOtlpJson({
        langfuseSessionID, sessionID, turnNum, userMsg, userParts,
        assistantEntries, sessionModel, isSubagent,
      });
      const ok = await deliverTrace(otlpJson, { fetchFn: curlFetch });
      if (ok) {
        await debug(`Delivered turn ${turnNum}: generations=${assistantEntries.length} model=${modelName} tools=${totalTools}${isSubagent ? ' (subagent)' : ''}`);
      } else {
        await warn(`deliverTrace returned false for turn ${turnNum} (saved to failed log)`);
      }
    } catch (e) {
      await warn(`deliverTrace error for turn ${turnNum}: ${e.message}`);
    }
  };

  // ----------------- Hooks -----------------

  return {
    'chat.params': async (input) => {
      try {
        if (!input.sessionID || !input.model) return;
        const modelID = input.model.id || '';
        const providerID = input.model.providerID || '';
        if (modelID) {
          sessionModels.set(input.sessionID, { modelID, providerID });
        }
        if (sessionModels.size > 500) {
          const oldest = sessionModels.keys().next().value;
          sessionModels.delete(oldest);
        }
      } catch (e) {
        await warn(`chat.params hook error: ${e.message}`).catch(() => {});
      }
    },

    event: async ({ event }) => {
      try {
        if (!event) return;
        if (event.type === 'session.idle') {
          const sessionID = event.properties?.sessionID;
          if (sessionID) {
            await debug(`session.idle: ${sessionID}`);
            await processSession(sessionID);
          }
        }
      } catch (e) {
        await warn(`event hook error: ${e.message}`).catch(() => {});
      }
    },
  };
};

export default { id: "langfuse-exporter", server: LangfuseExporterPlugin };
