import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  linkSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import registerFinish, * as finish from "../worker/finish.mjs";

const {
  FINISH_PARAMETERS,
  FINISH_TOOL_NAME,
  MAX_FINISH_REMINDERS,
  MAX_REPORT_BYTES,
  prepareReport,
  validateReport,
} = finish;

const CALL_ID = "finish-call";
const REPORT = {
  status: "completed",
  summary: "The requested work is complete.",
  response: "The answer is 42.",
  result: { answer: 42 },
};

function extension() {
  const handlers = new Map();
  const sent = [];
  let tool;
  registerFinish({
    on(name, handler) {
      handlers.set(name, handler);
    },
    registerTool(definition) {
      tool = definition;
    },
    sendMessage(message, options) {
      sent.push({ message, options });
    },
  });
  function message(calls) {
    handlers.get("message_end")({
      message: { role: "assistant", content: calls },
    });
  }
  function standalone() {
    message([
      {
        type: "toolCall",
        name: FINISH_TOOL_NAME,
        id: CALL_ID,
        arguments: REPORT,
      },
    ]);
  }
  function end(messages, context = {}) {
    handlers.get("agent_end")?.(
      { messages },
      { hasPendingMessages: () => false, ...context },
    );
  }
  return { handlers, tool, message, standalone, sent, end };
}

test("plain assistant completion queues finish before the session settles", async () => {
  const { handlers, tool, standalone, sent, end } = extension();
  // The cloud run stopped normally with this answer but never called finish.
  const answer =
    "Gentle clouds drift by,\nsilent boxes hold the code—\nsoft rain of ideas.";
  const message = {
    role: "assistant",
    stopReason: "stop",
    content: [{ type: "text", text: answer }],
  };
  handlers.get("turn_start")({});
  handlers.get("message_end")({ message });
  assert.equal(sent.length, 0);
  end([message]);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].message.customType, "cloudbox_finish_reminder");
  assert.equal(sent[0].message.display, true);
  assert.deepEqual(sent[0].message.details, { attempt: 1 });
  assert.match(sent[0].message.content, /finish/);
  assert.match(sent[0].message.content, /existing answer/i);
  assert.match(sent[0].message.content, /do not repeat/i);
  assert.deepEqual(sent[0].options, {
    deliverAs: "followUp",
    triggerTurn: true,
  });
  standalone();
  const result = await tool.execute(CALL_ID, { ...REPORT, response: answer });
  assert.equal(result.terminate, true);
  assert.equal(result.details.report.response, answer);
  end([message]);
  assert.equal(sent.length, 1);
});

test("finish reminders stay bounded across turns", () => {
  const { handlers, sent, end } = extension();
  assert.ok(Number.isInteger(MAX_FINISH_REMINDERS));
  for (let index = 0; index < MAX_FINISH_REMINDERS + 2; index++) {
    handlers.get("turn_start")({});
    end([
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "Done." }],
      },
    ]);
  }
  assert.equal(sent.length, MAX_FINISH_REMINDERS);
});

test("errors, interrupted turns, pending messages, and tool calls do not trigger reminders", () => {
  for (const stopReason of ["error", "aborted", "length", "toolUse"]) {
    const { sent, end } = extension();
    end([
      {
        role: "assistant",
        stopReason,
        content: [{ type: "text", text: "Partial" }],
      },
    ]);
    assert.equal(sent.length, 0, stopReason);
  }
  const complete = {
    role: "assistant",
    stopReason: "stop",
    content: [{ type: "text", text: "Done." }],
  };
  const { sent, end } = extension();
  end([complete], { signal: AbortSignal.abort() });
  end([complete], { hasPendingMessages: () => true });
  end([
    { ...complete, content: [{ type: "toolCall", name: "bash", id: "tool" }] },
  ]);
  end([complete, { role: "toolResult", content: [] }]);
  end([{ ...complete, content: [{ type: "text", text: " \n" }] }]);
  assert.equal(sent.length, 0);
});

test("an accepted finish never receives a completion reminder", async () => {
  const { tool, standalone, sent, end } = extension();
  standalone();
  await tool.execute(CALL_ID, REPORT);
  end([
    {
      role: "assistant",
      stopReason: "stop",
      content: [{ type: "text", text: "Done." }],
    },
  ]);
  assert.equal(sent.length, 0);
});

