import assert from "node:assert/strict";
import test from "node:test";

import registerFinish, { FINISH_PARAMETERS, FINISH_TOOL_NAME, MAX_REPORT_BYTES, MAX_REPORT_DEPTH, validateReport } from "../worker/finish.mjs";

const CALL_ID = "finish-call";
const REPORT = { status: "completed", summary: "The requested work is complete.", result: { answer: 42 } };

function extension() {
  const handlers = new Map();
  let tool;
  registerFinish({
    on(name, handler) { handlers.set(name, handler); },
    registerTool(definition) { tool = definition; },
  });
  function message(calls) {
    handlers.get("message_end")({ message: { role: "assistant", content: calls } });
  }
  function standalone() {
    message([{ type: "toolCall", name: FINISH_TOOL_NAME, id: CALL_ID, arguments: REPORT }]);
  }
  return { handlers, tool, message, standalone };
}

test("tool schema requires status and summary and rejects extra fields", () => {
  assert.equal(FINISH_PARAMETERS.type, "object");
  assert.deepEqual(FINISH_PARAMETERS.required, ["status", "summary"]);
  assert.equal(FINISH_PARAMETERS.additionalProperties, false);
  assert.deepEqual(FINISH_PARAMETERS.properties.status.enum, ["completed", "blocked"]);
  assert.equal(FINISH_PARAMETERS.properties.result.type, "object");
  assert.equal(FINISH_PARAMETERS.properties.result.additionalProperties, true);
});

test("standalone finish returns the full report and native termination", async () => {
  const { tool, standalone } = extension();
  standalone();
  const result = await tool.execute(CALL_ID, REPORT);
  assert.equal(tool.executionMode, "sequential");
  assert.deepEqual(result.details, { report: REPORT });
  assert.notEqual(result.details.report, REPORT);
  assert.equal(result.terminate, true);
  assert.equal(result.isError, undefined);
});

test("blocked completion preserves its summary and partial JSON result", async () => {
  const { tool, standalone } = extension();
  standalone();
  const report = { status: "blocked", summary: "Missing repository permission.", result: { partial: [null, false, 3, "partial"] } };
  assert.deepEqual((await tool.execute(CALL_ID, report)).details, { report });
});

test("optional result object accepts nested values of every JSON type", () => {
  for (const value of [null, false, 0, 1.25, "text", [], {}, { nested: [true, { key: "value" }] }]) {
    const result = { value };
    assert.deepEqual(validateReport({ ...REPORT, result }).result, result);
  }
  assert.deepEqual(validateReport({ status: "completed", summary: "Done." }), { status: "completed", summary: "Done." });
});

test("finish rejects non-object results and permits an object retry", async () => {
  const { tool, standalone } = extension();
  standalone();
  for (const result of ["{\"answer\":42}", [], null, false, 0, 1.25, undefined, new Date()]) {
    assert.throws(() => tool.prepareArguments({ ...REPORT, result }), /result.*object/i);
    await assert.rejects(tool.execute(CALL_ID, { ...REPORT, result }), /result.*object/i);
  }
  assert.equal((await tool.execute(CALL_ID, REPORT)).terminate, true);
});

test("invalid top-level fields return errors that can be corrected", async () => {
  const invalid = [null, [], {}, { ...REPORT, status: "failed" }, { ...REPORT, summary: " \n\t" },
    { ...REPORT, summary: 42 }, { ...REPORT, extra: true }, { summary: "Done." }];
  for (const report of invalid) assert.throws(() => validateReport(report), Error);
  const { tool, standalone } = extension();
  standalone();
  await assert.rejects(tool.execute(CALL_ID, { ...REPORT, summary: "" }), /summary/i);
  assert.equal((await tool.execute(CALL_ID, REPORT)).terminate, true);
});

test("non-JSON result values are rejected without silently changing them", () => {
  const cyclic = {};
  cyclic.self = cyclic;
  for (const value of [NaN, Infinity, undefined, () => {}, Symbol("value"), 1n, new Date(), cyclic, [undefined], { value: NaN }]) {
    const result = { value };
    assert.throws(() => validateReport({ ...REPORT, result }), /JSON/i);
  }
});

