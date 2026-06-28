import { useEffect, useRef } from "react";

type PointCloudPreviewProps = {
  id: string;
  className?: string;
};

function hashSeed(value: string) {
  let hash = 2166136261;

  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }

  return hash >>> 0;
}

function createPrng(seed: string) {
  let state = hashSeed(seed) || 1;

  return () => {
    state = Math.imul(state ^ (state >>> 15), 1 | state);
    state ^= state + Math.imul(state ^ (state >>> 7), 61 | state);

    return ((state ^ (state >>> 14)) >>> 0) / 4294967296;
  };
}

function drawPointCloud(canvas: HTMLCanvasElement, id: string) {
  if (typeof navigator !== "undefined" && navigator.userAgent.includes("jsdom")) {
    return;
  }

  const context = canvas.getContext("2d");

  if (!context) {
    return;
  }

  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.floor(rect.width || canvas.clientWidth || 320));
  const height = Math.max(1, Math.floor(rect.height || canvas.clientHeight || 180));
  const pixelRatio = window.devicePixelRatio || 1;

  canvas.width = Math.floor(width * pixelRatio);
  canvas.height = Math.floor(height * pixelRatio);
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, width, height);

  const gradient = context.createLinearGradient(0, 0, width, height);
  gradient.addColorStop(0, "rgba(21, 209, 216, 0.2)");
  gradient.addColorStop(0.55, "rgba(52, 211, 153, 0.08)");
  gradient.addColorStop(1, "rgba(8, 17, 31, 0.9)");
  context.fillStyle = gradient;
  context.fillRect(0, 0, width, height);

  context.strokeStyle = "rgba(148, 163, 184, 0.16)";
  context.lineWidth = 1;
  for (let y = 24; y < height; y += 24) {
    context.beginPath();
    context.moveTo(0, y);
    context.lineTo(width, y);
    context.stroke();
  }
  for (let x = 32; x < width; x += 32) {
    context.beginPath();
    context.moveTo(x, 0);
    context.lineTo(x, height);
    context.stroke();
  }

  const random = createPrng(id);
  const cx = width * 0.52;
  const cy = height * 0.54;
  const radius = Math.min(width, height) * 0.38;

  for (let index = 0; index < 180; index += 1) {
    const angle = random() * Math.PI * 2;
    const depth = random();
    const spread = radius * (0.22 + random() * 0.92);
    const x = cx + Math.cos(angle) * spread * (0.8 + depth * 0.5);
    const y = cy + Math.sin(angle) * spread * 0.52 - depth * 28;
    const size = 1.1 + depth * 2.4;
    const alpha = 0.28 + depth * 0.62;

    context.beginPath();
    context.fillStyle = index % 5 === 0 ? `rgba(251, 191, 36, ${alpha})` : `rgba(21, 209, 216, ${alpha})`;
    context.arc(x, y, size, 0, Math.PI * 2);
    context.fill();
  }

  context.strokeStyle = "rgba(21, 209, 216, 0.34)";
  context.lineWidth = 1.5;
  context.beginPath();
  context.ellipse(cx, cy + 5, radius * 1.1, radius * 0.38, -0.08, 0, Math.PI * 2);
  context.stroke();
}

export function PointCloudPreview({ id, className }: PointCloudPreviewProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;

    if (!canvas) {
      return undefined;
    }

    const redraw = () => {
      drawPointCloud(canvas, id);
    };

    redraw();
    window.addEventListener("resize", redraw);

    return () => {
      window.removeEventListener("resize", redraw);
    };
  }, [id]);

  return <canvas ref={canvasRef} aria-hidden="true" className={className} />;
}
