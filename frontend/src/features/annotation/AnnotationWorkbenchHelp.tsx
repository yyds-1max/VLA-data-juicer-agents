import {
  CircleHelp,
  Keyboard,
  LayoutPanelLeft,
  MousePointerClick,
  Workflow,
  X,
} from "lucide-react";

import { Button } from "../../components/ui/button";
import {
  Popover,
  PopoverClose,
  PopoverContent,
  PopoverTrigger,
} from "../../components/ui/popover";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/tabs";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "../../components/ui/tooltip";

const HELP_SECTIONS = [
  {
    value: "layout",
    label: "页面构成",
    icon: LayoutPanelLeft,
    title: "先认识四个工作区域",
    items: [
      ["首帧画布", "展示预处理、resize 后的 Segment 首帧；边界框和前景点都使用该图像的像素坐标。"],
      ["目标属性", "维护目标顺序、边界框、前景点，以及上衣、裤子和鞋子颜色。"],
      ["标注工具栏", "提供选择 / 调整、框选目标和前景点三种操作模式。"],
      ["Segment 状态刻度", "查看当前外层 clip 内各 Segment 的真实状态，并按 clip 内序号切换。"],
      ["保存与提交", "显示草稿保存状态；只有目标信息完整时才能提交首帧标注。"],
      ["页眉信息", "显示数据日期、当前 Segment、处理状态和所属外层 clip。"],
    ],
  },
  {
    value: "components",
    label: "组件功能",
    icon: MousePointerClick,
    title: "常用组件如何配合",
    items: [
      ["选择 / 调整", "选择目标后可拖动目标框、拖拽控制点改变大小；方向键可微调位置。"],
      ["框选目标", "在首帧中拖出新的目标框，完成后会自动转入前景点选择。"],
      ["前景点", "先选中目标，再点击目标内部清晰、稳定的位置。系统只校验图像边界，不会自动判断点是否位于目标框内。"],
      ["目标属性栏", "第一项固定作为 master，其余依次为 other1、other2；可调整顺序或删除目标。"],
      ["缩放与刻度", "缩放只改变查看比例，不改变坐标；刻度可点击、滚轮或使用两侧箭头切换。"],
      ["状态颜色", "紫色待标注、琥珀色草稿、绿色已提交/已标注、灰色已跳过，红色表示后处理失败。"],
    ],
  },
  {
    value: "workflow",
    label: "业务流程",
    icon: Workflow,
    title: "首帧标注的完成顺序",
    items: [
      ["1. 确认目标", "识别首帧中需要跟踪的行人目标，并确认 master 与 otherN 的顺序。"],
      ["2. 补全几何", "为每个目标提供 bbox 和前景点；前景点应落在目标可见区域内。"],
      ["3. 补全属性", "选择上衣、裤子和鞋子颜色。每个目标信息完整后才能提交。"],
      ["4. 保存并提交", "编辑会自动保存为草稿；提交成功后，当前 Segment 的首帧输入被固定。"],
      ["5. 交接自动处理", "全部 Segment 均已提交或跳过后，再由既有任务交接恢复 Tracking 和后处理。"],
      ["无有效目标", "首帧没有可用目标或图像不可用时，应使用页眉中的“跳过此 Segment”并说明原因。"],
    ],
  },
  {
    value: "operations",
    label: "操作",
    icon: Keyboard,
    title: "快捷操作与安全提示",
    items: [
      ["V / B / P", "切换选择调整、框选目标和前景点工具；前景点工具需要先选中目标。"],
      ["Esc", "退出框选或前景点专注模式，取消当前未完成的手势。"],
      ["方向键", "移动当前目标框 1 个坐标单位；按住 Shift 时每次移动 10 个单位。"],
      ["数字 + Enter", "输入当前外层 clip 内的 Segment 序号后按 Enter 可跳转；也可点击刻度或使用两侧箭头。"],
      ["自动保存", "修改约 700ms 后自动保存；切换 Segment 或离开页面前会等待当前草稿保存完成。"],
      ["图片校验", "首帧加载完成且实际尺寸与元数据一致后才允许编辑；不一致时会停止编辑。"],
      ["并发冲突", "几何数据不会自动合并。如检测到其他页面更新，请明确选择服务器版本或保留本地版本。"],
    ],
  },
] as const;

