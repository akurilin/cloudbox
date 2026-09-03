import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readSync,
} from "node:fs";
import { relative, resolve, sep } from "node:path";

// Pi emits the accepted report, then ends through its native tool termination.
export const FINISH_TOOL_NAME = "finish";
export const MAX_REPORT_BYTES = 1024 * 1024;
export const MAX_REPORT_DEPTH = 128;
export const MAX_FINISH_REMINDERS = 1;
const NORMAL_STOP_REASON = "stop";
const FINISH_REMINDER_TYPE = "cloudbox_finish_reminder";
const FINISH_REMINDER =
  "No finish report was submitted. Do not repeat completed work. Call finish alone with your existing answer, status, and a short summary. Use the existing response_file for published files; otherwise use response.";
const MAX_RESPONSE_PATH_BYTES = 4096;
const RESPONSE_DIRECTORY = "output";
const RESPONSE_FILE_ERROR =
  "Use a regular response_file inside output/, without traversal or links.";
export const FINISH_STATUSES = ["completed", "blocked"];
// Both runtimes reject blank summaries and responses before termination.
const SUMMARY_TEXT_PATTERN = /[^\s\u001c-\u001f\u0085]/u;
const SINGLE_DIGIT_NEGATIVE_EXPONENT_PATTERN = /e-[0-9]$/;
// Keep regex checks local so provider regex support cannot limit response text.
export const FINISH_PARAMETERS = {
  type: "object",
  properties: {
    status: { type: "string", enum: FINISH_STATUSES },
    summary: {
      type: "string",
      minLength: 1,
    },
    response: {
      type: "string",
      minLength: 1,
      description:
        "The full answer shown to the user, including all published file URLs.",
    },
    response_file: {
      type: "string",
      minLength: 1,
      maxLength: MAX_RESPONSE_PATH_BYTES,
      description:
        "UTF-8 final answer file under output/. Read publication receipts in code to preserve exact URLs, write this file, then pass its workspace-relative or absolute path.",
    },
    result: {
      type: "object",
      additionalProperties: true,
      description:
        "Task data as a JSON object. Nested values can use any JSON type.",
    },
  },
  required: ["status", "summary"],
  oneOf: [{ required: ["response"] }, { required: ["response_file"] }],
  additionalProperties: false,
};

const REPORT_FIELDS = new Set(["status", "summary", "response", "result"]);
const INPUT_FIELDS = new Set(Object.keys(FINISH_PARAMETERS.properties));
const LONE_SURROGATE_PATTERN = /[\uD800-\uDFFF]/u;
const INVALID_JSON_MESSAGE =
  "The finish report must contain only valid JSON data and UTF-8 text.";

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
      throw new Error(
        `The finish report exceeds ${MAX_REPORT_DEPTH} nesting levels. Reduce nesting and retry.`,
      );
    }
    const item = current.value;
    if (item === null || typeof item === "boolean") continue;
    if (typeof item === "string") {
      if (LONE_SURROGATE_PATTERN.test(item))
        throw new Error(INVALID_JSON_MESSAGE);
      continue;
    }
    if (typeof item === "number") {
      if (!Number.isFinite(item)) throw new Error(INVALID_JSON_MESSAGE);
      // Python pads short exponents: 1e-7 becomes 1e-07 in the saved report.
      if (SINGLE_DIGIT_NEGATIVE_EXPONENT_PATTERN.test(JSON.stringify(item)))
        exponentPaddingBytes++;
      continue;
    }
    if (typeof item !== "object") throw new Error(INVALID_JSON_MESSAGE);
    const array = Array.isArray(item);
    const prototype = Object.getPrototypeOf(item);
    if (
      prototype !== (array ? Array.prototype : Object.prototype) &&
      prototype !== null
    ) {
      throw new Error(INVALID_JSON_MESSAGE);
    }
    if (parents.has(item)) throw new Error(INVALID_JSON_MESSAGE);
    parents.add(item);
    pending.push({ value: item, leave: true });
    const keys = Reflect.ownKeys(item).filter(
      (key) => !(array && key === "length"),
    );
    if (array && keys.length !== item.length)
      throw new Error(INVALID_JSON_MESSAGE);
    for (const key of keys) {
      if (typeof key !== "string" || LONE_SURROGATE_PATTERN.test(key))
        throw new Error(INVALID_JSON_MESSAGE);
      if (
        array &&
        (!/^(0|[1-9][0-9]*)$/.test(key) || Number(key) >= item.length)
      ) {
        throw new Error(INVALID_JSON_MESSAGE);
      }
      const descriptor = Object.getOwnPropertyDescriptor(item, key);
      if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value"))
        throw new Error(INVALID_JSON_MESSAGE);
      pending.push({ value: descriptor.value, depth: current.depth + 1 });
    }
  }
  return exponentPaddingBytes;
}