function responseFixture(t, contents = "Answer: 42.\n") {
  const workspace = mkdtempSync(join(tmpdir(), "cloudbox-finish-"));
  t.after(() => rmSync(workspace, { recursive: true, force: true }));
  mkdirSync(join(workspace, "output"));
  const path = join(workspace, "output", "response.txt");
  writeFileSync(path, contents);
  const { response, ...fields } = REPORT;
  return {
    workspace,
    path,
    report: { ...fields, response_file: "output/response.txt" },
  };
}

test("response_file preserves long signed URLs and UTF-8 text exactly", (t) => {
  const url = `https://files.example.test/file?token=${"aB09%2F".repeat(260)}&sig=123`;
  const text = `\uFEFFResult: 385. Image: \u{1F535}\n[Download](${url})\n`;
  const { workspace, path, report } = responseFixture(t, text);
  for (const response_file of [report.response_file, path]) {
    const prepared = prepareReport({ ...report, response_file }, workspace);
    assert.equal(prepared.response, text);
    assert.equal(Object.hasOwn(prepared, "response_file"), false);
    assert.deepEqual(validateReport(prepared), prepared);
  }
});

test("response input requires exactly one field in schema and native validation", (t) => {
  const { workspace, report } = responseFixture(t);
  assert.deepEqual(FINISH_PARAMETERS.required, ["status", "summary"]);
  assert.deepEqual(FINISH_PARAMETERS.oneOf, [
    { required: ["response"] },
    { required: ["response_file"] },
  ]);
  assert.throws(
    () => prepareReport({ ...report, response: "Answer" }, workspace),
    /exactly one/i,
  );
  const { response_file, ...missing } = report;
  assert.throws(() => prepareReport(missing, workspace), /response/i);
  assert.deepEqual(prepareReport(REPORT, workspace), REPORT);
  assert.throws(() => validateReport(report), /response/i);
});

test("response_file rejects traversal, links, and non-regular files", (t) => {
  const { workspace, path, report } = responseFixture(t);
  const outside = join(workspace, "outside.txt");
  writeFileSync(outside, "Outside");
  symlinkSync(outside, join(workspace, "output", "linked.txt"));
  mkdirSync(join(workspace, "real"));
  writeFileSync(join(workspace, "real", "response.txt"), "Outside");
  symlinkSync(join(workspace, "real"), join(workspace, "output", "linked-dir"));
  linkSync(path, join(workspace, "output", "hardlink.txt"));
  execFileSync("mkfifo", [join(workspace, "output", "pipe")]);
  for (const response_file of [
    outside,
    "output/../outside.txt",
    "output/../output/response.txt",
    "output/linked.txt",
    "output/linked-dir/response.txt",
    "output/hardlink.txt",
    "output/response.txt",
    "output/pipe",
    "output",
    "output/missing.txt",
    "output/response.txt\0",
  ]) {
    assert.throws(
      () => prepareReport({ ...report, response_file }, workspace),
      /file|path|output/i,
      response_file,
    );
  }
});

test("response_file rejects invalid UTF-8, blank text, and size overflow", (t) => {
  const { workspace, path, report } = responseFixture(t);
  for (const [contents, error] of [
    [Buffer.from([0xc3, 0x28]), /UTF-8/i],
    [" \n\t", /response/i],
    ["x".repeat(MAX_REPORT_BYTES + 1), /1 MiB/i],
    ["x".repeat(MAX_REPORT_BYTES), /1 MiB/i],
  ]) {
    writeFileSync(path, contents);
    assert.throws(() => prepareReport(report, workspace), error);
  }
});

test("native prepare and direct execute accept response_file and return canonical response", async (t) => {
  const { workspace, report } = responseFixture(t);
  const previous = process.cwd();
  process.chdir(workspace);
  try {
    for (const prepare of [true, false]) {
      const { tool, standalone } = extension();
      standalone();
      const params = prepare ? tool.prepareArguments(report) : report;
      const result = await tool.execute(CALL_ID, params);
      assert.equal(result.terminate, true);
      assert.equal(result.details.report.response, "Answer: 42.\n");
      assert.equal(
        Object.hasOwn(result.details.report, "response_file"),
        false,
      );
      assert.deepEqual(
        validateReport(result.details.report),
        result.details.report,
      );
    }
  } finally {
    process.chdir(previous);
  }
});

test("standalone finish returns the full report and native termination", async () => {
  const { tool, standalone } = extension();
  standalone();
  const result = await tool.execute(CALL_ID, REPORT);
  assert.equal(tool.executionMode, "sequential");
  assert.deepEqual(result.details, { report: REPORT });
  assert.equal(result.terminate, true);
  assert.equal(result.isError, undefined);
  await assert.rejects(tool.execute(CALL_ID, REPORT), /already/i);
});