export function AnnotationWorkbenchHelp() {
  return (
    <Popover>
      <TooltipProvider delayDuration={260}>
        <Tooltip>
          <TooltipTrigger asChild>
            <PopoverTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="打开标注台帮助"
                className="size-8 shrink-0 rounded-full border border-transparent text-[#7b8494] transition-[color,background-color,border-color,box-shadow] duration-150 hover:border-[#dfe4ed] hover:bg-[#f4f6fa] hover:text-[#3156c8] active:bg-[#e8edf7] focus-visible:border-[#7f9ce1] focus-visible:ring-[#3156c8]/25 data-[state=open]:border-[#c8d5f2] data-[state=open]:bg-[#eef3ff] data-[state=open]:text-[#3156c8] motion-reduce:transition-none"
              >
                <CircleHelp className="size-[18px]" strokeWidth={1.8} aria-hidden="true" />
              </Button>
            </PopoverTrigger>
          </TooltipTrigger>
          <TooltipContent side="bottom" sideOffset={6}>标注台帮助</TooltipContent>
        </Tooltip>
      </TooltipProvider>

      <PopoverContent
        data-annotation-help
        align="end"
        sideOffset={8}
        collisionPadding={8}
        aria-label="标注台帮助"
        className="z-[80] flex max-h-[var(--radix-popover-content-available-height)] w-[min(29rem,calc(100vw-1rem))] flex-col overflow-hidden rounded-2xl border border-[#dfe4ed] bg-white p-0 shadow-[0_18px_46px_rgba(24,35,61,0.18)] ring-0"
      >
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-[#e6e9ef] px-4 py-3.5">
          <div>
            <h2 className="text-sm font-semibold text-[#202938]">标注台帮助</h2>
            <p className="mt-1 text-xs leading-5 text-[#737d90]">首帧目标标注、草稿保存与 Segment 流转说明</p>
          </div>
          <PopoverClose asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label="关闭标注台帮助"
              className="shrink-0 text-[#7b8494] hover:bg-[#f1f3f7] hover:text-[#202938] active:bg-[#e8ebf1]"
            >
              <X className="size-4" aria-hidden="true" />
            </Button>
          </PopoverClose>
        </div>

        <Tabs
          defaultValue="layout"
          orientation="vertical"
          className="grid h-[min(31rem,calc(100dvh-10rem))] min-h-0 grid-cols-[8rem_minmax(0,1fr)] grid-rows-1 gap-0 overflow-hidden max-[520px]:grid-cols-1 max-[520px]:grid-rows-[auto_minmax(0,1fr)]"
        >
          <TabsList
            aria-label="帮助分类"
            className="flex h-full min-h-0 w-full flex-col items-stretch justify-start gap-1 overflow-hidden rounded-none border-r border-[#e6e9ef] bg-[#f6f7f9] p-2 max-[520px]:h-auto max-[520px]:flex-row max-[520px]:overflow-x-auto max-[520px]:border-b max-[520px]:border-r-0"
          >
            {HELP_SECTIONS.map(({ value, label, icon: Icon }) => (
              <TabsTrigger
                key={value}
                value={value}
                className="h-9 min-w-0 w-full flex-none justify-start gap-2 overflow-hidden rounded-lg px-2.5 text-xs whitespace-nowrap text-[#667085] transition-[color,background-color,box-shadow] duration-150 hover:bg-white/70 hover:text-[#293449] data-[state=active]:bg-white data-[state=active]:text-[#3156c8] data-[state=active]:shadow-[0_1px_3px_rgba(35,47,74,0.1)] max-[520px]:w-auto motion-reduce:transition-none"
              >
                <Icon className="size-3.5" aria-hidden="true" />
                {label}
              </TabsTrigger>
            ))}
          </TabsList>

          <div
            role="region"
            aria-label="帮助内容"
            tabIndex={0}
            className="console-soft-scrollbar min-h-0 overscroll-contain overflow-x-hidden overflow-y-auto px-4 py-3.5 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[#3156c8]/30"
          >
            {HELP_SECTIONS.map(({ value, title, items }) => (
              <TabsContent key={value} value={value} className="m-0 outline-none">
                <h3 className="text-sm font-semibold text-[#263044]">{title}</h3>
                <dl className="mt-3 space-y-3">
                  {items.map(([term, detail]) => (
                    <div key={term} className="border-l-2 border-[#dbe4f7] pl-3">
                      <dt className="text-xs font-semibold leading-5 text-[#344054]">{term}</dt>
                      <dd className="mt-0.5 text-xs leading-5 text-[#6f7a8e]">{detail}</dd>
                    </div>
                  ))}
                </dl>
              </TabsContent>
            ))}
          </div>
        </Tabs>
      </PopoverContent>
    </Popover>
  );
}
