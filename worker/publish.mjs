import { mkdtemp, writeFile } from "node:fs/promises";
import { createConnection } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const PUBLISH_TOOL_NAME = "publish_file";
export const PUBLISH_SOCKET_ENV = "CLOUDBOX_PUBLISH_SOCKET";
export const MAX_PATH_BYTES = 4096;
export const MAX_REPLY_BYTES = 64 * 1024;
export const REQUEST_TIMEOUT_MS = 20_000;
const RECEIPT_DIRECTORY_PREFIX = "cloudbox-publish-receipt-";
const RECEIPT_FILENAME = "receipt.json";
const RECEIPT_FILE_MODE = 0o600;
export const PUBLISH_PARAMETERS = {
  type: "object",
  properties: {
    path: {
      type: "string",
      minLength: 1,
      maxLength: MAX_PATH_BYTES,
      description:
        "Workspace-relative output/file.ext, or an absolute path inside output/.",
    },
  },
  required: ["path"],
  additionalProperties: false,
};

export function validatePublishArguments(params) {
  if (
    params === null ||
    typeof params !== "object" ||
    Array.isArray(params) ||
    Reflect.ownKeys(params).length !== 1 ||
    !Object.hasOwn(params, "path") ||
    typeof params.path !== "string" ||
    !params.path ||
    params.path.includes("\0") ||
    Buffer.byteLength(params.path, "utf8") > MAX_PATH_BYTES
  ) {
    throw new Error("Pass a file path inside output/.");
  }
  return { path: params.path };
}

export async function requestPublication(
  params,
  signal,
  socketPath = process.env[PUBLISH_SOCKET_ENV],
) {
  const request = validatePublishArguments(params);
  if (!socketPath)
    throw new Error("File publication is not available in this run.");
  if (signal?.aborted) throw new Error("File publication was cancelled.");
  return new Promise((resolve, reject) => {
    const socket = createConnection({ path: socketPath });
    const chunks = [];
    let length = 0;
    let settled = false;
    function finish(error, artifact) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      socket.destroy();
      if (error) reject(error);
      else resolve(artifact);
    }
    function abort() {
      finish(new Error("File publication was cancelled."));
    }
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(() => {
      finish(
        new Error("File publication timed out. Check the output and retry."),
      );
    }, REQUEST_TIMEOUT_MS);
    socket.on("error", () => {
      finish(new Error("Cannot contact the file publication service."));
    });
    socket.on("connect", () => {
      socket.write(JSON.stringify(request) + "\n");
    });
    socket.on("data", (chunk) => {
      length += chunk.length;
      if (length > MAX_REPLY_BYTES) {
        finish(new Error("The file publication reply is too large."));
        return;
      }
      chunks.push(chunk);
      const data = Buffer.concat(chunks);
      const newline = data.indexOf("\n");
      if (newline < 0) return;
      try {
        const reply = JSON.parse(data.subarray(0, newline).toString("utf8"));
        if (typeof reply.error === "string") {
          finish(new Error(reply.error));
        } else if (reply.artifact && typeof reply.artifact.url === "string") {
          finish(null, reply.artifact);
        } else {
          finish(new Error("The file publication reply is invalid."));
        }
      } catch {
        finish(new Error("The file publication reply is invalid."));
      }
    });
    socket.on("end", () => {
      finish(new Error("The file publication service closed without a reply."));
    });
  });
}

async function writeReceipt(artifact) {
  try {
    // Preserve URL bytes as local data; the agent can compose its answer in code.
    const directory = await mkdtemp(join(tmpdir(), RECEIPT_DIRECTORY_PREFIX));
    const path = join(directory, RECEIPT_FILENAME);
    await writeFile(path, JSON.stringify(artifact) + "\n", {
      encoding: "utf8",
      flag: "wx",
      mode: RECEIPT_FILE_MODE,
    });
    return path;
  } catch {
    throw new Error(
      "File uploaded, but its local receipt could not be saved. Check local storage and retry publish_file.",
    );
  }
}

export default function registerPublish(pi) {
  pi.registerTool({
    name: PUBLISH_TOOL_NAME,
    label: "Publish file",
    description:
      "Upload one output/ file and return its download URL and receipt_path to exact JSON metadata. Accepts output/file.ext or an absolute path inside output/. Limits: 32 files, 32 MiB each, 128 MiB total. Close files before publication; links expire.",
    promptSnippet: "Publish an output file and receive its download URL",
    promptGuidelines: [
      "Write user files in output/ and publish them before finish. ZIP directories first.",
      "Read receipt_path as JSON in code. Copy its URL into an output/ response file with the full answer; do not retype signed URLs. Call finish with response_file. This internal response file does not need publication.",
    ],
    parameters: PUBLISH_PARAMETERS,
    prepareArguments: validatePublishArguments,
    executionMode: "sequential",
    async execute(_toolCallId, params, signal) {
      const artifact = await requestPublication(params, signal);
      const receiptPath = await writeReceipt(artifact);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify({ ...artifact, receipt_path: receiptPath }),
          },
        ],
        details: { artifact, receipt_path: receiptPath },
      };
    },
  });
}