test("UTF-8 validation accepts emoji and rejects lone surrogates", () => {
  assert.equal(validateReport({ ...REPORT, result: { value: "🧪" } }).result.value, "🧪");
  for (const value of ["\ud800", "\udc00", { ["\ud800"]: "value" }]) {
    const result = { value };
    assert.throws(() => validateReport({ ...REPORT, result }), /UTF-8/);
  }
});

test("depth limit counts the report object as depth zero", () => {
  let value = null;
  for (let depth = 2; depth < MAX_REPORT_DEPTH; depth++) value = [value];
  const result = { value };
  assert.deepEqual(validateReport({ ...REPORT, result }).result, result);
  assert.throws(() => validateReport({ ...REPORT, result: { value: [value] } }), /nesting/);
});

test("report size limit includes every field and uses UTF-8 bytes", () => {
  const report = { status: "completed", summary: "Done.", result: { text: "" } };
  const overhead = Buffer.byteLength(JSON.stringify(report), "utf8");
  report.result.text = "x".repeat(MAX_REPORT_BYTES - overhead);
  assert.equal(Buffer.byteLength(JSON.stringify(validateReport(report)), "utf8"), MAX_REPORT_BYTES);
  assert.throws(() => validateReport({ ...report, result: { text: report.result.text + "x" } }), /1 MiB/);
  assert.throws(() => validateReport({ ...report, result: { text: "🧪".repeat(MAX_REPORT_BYTES / 4) } }), /1 MiB/);
});

test("report size reserves Python exponent padding before finish succeeds", async () => {
  const numberCount = 180_000;
  const report = { ...REPORT, result: { values: Array(numberCount).fill(1e-7) } };
  const encodedBytes = Buffer.byteLength(JSON.stringify(report), "utf8");
  assert.ok(encodedBytes <= MAX_REPORT_BYTES);
  assert.ok(encodedBytes + numberCount > MAX_REPORT_BYTES);
  const { tool, standalone } = extension();
  standalone();
  await assert.rejects(tool.execute(CALL_ID, report), /1 MiB/);
  assert.equal((await tool.execute(CALL_ID, REPORT)).terminate, true);
});

test("summary rejects whitespace accepted by either runtime", () => {
  for (const summary of ["\u0085", "\u001c\u001d\u001e\u001f", " \u0085\n", "\ufeff"]) {
    assert.throws(() => validateReport({ ...REPORT, summary }), /summary/);
  }
  assert.equal(validateReport({ ...REPORT, summary: "\u0085Done.\u001c" }).summary, "\u0085Done.\u001c");
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

test("unmatched or cancelled calls cannot finish a run", async () => {
  const { tool, standalone } = extension();
  await assert.rejects(tool.execute(CALL_ID, REPORT), /alone/i);
  standalone();
  await assert.rejects(tool.execute("other-call", REPORT), /alone/i);
  await assert.rejects(tool.execute(CALL_ID, REPORT, AbortSignal.abort()), /cancelled/i);
  assert.equal((await tool.execute(CALL_ID, REPORT)).terminate, true);
  await assert.rejects(tool.execute(CALL_ID, REPORT), /already/i);
});

test("new assistant turns clear the prior standalone call", async () => {
  const { tool, handlers, standalone, message } = extension();
  standalone();
  handlers.get("turn_start")({});
  await assert.rejects(tool.execute(CALL_ID, REPORT), /alone/i);
  standalone();
  message([{ type: "text", text: "Final prose is not a finish call." }]);
  await assert.rejects(tool.execute(CALL_ID, REPORT), /alone/i);
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

test("argument preparation rejects types before provider coercion", () => {
  const { tool } = extension();
  assert.throws(() => tool.prepareArguments({ ...REPORT, summary: 42 }), /summary/);
  assert.deepEqual(tool.prepareArguments(REPORT), REPORT);
});
