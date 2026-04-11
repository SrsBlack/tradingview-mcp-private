// Quick MCP tool caller — usage: node mcp_call.js <tool_name> [json_args]
import { spawn } from "child_process";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const toolName = process.argv[2] || "quote_get";
const toolArgs = process.argv[3] ? JSON.parse(process.argv[3]) : {};

const child = spawn("node", [join(__dirname, "src/server.js")], {
  stdio: ["pipe", "pipe", "pipe"],
});

let buffer = "";
let responseCount = 0;

child.stdout.on("data", (data) => {
  buffer += data.toString();
  // Count complete JSON responses
  const lines = buffer.split("\n").filter((l) => l.trim());
  for (const line of lines) {
    try {
      const msg = JSON.parse(line);
      if (msg.id === 1) {
        // Init response received, send notification + tool call
        child.stdin.write(
          JSON.stringify({
            jsonrpc: "2.0",
            method: "notifications/initialized",
          }) + "\n"
        );
        child.stdin.write(
          JSON.stringify({
            jsonrpc: "2.0",
            id: 3,
            method: "tools/call",
            params: { name: toolName, arguments: toolArgs },
          }) + "\n"
        );
      }
      if (msg.id === 3) {
        // Tool response
        const content = msg.result?.content?.[0]?.text;
        if (content) {
          try {
            console.log(JSON.stringify(JSON.parse(content), null, 2));
          } catch {
            console.log(content);
          }
        } else {
          console.log(JSON.stringify(msg, null, 2));
        }
        child.kill();
        process.exit(0);
      }
    } catch {}
  }
  buffer = "";
});

child.stderr.on("data", () => {}); // suppress warnings

// Send init
child.stdin.write(
  JSON.stringify({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "cli", version: "1.0" },
    },
  }) + "\n"
);

setTimeout(() => {
  console.error("Timeout");
  child.kill();
  process.exit(1);
}, 15000);