export function validateReport(params) {
  if (params === null || typeof params !== "object" || Array.isArray(params)) {
    throw new Error("Pass a finish report with status, summary, and response.");
  }
  if (Reflect.ownKeys(params).some((key) => !REPORT_FIELDS.has(key))) {
    throw new Error(
      "Use status, summary, response, and optional result in the finish report.",
    );
  }
  if (
    !Object.hasOwn(params, "response") ||
    typeof params.response !== "string" ||
    !SUMMARY_TEXT_PATTERN.test(params.response)
  ) {
    throw new Error(
      "Provide the full answer and any published file URLs in response.",
    );
  }
  if (
    !Object.hasOwn(params, "status") ||
    !FINISH_STATUSES.includes(params.status)
  ) {
    throw new Error("Set status to completed or blocked.");
  }
  if (
    !Object.hasOwn(params, "summary") ||
    typeof params.summary !== "string" ||
    !SUMMARY_TEXT_PATTERN.test(params.summary)
  ) {
    throw new Error("Provide a nonempty summary.");
  }
  // Keep task data structured before Pi can coerce or drop a root value.
  if (Object.hasOwn(params, "result")) {
    const result = params.result;
    if (
      result === null ||
      typeof result !== "object" ||
      Array.isArray(result) ||
      ![Object.prototype, null].includes(Object.getPrototypeOf(result))
    ) {
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
  if (
    Buffer.byteLength(encoded, "utf8") + exponentPaddingBytes >
    MAX_REPORT_BYTES
  ) {
    throw new Error(
      "The finish report exceeds 1 MiB. Reduce the report and retry.",
    );
  }
  return JSON.parse(encoded);
}

function inspectResponsePath(workspace, path) {
  let current = workspace;
  const parts = relative(workspace, path).split(sep);
  for (const [index, part] of parts.entries()) {
    current = resolve(current, part);
    const stats = lstatSync(current);
    if (stats.isSymbolicLink()) throw new Error(RESPONSE_FILE_ERROR);
    if (index < parts.length - 1) {
      if (!stats.isDirectory()) throw new Error(RESPONSE_FILE_ERROR);
    } else {
      if (!stats.isFile() || stats.nlink !== 1)
        throw new Error(RESPONSE_FILE_ERROR);
      return stats;
    }
  }
  throw new Error(RESPONSE_FILE_ERROR);
}

function readResponseFile(value, workspace) {
  if (
    typeof value !== "string" ||
    !value ||
    value.includes("\0") ||
    value.split(sep).includes("..") ||
    Buffer.byteLength(value, "utf8") > MAX_RESPONSE_PATH_BYTES
  ) {
    throw new Error(RESPONSE_FILE_ERROR);
  }
  workspace = resolve(workspace);
  const path = resolve(workspace, value);
  const local = relative(resolve(workspace, RESPONSE_DIRECTORY), path);
  if (!local || local === ".." || local.startsWith(`..${sep}`)) {
    throw new Error(RESPONSE_FILE_ERROR);
  }
  let descriptor;
  let length = 0;
  const bytes = Buffer.alloc(MAX_REPORT_BYTES + 1);
  try {
    const before = inspectResponsePath(workspace, path);
    // A nonblocking, no-follow open cannot wait on a replaced pipe or follow a link.
    descriptor = openSync(
      path,
      constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
    );
    const opened = fstatSync(descriptor);
    const after = inspectResponsePath(workspace, path);
    if (
      !opened.isFile() ||
      opened.nlink !== 1 ||
      opened.dev !== before.dev ||
      opened.ino !== before.ino ||
      opened.dev !== after.dev ||
      opened.ino !== after.ino
    ) {
      throw new Error(RESPONSE_FILE_ERROR);
    }
    if (opened.size > MAX_REPORT_BYTES) {
      throw new Error("The response_file exceeds 1 MiB. Reduce it and retry.");
    }
    while (length < bytes.length) {
      const count = readSync(descriptor, bytes, length, bytes.length - length);
      if (!count) break;
      length += count;
    }
    if (length > MAX_REPORT_BYTES) {
      throw new Error("The response_file exceeds 1 MiB. Reduce it and retry.");
    }
    const finished = fstatSync(descriptor);
    if (
      finished.size !== length ||
      finished.mtimeMs !== opened.mtimeMs ||
      finished.ctimeMs !== opened.ctimeMs
    ) {
      throw new Error(
        "The response_file changed during reading. Close it and retry.",
      );
    }
  } catch (error) {
    if (error.code) throw new Error(RESPONSE_FILE_ERROR);
    throw error;
  } finally {
    if (descriptor !== undefined) closeSync(descriptor);
  }
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
      bytes.subarray(0, length),
    );
  } catch {
    throw new Error("The response_file must contain valid UTF-8 text.");
  }
}

