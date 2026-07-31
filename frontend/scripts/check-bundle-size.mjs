import { readdir, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const MAX_JAVASCRIPT_CHUNK_BYTES = 500 * 1024;
const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const assetsDirectory = path.join(frontendRoot, "dist", "assets");

const assetNames = await readdir(assetsDirectory);
const javascriptChunks = await Promise.all(
  assetNames
    .filter((assetName) => assetName.endsWith(".js"))
    .map(async (assetName) => ({
      assetName,
      sizeBytes: (await stat(path.join(assetsDirectory, assetName))).size,
    })),
);

if (javascriptChunks.length === 0) {
  throw new Error(`No JavaScript chunks were found under ${assetsDirectory}.`);
}

javascriptChunks.sort((left, right) => right.sizeBytes - left.sizeBytes);
const oversizedChunks = javascriptChunks.filter(
  ({ sizeBytes }) => sizeBytes > MAX_JAVASCRIPT_CHUNK_BYTES,
);
const largestChunk = javascriptChunks[0];

console.log(
  `Bundle size gate: largest JavaScript chunk ${largestChunk.assetName} is ${largestChunk.sizeBytes} bytes; limit is ${MAX_JAVASCRIPT_CHUNK_BYTES} bytes.`,
);

if (oversizedChunks.length > 0) {
  for (const { assetName, sizeBytes } of oversizedChunks) {
    console.error(`Oversized JavaScript chunk: ${assetName} (${sizeBytes} bytes).`);
  }
  process.exitCode = 1;
}