test("blocked completion preserves its summary and partial JSON result", async () => {
  const { tool, standalone } = extension();
  standalone();
  const report = {
    status: "blocked",
    summary: "Missing repository permission.",
    response: "Repository permission is required to complete this task.",
    result: { partial: [null, false, 3, "partial"] },
  };
  assert.deepEqual((await tool.execute(CALL_ID, report)).details, { report });
});

test("finish preserves response text and rejects empty responses", () => {
  const response =
    "Answer: 42. Download: https://example.test/output?a=1&b=2\n";
  assert.equal(validateReport({ ...REPORT, response }).response, response);
  for (const invalid of ["", " \n\t", null, 42, {}]) {
    assert.throws(
      () => validateReport({ ...REPORT, response: invalid }),
      /response/i,
    );
  }
});

test("finish text schemas support multiline answers without provider regex support", () => {
  const text = "The answer is 42.\nFile: https://example.test/output\n";
  for (const field of ["summary", "response"]) {
    const schema = FINISH_PARAMETERS.properties[field];
    assert.equal(
      Object.hasOwn(schema, "pattern"),
      false,
      `${field} requires provider regex support`,
    );
    assert.equal(schema.type, "string");
    assert.equal(schema.minLength, 1);
    if (field === "summary")
      assert.ok(FINISH_PARAMETERS.required.includes(field));
    assert.equal(validateReport({ ...REPORT, [field]: text })[field], text);
    assert.throws(
      () => validateReport({ ...REPORT, [field]: " \n\t\u0085" }),
      /summary|response/i,
    );
  }
});

test("finish rejects a missing response before accepting termination", async () => {
  const { tool, standalone } = extension();
  standalone();
  const report = { ...REPORT };
  delete report.response;
  assert.throws(() => tool.prepareArguments(report), /response/i);
  await assert.rejects(tool.execute(CALL_ID, report), /response/i);
  const corrected = { ...report, response: "The answer is 42." };
  assert.equal((await tool.execute(CALL_ID, corrected)).terminate, true);
});

test("finish rejects a stringified result and permits an object retry", async () => {
  const { tool, standalone } = extension();
  standalone();
  // A cloud run returned JSON text; reject it before accepting completion.
  const report = { ...REPORT, result: JSON.stringify(REPORT.result) };
  assert.throws(() => tool.prepareArguments(report), /result.*object/i);
  await assert.rejects(tool.execute(CALL_ID, report), /result.*object/i);
  assert.equal((await tool.execute(CALL_ID, REPORT)).terminate, true);
});

test("report size limit accepts a full report and rejects overflow", () => {
  const report = {
    status: "completed",
    summary: "Done.",
    response: "The work is complete.",
    result: { text: "" },
  };
  const overhead = Buffer.byteLength(JSON.stringify(report), "utf8");
  report.result.text = "x".repeat(MAX_REPORT_BYTES - overhead);
  assert.equal(
    Buffer.byteLength(JSON.stringify(validateReport(report)), "utf8"),
    MAX_REPORT_BYTES,
  );
  assert.throws(
    () =>
      validateReport({ ...report, result: { text: report.result.text + "x" } }),
    /1 MiB/,
  );
});

test("finish rejects mixed tool batches so sibling side effects can complete", async () => {
  for (const finishFirst of [true, false]) {
    const { tool, message, standalone } = extension();
    const finish = { type: "toolCall", name: FINISH_TOOL_NAME, id: CALL_ID };
    const sibling = { type: "toolCall", name: "bash", id: "side-effect" };
    message(finishFirst ? [finish, sibling] : [sibling, finish]);
    await assert.rejects(tool.execute(CALL_ID, REPORT), /alone/i);
    standalone();
    assert.equal((await tool.execute(CALL_ID, REPORT)).terminate, true);
  }
});

test("compaction remains enabled until a report is accepted", async () => {
  const { tool, handlers, standalone } = extension();
  const compact = handlers.get("session_before_compact");
  assert.equal(compact({}), undefined);
  standalone();
  await assert.rejects(tool.execute(CALL_ID, { ...REPORT, summary: "" }));
  assert.equal(compact({}), undefined);
  await tool.execute(CALL_ID, REPORT);
  assert.deepEqual(compact({}), { cancel: true });
});
