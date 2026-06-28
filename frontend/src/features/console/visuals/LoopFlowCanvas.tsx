import { Bot, CheckCircle2, Database, Filter, Rocket } from "lucide-react";
import { useEffect, useRef } from "react";

const flowNodes = [
  { label: "数据采集", icon: Database, x: 50, y: 18 },
  { label: "自动标注", icon: Bot, x: 82, y: 45 },
  { label: "质量过滤", icon: Filter, x: 68, y: 82 },
  { label: "模型训练", icon: Rocket, x: 32, y: 82 },
  { label: "部署验证", icon: CheckCircle2, x: 18, y: 45 },
];

function drawLoop(context: CanvasRenderingContext2D, width: number, height: number, progress: number) {
  context.clearRect(0, 0, width, height);
  context.lineWidth = 1.4;
  context.strokeStyle = "rgba(21, 209, 216, 0.42)";
  context.setLineDash([8, 8]);
  context.lineDashOffset = -progress * 28;

  const points = flowNodes.map((node) => ({
    x: (node.x / 100) * width,
    y: (node.y / 100) * height,
  }));

  context.beginPath();
  points.forEach((point, index) => {
    if (index === 0) {
      context.moveTo(point.x, point.y);
    } else {
      context.lineTo(point.x, point.y);
    }
  });
  context.closePath();
  context.stroke();
  context.setLineDash([]);

  points.forEach((point, index) => {
    const next = points[(index + 1) % points.length];
    const local = (progress + index / points.length) % 1;
    const dotX = point.x + (next.x - point.x) * local;
    const dotY = point.y + (next.y - point.y) * local;

    context.beginPath();
    context.fillStyle = "rgba(0, 229, 155, 0.95)";
    context.shadowColor = "rgba(0, 229, 155, 0.55)";
    context.shadowBlur = 12;
    context.arc(dotX, dotY, 3.5, 0, Math.PI * 2);
    context.fill();
    context.shadowBlur = 0;
  });
}

export function LoopFlowCanvas() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;

    if (!canvas || typeof window.requestAnimationFrame !== "function" || /jsdom/i.test(window.navigator.userAgent)) {
      return undefined;
    }

    let context: CanvasRenderingContext2D | null = null;

    try {
      context = canvas.getContext("2d");
    } catch {
      return undefined;
    }

    if (!context) {
      return undefined;
    }

    let frameId = 0;
    let width = 0;
    let height = 0;

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const scale = window.devicePixelRatio || 1;
      width = Math.max(320, rect.width);
      height = Math.max(260, rect.height);

      canvas.width = Math.floor(width * scale);
      canvas.height = Math.floor(height * scale);
      context.setTransform(scale, 0, 0, scale, 0, 0);
    };

    const draw = (time: number) => {
      drawLoop(context, width, height, (time / 2600) % 1);
      frameId = window.requestAnimationFrame(draw);
    };

    resize();
    frameId = window.requestAnimationFrame(draw);
    window.addEventListener("resize", resize);

    return () => {
      window.removeEventListener("resize", resize);
      window.cancelAnimationFrame(frameId);
    };
  }, []);

  return (
    <div className="relative min-h-[20rem] overflow-hidden rounded border border-console-line bg-console-panel2/70">
      <canvas ref={canvasRef} aria-hidden="true" className="absolute inset-0 h-full w-full" />
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_center,rgba(21,209,216,0.12),transparent_56%)]" />
      {flowNodes.map((node) => {
        const Icon = node.icon;

        return (
          <div
            key={node.label}
            className="absolute flex w-28 -translate-x-1/2 -translate-y-1/2 flex-col items-center gap-2 rounded border border-console-cyan/30 bg-console-panel/92 px-3 py-3 text-center shadow-[0_14px_36px_rgba(0,0,0,0.24)]"
            style={{ left: `${node.x}%`, top: `${node.y}%` }}
          >
            <span className="flex h-9 w-9 items-center justify-center rounded border border-console-cyan/35 bg-console-cyan/10 text-console-cyan">
              <Icon aria-hidden="true" className="h-4 w-4" />
            </span>
            <span className="text-xs font-medium text-console-text">{node.label}</span>
          </div>
        );
      })}
    </div>
  );
}
