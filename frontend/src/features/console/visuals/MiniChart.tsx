import { cn } from "../../../lib/utils";

type DonutDatum = {
  label: string;
  value: number;
  color: string;
};

type SeriesChart = {
  labels: string[];
  data: Array<number | null>;
  label: string;
  color: string;
};

type DonutChartProps = {
  type: "donut";
  title: string;
  data: DonutDatum[];
  className?: string;
};

type LineChartProps = {
  type: "line";
  title: string;
  data: SeriesChart;
  className?: string;
};

type BarChartProps = {
  type: "bar";
  title: string;
  data: SeriesChart;
  className?: string;
};

type MiniChartProps = DonutChartProps | LineChartProps | BarChartProps;

const SVG_WIDTH = 320;
const SVG_HEIGHT = 180;
const PADDING = 24;

function polarToCartesian(cx: number, cy: number, radius: number, angle: number) {
  const radians = (angle - 90) * (Math.PI / 180);

  return {
    x: cx + radius * Math.cos(radians),
    y: cy + radius * Math.sin(radians),
  };
}

function describeArc(cx: number, cy: number, radius: number, startAngle: number, endAngle: number) {
  const start = polarToCartesian(cx, cy, radius, endAngle);
  const end = polarToCartesian(cx, cy, radius, startAngle);
  const largeArcFlag = endAngle - startAngle <= 180 ? "0" : "1";

  return ["M", start.x, start.y, "A", radius, radius, 0, largeArcFlag, 0, end.x, end.y].join(" ");
}

function chartScale(values: Array<number | null>) {
  const validValues = values.filter((value): value is number => value !== null);
  const min = Math.min(...validValues);
  const max = Math.max(...validValues);
  const span = max - min || 1;

  return { min, max, span };
}

function toLinePath(data: SeriesChart) {
  const { min, span } = chartScale(data.data);
  const step = (SVG_WIDTH - PADDING * 2) / Math.max(data.data.length - 1, 1);

  return data.data
    .map((value, index) => {
      if (value === null) {
        return "";
      }

      const x = PADDING + index * step;
      const y = SVG_HEIGHT - PADDING - ((value - min) / span) * (SVG_HEIGHT - PADDING * 2);

      return `${index === 0 || data.data[index - 1] === null ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .filter(Boolean)
    .join(" ");
}

function DonutChart({ title, data, className }: DonutChartProps) {
  const total = data.reduce((sum, item) => sum + item.value, 0);
  let cursor = 0;

  return (
    <div className={cn("grid gap-4 sm:grid-cols-[12rem_1fr] sm:items-center", className)}>
      <svg role="img" aria-label={title} viewBox="0 0 180 180" className="mx-auto h-44 w-44">
        <circle cx="90" cy="90" r="58" fill="none" stroke="rgba(148,163,184,0.16)" strokeWidth="24" />
        {data.map((item) => {
          const start = cursor;
          const angle = (item.value / total) * 360;
          const end = start + angle;
          cursor = end;

          return (
            <path
              key={item.label}
              d={describeArc(90, 90, 58, start, end)}
              fill="none"
              stroke={item.color}
              strokeLinecap="round"
              strokeWidth="24"
            />
          );
        })}
        <text x="90" y="84" textAnchor="middle" className="fill-console-text text-lg font-semibold">
          {total}%
        </text>
        <text x="90" y="105" textAnchor="middle" className="fill-console-muted text-[10px]">
          distribution
        </text>
      </svg>

      <div className="space-y-2">
        {data.map((item) => (
          <div key={item.label} className="flex items-center justify-between gap-3 text-sm">
            <span className="flex min-w-0 items-center gap-2 text-console-muted">
              <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: item.color }} />
              <span className="truncate">{item.label}</span>
            </span>
            <span className="font-medium text-console-text">{item.value}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function LineChart({ title, data, className }: LineChartProps) {
  const path = toLinePath(data);
  const { min, span } = chartScale(data.data);
  const step = (SVG_WIDTH - PADDING * 2) / Math.max(data.data.length - 1, 1);

  return (
    <div className={cn("space-y-3", className)}>
      <p className="text-sm font-medium text-console-text">{data.label}</p>
      <svg role="img" aria-label={title} viewBox={`0 0 ${SVG_WIDTH} ${SVG_HEIGHT}`} className="h-48 w-full overflow-visible">
        {[0, 1, 2].map((line) => {
          const y = PADDING + line * ((SVG_HEIGHT - PADDING * 2) / 2);

          return <line key={line} x1={PADDING} x2={SVG_WIDTH - PADDING} y1={y} y2={y} stroke="rgba(148,163,184,0.16)" />;
        })}
        <path d={path} fill="none" stroke={data.color} strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" />
        {data.data.map((value, index) => {
          if (value === null) {
            return null;
          }

          const x = PADDING + index * step;
          const y = SVG_HEIGHT - PADDING - ((value - min) / span) * (SVG_HEIGHT - PADDING * 2);

          return <circle key={`${data.labels[index]}-${value}`} cx={x} cy={y} r="3.5" fill={data.color} stroke="#08111f" strokeWidth="2" />;
        })}
        {data.labels.map((label, index) => (
          <text key={label} x={PADDING + index * step} y={SVG_HEIGHT - 6} textAnchor="middle" className="fill-console-muted text-[9px]">
            {label}
          </text>
        ))}
      </svg>
    </div>
  );
}

function BarChart({ title, data, className }: BarChartProps) {
  const { min, span } = chartScale(data.data);
  const barWidth = (SVG_WIDTH - PADDING * 2) / data.data.length - 8;

  return (
    <div className={cn("space-y-3", className)}>
      <p className="text-sm font-medium text-console-text">{data.label}</p>
      <svg role="img" aria-label={title} viewBox={`0 0 ${SVG_WIDTH} ${SVG_HEIGHT}`} className="h-48 w-full">
        {data.data.map((value, index) => {
          const x = PADDING + index * (barWidth + 8);
          const normalized = value === null ? 0 : (value - min) / span;
          const height = 18 + normalized * (SVG_HEIGHT - PADDING * 2 - 18);
          const y = SVG_HEIGHT - PADDING - height;

          return <rect key={data.labels[index]} x={x} y={y} width={barWidth} height={height} rx="4" fill={data.color} opacity={value === null ? 0.2 : 0.85} />;
        })}
      </svg>
    </div>
  );
}

export function MiniChart(props: MiniChartProps) {
  if (props.type === "donut") {
    return <DonutChart {...props} />;
  }

  if (props.type === "line") {
    return <LineChart {...props} />;
  }

  return <BarChart {...props} />;
}
