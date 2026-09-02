import assert from "node:assert/strict";
import test from "node:test";

import registerFinish, {
  FINISH_TOOL_NAME,
  MAX_REPORT_BYTES,
  validateReport,
} from "../worker/finish.mjs";

const CALL_ID = "finish-call";
const REPORT = {
  status: "completed",
  summary: "The requested work is complete.",
  result: { answer: 42 },
};

function extension() {
  const handlers = new Map();
  let tool;
  registerFinish({
    on(name, handler) {
      handlers.set(name, handler);
    },
    registerTool(definition) {
      tool = definition;
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
  return { handlers, tool, message, standalone };
}

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
    result: { partial: [null, false, 3, "partial"] },
  };
  assert.deepEqual((await tool.execute(CALL_ID, report)).details, { report });
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
