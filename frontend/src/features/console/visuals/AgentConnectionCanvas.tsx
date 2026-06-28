import { useEffect, useRef } from "react";

type CanvasNode = {
  id: string;
  x: number;
  y: number;
};

type AgentConnectionCanvasProps = {
  nodes: CanvasNode[];
  connections: ReadonlyArray<readonly [string, string]>;
  className?: string;
};

export function AgentConnectionCanvas({ nodes, connections, className }: AgentConnectionCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || /jsdom/i.test(window.navigator.userAgent)) {
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

    const nodeMap = new Map(nodes.map((node) => [node.id, node]));

    const draw = () => {
      const rect = canvas.getBoundingClientRect();
      const width = rect.width || canvas.offsetWidth;
      const height = rect.height || canvas.offsetHeight;
      const ratio = window.devicePixelRatio || 1;

      canvas.width = Math.max(1, Math.floor(width * ratio));
      canvas.height = Math.max(1, Math.floor(height * ratio));
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);

      context.lineWidth = 2;
      context.lineCap = "round";

      connections.forEach(([fromId, toId]) => {
        const from = nodeMap.get(fromId);
        const to = nodeMap.get(toId);

        if (!from || !to) {
          return;
        }

        const startX = (from.x / 100) * width;
        const startY = (from.y / 100) * height;
        const endX = (to.x / 100) * width;
        const endY = (to.y / 100) * height;
        const controlOffset = Math.max(52, Math.abs(endX - startX) * 0.45);
        const controlX1 = startX + controlOffset;
        const controlY1 = startY;
        const controlX2 = endX - controlOffset;
        const controlY2 = endY;
        const gradient = context.createLinearGradient(startX, startY, endX, endY);

        gradient.addColorStop(0, "rgba(21, 209, 216, 0.78)");
        gradient.addColorStop(0.55, "rgba(52, 211, 153, 0.42)");
        gradient.addColorStop(1, "rgba(167, 139, 250, 0.62)");

        context.strokeStyle = gradient;
        context.beginPath();
        context.moveTo(startX, startY);
        context.bezierCurveTo(controlX1, controlY1, controlX2, controlY2, endX, endY);
        context.stroke();

        context.fillStyle = "rgba(21, 209, 216, 0.85)";
        context.beginPath();
        context.arc(endX, endY, 3.2, 0, Math.PI * 2);
        context.fill();
      });
    };

    draw();
    window.addEventListener("resize", draw);

    return () => {
      window.removeEventListener("resize", draw);
    };
  }, [connections, nodes]);

  return <canvas ref={canvasRef} aria-hidden="true" className={className} />;
}
