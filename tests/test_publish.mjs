import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";

import registerPublish, { PUBLISH_SOCKET_ENV } from "../worker/publish.mjs";

const ARTIFACT = {
  name: "report.csv",
  key: "runs/test/artifacts/file/report.csv",
  bytes: 12,
  content_type: "text/csv",
  sha256: "a".repeat(64),
  url: `https://example.test/report.csv?X-Amz-Security-Token=${encodeURIComponent("Ab+/0123456789".repeat(80))}&X-Amz-Signature=test`,
  expires_at: "2026-09-04T00:00:00+00:00",
};

async function publicationTool(context) {
  const directory = await mkdtemp(join(tmpdir(), "cloudbox-receipt-test-"));
  context.after(() => rm(directory, { recursive: true, force: true }));
  const socketPath = join(directory, "service.sock");
  const server = createServer((socket) => {
    socket.once("data", () =>
      socket.end(JSON.stringify({ artifact: ARTIFACT }) + "\n"),
    );
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  context.after(() => new Promise((resolve) => server.close(resolve)));
  const previous = process.env[PUBLISH_SOCKET_ENV];
  process.env[PUBLISH_SOCKET_ENV] = socketPath;
  context.after(() => {
    if (previous === undefined) delete process.env[PUBLISH_SOCKET_ENV];
    else process.env[PUBLISH_SOCKET_ENV] = previous;
  });
  let tool;
  registerPublish({
    registerTool(value) {
      tool = value;
    },
  });
  return { directory, tool };
}

test("publish_file saves exact URL data in a private receipt", async (context) => {
  const { tool } = await publicationTool(context);
  const result = await tool.execute("publish", { path: "output/report.csv" });
  const content = JSON.parse(result.content[0].text);
  assert.equal(typeof content.receipt_path, "string");
  context.after(() =>
    rm(dirname(content.receipt_path), { recursive: true, force: true }),
  );
  assert.deepEqual(result.details.artifact, ARTIFACT);
  assert.equal(result.details.receipt_path, content.receipt_path);
  const { receipt_path: receiptPath, ...artifact } = content;
  assert.deepEqual(artifact, ARTIFACT);
  const receipt = JSON.parse(await readFile(receiptPath, "utf8"));
  assert.deepEqual(receipt, ARTIFACT);
  assert.equal((await stat(receiptPath)).mode & 0o777, 0o600);
  assert.equal((await stat(dirname(receiptPath))).mode & 0o777, 0o700);
});

test("receipt write failure permits retry and preserves artifact data", async (context) => {
  const { directory, tool } = await publicationTool(context);
  const invalidTemp = join(directory, "not-a-directory");
  await writeFile(invalidTemp, "occupied");
  const previous = process.env.TMPDIR;
  try {
    process.env.TMPDIR = invalidTemp;
    await assert.rejects(
      tool.execute("publish-fail", { path: "output/report.csv" }),
      /uploaded.*receipt.*retry/i,
    );
  } finally {
    if (previous === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = previous;
  }
  const result = await tool.execute("publish-retry", {
    path: "output/report.csv",
  });
  const receiptPath = result.details.receipt_path;
  context.after(() =>
    rm(dirname(receiptPath), { recursive: true, force: true }),
  );
  assert.deepEqual(result.details.artifact, ARTIFACT);
  assert.deepEqual(JSON.parse(await readFile(receiptPath, "utf8")), ARTIFACT);
});
