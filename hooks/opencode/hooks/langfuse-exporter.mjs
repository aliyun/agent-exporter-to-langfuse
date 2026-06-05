import { Langfuse } from 'langfuse';
import { appendFile, mkdir } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir, userInfo } from 'node:os';
import { createHash } from 'node:crypto';
import { execSync } from 'node:child_process';

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
const ENV_FILE = join(homedir(), '.config', 'agent-exporter-to-langfuse', 'opencode.env');
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
let logDirReady = false;

const pad = (n) => String(n).padStart(2, '0');
const logTimestamp = () => {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};
const logDateKey = () => {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

const writeLogFile = async (level, msg) => {
  try {
    if (!logDirReady) { await mkdir(LOG_DIR, { recursive: true }); logDirReady = true; }
    const line = `[${logTimestamp()}] [${level}] ${msg}\n`;
    await appendFile(join(LOG_DIR, `${logDateKey()}.log`), line, 'utf8');
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

  let langfuse;
  try {
    langfuse = new Langfuse({ publicKey, secretKey, baseUrl });
    langfuse.fetch = curlFetch;
    langfuse.on('error', async (e) => { await warn(`Langfuse SDK error: ${e.message || e}`); });
    await info('Plugin initialized (curl transport)');
    await debug(`Langfuse config: host=${baseUrl}, public_key=${publicKey.slice(0, 12)}...`);
  } catch (e) {
    await warn(`Langfuse init failed: ${e.message}`);
    return {};
  }

  const client = ctx.client;
  const directory = ctx.directory;
  const userId = resolveUserId();

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
          await emitTurn(langfuseSessionID, sessionID, turnNum, userEntry.info, userEntry.parts || [], assistantEntries);
        } catch (e) {
          await info(`emit_turn failed: ${e.message} (session=${sessionID}, turn=${turnNum})`);
        }
      }

      processedCounts.set(sessionID, allMessages.length);

      try {
        await langfuse.flushAsync();
        await debug(`Langfuse flush succeeded`);
      } catch (e) {
        await warn(`Langfuse flush failed: ${e.message}`);
      }

      const dur = ((Date.now() - start) / 1000).toFixed(2);
      await info(`Processed ${emitted} turns in ${dur}s (session=${sessionID})`);
    } catch (e) {
      await warn(`processSession failed: ${e.message} (session=${sessionID})`);
    }
  };

  // ----------------- Turn emit -----------------

  const emitTurn = async (langfuseSessionID, sessionID, turnNum, userMsg, userParts, assistantEntries) => {
    const userTextRaw = extractText(userParts);
    const [userText, userTextMeta] = truncate(userTextRaw);

    const allAssistantParts = assistantEntries.flatMap(e => e.parts || []);
    const assistantTextRaw = extractText(allAssistantParts);
    const [assistantText, assistantTextMeta] = truncate(assistantTextRaw);
    const tools = extractTools(allAssistantParts);

    const firstAssistant = assistantEntries[0].info;
    const lastAssistant = assistantEntries[assistantEntries.length - 1].info;

    const modelID = firstAssistant.modelID || sessionModels.get(sessionID)?.modelID || 'unknown';
    const providerID = firstAssistant.providerID || sessionModels.get(sessionID)?.providerID || '';
    const modelName = providerID ? `${providerID}/${modelID}` : modelID;

    const aggTokens = { input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0 };
    let aggCost = 0;
    for (const e of assistantEntries) {
      const t = e.info.tokens;
      if (t) {
        aggTokens.input += t.input || 0;
        aggTokens.output += t.output || 0;
        aggTokens.reasoning += t.reasoning || 0;
        aggTokens.cacheRead += (t.cache?.read) || 0;
        aggTokens.cacheWrite += (t.cache?.write) || 0;
      }
      aggCost += e.info.cost || 0;
    }

    const userTime = userMsg.time?.created ? new Date(userMsg.time.created) : new Date();
    const assistantStartTime = firstAssistant.time?.created ? new Date(firstAssistant.time.created) : userTime;
    const assistantEndTime = lastAssistant.time?.completed ? new Date(lastAssistant.time.completed) : assistantStartTime;

    const isSubagent = langfuseSessionID !== sessionID;
    const traceName = isSubagent
      ? `OpenCode - Subagent Turn ${turnNum}`
      : `OpenCode - Turn ${turnNum}`;

    if (!assistantText && tools.length === 0) {
      await warn(`turn ${turnNum}: empty output (no text, no tools)`);
    }

    // --- Trace ---
    const trace = langfuse.trace({
      name: traceName,
      sessionId: langfuseSessionID,
      timestamp: userTime,
      userId,
      tags: TAGS,
      input: { role: 'user', content: userText },
      output: { role: 'assistant', content: assistantText },
      metadata: {
        source: 'opencode',
        sessionId: sessionID,
        parentSessionId: isSubagent ? langfuseSessionID : undefined,
        turnNumber: turnNum,
        directory,
        userText: userTextMeta,
        assistantText: assistantTextMeta,
        assistantMessageCount: assistantEntries.length,
        toolCount: tools.length,
      },
    });

    // --- Generation ---
    const usage = {};
    if (aggTokens.input > 0) usage.input = aggTokens.input;
    if (aggTokens.output > 0) usage.output = aggTokens.output;
    if (aggTokens.cacheRead > 0) usage.cache_read_input_tokens = aggTokens.cacheRead;
    if (aggTokens.cacheWrite > 0) usage.cache_creation_input_tokens = aggTokens.cacheWrite;

    const genToolCalls = tools.map(tp => {
      const state = tp.state || {};
      const inputStr = typeof state.input === 'object' ? JSON.stringify(state.input) : String(state.input || '');
      const [truncInput] = truncate(inputStr);
      return { id: tp.callID, name: tp.tool, input: truncInput };
    });

    const genOutput = { role: 'assistant' };
    if (assistantText) genOutput.content = assistantText;
    if (genToolCalls.length) genOutput.tool_calls = genToolCalls;

    const genMeta = {
      toolCount: tools.length,
      finish: lastAssistant.finish,
      mode: firstAssistant.mode,
      agent: userMsg.agent,
    };
    if (aggCost > 0) genMeta.cost = aggCost;

    const genParams = {
      name: 'Generation',
      model: modelName,
      startTime: assistantStartTime,
      endTime: assistantEndTime,
      input: { role: 'user', content: userText },
      output: genOutput,
      metadata: genMeta,
    };
    if (Object.keys(usage).length) genParams.usage = usage;

    const generation = trace.generation(genParams);

    // --- Tool spans ---
    for (const tp of tools) {
      const state = tp.state || {};
      if (state.status && state.status !== 'completed' && state.status !== 'error') continue;

      const toolInput = state.input || {};
      const inputStr = typeof toolInput === 'object' ? JSON.stringify(toolInput) : String(toolInput);
      const [truncInput, inputMeta] = truncate(inputStr);

      let toolOutput = state.output || state.error || '';
      if (typeof toolOutput !== 'string') toolOutput = JSON.stringify(toolOutput);
      const [truncOutput, outputMeta] = truncate(toolOutput);

      const toolStart = state.time?.start ? new Date(state.time.start) : assistantStartTime;
      const toolEnd = state.time?.end ? new Date(state.time.end) : assistantEndTime;

      generation.span({
        name: `Tool: ${tp.tool || 'unknown'}`,
        startTime: toolStart,
        endTime: toolEnd,
        input: truncInput,
        output: truncOutput,
        metadata: {
          toolName: tp.tool,
          callID: tp.callID,
          status: state.status,
          title: state.title,
          inputMeta,
          outputMeta,
        },
      });
    }

    await debug(`Emitted turn ${turnNum}: model=${modelName} tools=${tools.length} tokens=${JSON.stringify(usage)}${isSubagent ? ' (subagent)' : ''}`);
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

export default LangfuseExporterPlugin;
export const server = LangfuseExporterPlugin;
