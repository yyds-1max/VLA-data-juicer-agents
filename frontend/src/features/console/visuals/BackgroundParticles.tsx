import { useEffect, useRef } from "react";

type Particle = {
  x: number;
  y: number;
  radius: number;
  speedX: number;
  speedY: number;
  alpha: number;
};

function createParticles(width: number, height: number): Particle[] {
  const count = Math.min(56, Math.max(18, Math.floor((width * height) / 36000)));

  return Array.from({ length: count }, () => ({
    x: Math.random() * width,
    y: Math.random() * height,
    radius: 0.8 + Math.random() * 1.8,
    speedX: (Math.random() - 0.5) * 0.18,
    speedY: 0.08 + Math.random() * 0.2,
    alpha: 0.12 + Math.random() * 0.32,
  }));
}

export function BackgroundParticles() {
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
    let particles: Particle[] = [];

    const resize = () => {
      const scale = window.devicePixelRatio || 1;
      const width = window.innerWidth;
      const height = window.innerHeight;

      canvas.width = Math.floor(width * scale);
      canvas.height = Math.floor(height * scale);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      context.setTransform(scale, 0, 0, scale, 0, 0);
      particles = createParticles(width, height);
    };

    const draw = () => {
      const width = window.innerWidth;
      const height = window.innerHeight;

      context.clearRect(0, 0, width, height);

      for (const particle of particles) {
        particle.x += particle.speedX;
        particle.y += particle.speedY;

        if (particle.y > height + 8) {
          particle.y = -8;
          particle.x = Math.random() * width;
        }

        if (particle.x < -8) {
          particle.x = width + 8;
        } else if (particle.x > width + 8) {
          particle.x = -8;
        }

        context.beginPath();
        context.fillStyle = `rgba(21, 209, 216, ${particle.alpha})`;
        context.arc(particle.x, particle.y, particle.radius, 0, Math.PI * 2);
        context.fill();
      }

      frameId = window.requestAnimationFrame(draw);
    };

    resize();
    draw();
    window.addEventListener("resize", resize);

    return () => {
      window.removeEventListener("resize", resize);
      window.cancelAnimationFrame(frameId);
    };
  }, []);

  return <canvas ref={canvasRef} aria-hidden="true" className="pointer-events-none fixed inset-0 z-0 opacity-70" />;
}