export function prepareReport(params, workspace = process.cwd()) {
  if (params === null || typeof params !== "object" || Array.isArray(params)) {
    return validateReport(params);
  }
  if (
    Object.hasOwn(params, "response") === Object.hasOwn(params, "response_file")
  ) {
    throw new Error("Use exactly one of response and response_file.");
  }
  if (!Object.hasOwn(params, "response_file")) return validateReport(params);
  if (Reflect.ownKeys(params).some((key) => !INPUT_FIELDS.has(key))) {
    throw new Error("Use status, summary, response_file, and optional result.");
  }
  validateJson(params);
  const { response_file, ...report } = params;
  // Read the agent's answer unchanged; the saved report keeps one response field.
  return validateReport({
    ...report,
    response: readResponseFile(response_file, workspace),
  });
}

export default function registerFinish(pi) {
  let standaloneCallId = null;
  let accepted = false;
  let reminders = 0;

  pi.on("turn_start", () => {
    standaloneCallId = null;
  });
  pi.on("message_end", ({ message }) => {
    if (message?.role !== "assistant") return;
    const calls = Array.isArray(message.content)
      ? message.content.filter((item) => item?.type === "toolCall")
      : [];
    standaloneCallId =
      calls.length === 1 && calls[0].name === FINISH_TOOL_NAME
        ? calls[0].id
        : null;
  });
  pi.on("agent_end", ({ messages }, ctx) => {
    if (
      accepted ||
      reminders >= MAX_FINISH_REMINDERS ||
      ctx.signal?.aborted ||
      ctx.hasPendingMessages()
    )
      return;
    const message = messages.at(-1);
    if (
      message?.role !== "assistant" ||
      message.stopReason !== NORMAL_STOP_REASON ||
      !Array.isArray(message.content) ||
      message.content.some((item) => item.type === "toolCall") ||
      !message.content.some(
        (item) =>
          item.type === "text" &&
          typeof item.text === "string" &&
          SUMMARY_TEXT_PATTERN.test(item.text),
      )
    )
      return;
    // Pi continues queued agent_end messages before print mode can exit.
    reminders++;
    pi.sendMessage(
      {
        customType: FINISH_REMINDER_TYPE,
        content: FINISH_REMINDER,
        display: true,
        details: { attempt: reminders },
      },
      { deliverAs: "followUp", triggerTurn: true },
    );
  });
  pi.on("session_before_compact", () => {
    // No extra model call is needed after the accepted terminal report.
    if (accepted) return { cancel: true };
  });

  pi.registerTool({
    name: FINISH_TOOL_NAME,
    label: "Finish",
    description:
      "End this run with status, summary, optional result, and exactly one of response or response_file. Include published file URLs in the final answer. Call finish alone after other tools complete.",
    promptSnippet: "Submit the final report and end the run",
    promptGuidelines: [
      "Call finish alone after all other work is complete. Use blocked when a required action cannot be completed.",
      "Include the full user answer and all published file URLs in the final answer. Use result only for optional structured data.",
      "For published files, read receipt JSON in code and write the final answer to output/response.txt. Use response_file to preserve exact URLs without copying them through model text.",
    ],
    parameters: FINISH_PARAMETERS,
    prepareArguments: (params) => prepareReport(params),
    executionMode: "sequential",
    async execute(toolCallId, params, signal) {
      if (accepted) throw new Error("A finish report was already accepted.");
      if (!standaloneCallId || toolCallId !== standaloneCallId) {
        // Pi terminates only when every result in a batch requests termination.
        throw new Error(
          "Call finish alone after all other tools complete, then retry.",
        );
      }
      if (signal?.aborted) throw new Error("Finish was cancelled.");
      const report = prepareReport(params);
      accepted = true;
      return {
        content: [{ type: "text", text: "Final report accepted." }],
        details: { report },
        terminate: true,
      };
    },
  });
}
