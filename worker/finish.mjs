// Pi emits the accepted report, then ends through its native tool termination.
export const FINISH_TOOL_NAME = "finish";
export const MAX_REPORT_BYTES = 1024 * 1024;
export const MAX_REPORT_DEPTH = 128;
export const FINISH_STATUSES = ["completed", "blocked"];
// Both runtimes must reject whitespace-only summaries before termination.
const SUMMARY_TEXT_PATTERN = /[^\s\u001c-\u001f\u0085]/u;
const SINGLE_DIGIT_NEGATIVE_EXPONENT_PATTERN = /e-[0-9]$/;
export const FINISH_PARAMETERS = {
  type: "object",
  properties: {
    status: { type: "string", enum: FINISH_STATUSES },
    summary: { type: "string", minLength: 1, pattern: SUMMARY_TEXT_PATTERN.source },
    result: {
      type: "object",
      additionalProperties: true,
      description: "Task data as a JSON object. Nested values can use any JSON type.",
    },
  },
  required: ["status", "summary"],
  additionalProperties: false,
};

const REPORT_FIELDS = new Set(Object.keys(FINISH_PARAMETERS.properties));
const LONE_SURROGATE_PATTERN = /[\uD800-\uDFFF]/u;
const INVALID_JSON_MESSAGE = "The finish report must contain only valid JSON data and UTF-8 text.";

function validateJson(value) {
  const pending = [{ value, depth: 0 }];
  const parents = new Set();
  let exponentPaddingBytes = 0;
  while (pending.length) {
    const current = pending.pop();
    if (current.leave) {
      parents.delete(current.value);
      continue;
    }
    if (current.depth > MAX_REPORT_DEPTH) {
      throw new Error(`The finish report exceeds ${MAX_REPORT_DEPTH} nesting levels. Reduce nesting and retry.`);
    }
    const item = current.value;
    if (item === null || typeof item === "boolean") continue;
    if (typeof item === "string") {
      if (LONE_SURROGATE_PATTERN.test(item)) throw new Error(INVALID_JSON_MESSAGE);
      continue;
    }
    if (typeof item === "number") {
      if (!Number.isFinite(item)) throw new Error(INVALID_JSON_MESSAGE);
      // Python pads short exponents: 1e-7 becomes 1e-07 in the saved report.
      if (SINGLE_DIGIT_NEGATIVE_EXPONENT_PATTERN.test(JSON.stringify(item))) exponentPaddingBytes++;
      continue;
    }
    if (typeof item !== "object") throw new Error(INVALID_JSON_MESSAGE);
    const array = Array.isArray(item);
    const prototype = Object.getPrototypeOf(item);
    if (prototype !== (array ? Array.prototype : Object.prototype) && prototype !== null) {
      throw new Error(INVALID_JSON_MESSAGE);
    }
    if (parents.has(item)) throw new Error(INVALID_JSON_MESSAGE);
    parents.add(item);
    pending.push({ value: item, leave: true });
    const keys = Reflect.ownKeys(item).filter((key) => !(array && key === "length"));
    if (array && keys.length !== item.length) throw new Error(INVALID_JSON_MESSAGE);
    for (const key of keys) {
      if (typeof key !== "string" || LONE_SURROGATE_PATTERN.test(key)) throw new Error(INVALID_JSON_MESSAGE);
      if (array && (!/^(0|[1-9][0-9]*)$/.test(key) || Number(key) >= item.length)) {
        throw new Error(INVALID_JSON_MESSAGE);
      }
      const descriptor = Object.getOwnPropertyDescriptor(item, key);
      if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value")) throw new Error(INVALID_JSON_MESSAGE);
      pending.push({ value: descriptor.value, depth: current.depth + 1 });
    }
  }
  return exponentPaddingBytes;
}

export function validateReport(params) {
  if (params === null || typeof params !== "object" || Array.isArray(params)) {
    throw new Error("Pass a finish report object with status and summary.");
  }
  if (Reflect.ownKeys(params).some((key) => !REPORT_FIELDS.has(key))) {
    throw new Error("Use only status, summary, and optional result in the finish report.");
  }
  if (!Object.hasOwn(params, "status") || !FINISH_STATUSES.includes(params.status)) {
    throw new Error("Set status to completed or blocked.");
  }
  if (!Object.hasOwn(params, "summary") || typeof params.summary !== "string" || !SUMMARY_TEXT_PATTERN.test(params.summary)) {
    throw new Error("Provide a nonempty summary.");
  }
  // Keep task data structured before Pi can coerce or drop a root value.
  if (Object.hasOwn(params, "result")) {
    const result = params.result;
    if (result === null || typeof result !== "object" || Array.isArray(result)
      || ![Object.prototype, null].includes(Object.getPrototypeOf(result))) {
      throw new Error("Pass result as a JSON object, or omit it.");
    }
  }
  // Reject values JSON.stringify would drop or change, then detach the report.
  const exponentPaddingBytes = validateJson(params);
  let encoded;
  try {
    encoded = JSON.stringify(params);
  } catch {
    throw new Error(INVALID_JSON_MESSAGE);
  }
  if (Buffer.byteLength(encoded, "utf8") + exponentPaddingBytes > MAX_REPORT_BYTES) {
    throw new Error("The finish report exceeds 1 MiB. Reduce the report and retry.");
  }
  return JSON.parse(encoded);
}

export default function registerFinish(pi) {
  let standaloneCallId = null;
  let accepted = false;

  pi.on("turn_start", () => { standaloneCallId = null; });
  pi.on("message_end", ({ message }) => {
    if (message?.role !== "assistant") return;
    const calls = Array.isArray(message.content) ? message.content.filter((item) => item?.type === "toolCall") : [];
    standaloneCallId = calls.length === 1 && calls[0].name === FINISH_TOOL_NAME ? calls[0].id : null;
  });
  pi.on("session_before_compact", () => {
    // No extra model call is needed after the accepted terminal report.
    if (accepted) return { cancel: true };
  });

  pi.registerTool({
    name: FINISH_TOOL_NAME,
    label: "Finish",
    description: "End this run with a final status, summary, and optional result object. Call finish alone after other tools complete.",
    promptSnippet: "Submit the final report and end the run",
    promptGuidelines: ["Call finish alone after all other work is complete. Use blocked when a required action cannot be completed."],
    parameters: FINISH_PARAMETERS,
    prepareArguments: validateReport,
    executionMode: "sequential",
    async execute(toolCallId, params, signal) {
      if (accepted) throw new Error("A finish report was already accepted.");
      if (!standaloneCallId || toolCallId !== standaloneCallId) {
        // Pi terminates only when every result in a batch requests termination.
        throw new Error("Call finish alone after all other tools complete, then retry.");
      }
      if (signal?.aborted) throw new Error("Finish was cancelled.");
      const report = validateReport(params);
      accepted = true;
      return {
        content: [{ type: "text", text: "Final report accepted." }],
        details: { report },
        terminate: true,
      };
    },
  });
}
