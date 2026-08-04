import { expect, test, type Page } from "@playwright/test";

const now = "2026-07-20T08:00:00.000Z";

test.beforeEach(async ({ page }) => {
  await installWebSocketStub(page);
  await page.route("**/api/navigation/datasets/summary", async (route) => {
    await route.fulfill({
      json: {
        totals: {
          date_count: 0,
          clip_count: 0,
          total_duration_ns: 0,
          raw_message_count: 0,
          extracted_clip_count: 0,
          synced_clip_count: 0,
        },
        sync_distribution: { image: 0, pointcloud: 0, odom: 0, grid_map: 0 },
        dates: [],
      },
    });
  });
});

test("preserves the DataPilot floating entry over the migrated console", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByText("智瀚星途", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "仪表盘" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open DataPilot" })).toBeVisible();

  await page.getByRole("button", { name: "Open DataPilot" }).click();

  await expect(page.getByText("开始一个任务")).toBeVisible();
  await expect(page.getByPlaceholder("我们要做什么？")).toBeVisible();
  await expect(page.getByText("继续任务")).toHaveCount(0);

  const dialog = page.getByRole("dialog", { name: "DataPilot" });
  await page.evaluate(() => window.scrollTo(0, 0));
  await dialog.hover();
  await page.mouse.wheel(0, 500);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);

  await page.mouse.move(420, 650);
  await page.mouse.wheel(0, 500);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);

  await page.getByRole("button", { name: "Close DataPilot" }).click();
  await expect(page.getByRole("dialog", { name: "DataPilot" })).toHaveCount(0);

  await page.getByRole("button", { name: "测试/仿真" }).click();
  await expect(page.getByRole("heading", { name: "测试/仿真" })).toBeVisible();
  await expect(page.getByText("仿真场景配置")).toBeVisible();
});

test("keeps one empty processing placeholder while optimistic and server turns reconcile", async ({ page }) => {
  const session = sessionRecord("session-race", "竞态回归");
  const emptySnapshot = sessionSnapshot(session, { messages: [], events: [], turns: [], tasks: [] });

  await page.route("**/api/sessions", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ json: { session } });
      return;
    }
    await route.fulfill({ json: { sessions: [] } });
  });
  await page.route("**/api/sessions/session-race", async (route) => {
    await route.fulfill({ json: { session: emptySnapshot } });
  });
  await page.route("**/api/sessions/session-race/turns", async (route) => {
    await emitPublicEvent(page, {
      type: "turn_start",
      contract_version: 1,
      timestamp: now,
      turn_id: "server-turn-race",
      payload: { status: "running", started_at: now },
    });
    // Keep the POST pending past the 400ms placeholder threshold. This recreates
    // the WebSocket-before-submit-response race that previously rendered twice.
    await new Promise((resolve) => setTimeout(resolve, 900));
    await route.fulfill({ json: { turn_id: "server-turn-race" } });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Open DataPilot" }).click();
  await page.getByPlaceholder("我们要做什么？").fill("处理 20270605 的导航数据");
  await page.getByRole("button", { name: "Send message" }).click();

  await page.waitForTimeout(500);
  await expect(page.getByRole("button", { name: "正在处理" })).toHaveCount(1);
  await expect(page.getByText("处理 20270605 的导航数据", { exact: true })).toHaveCount(1);
  await page.waitForTimeout(500);
  await expect(page.getByRole("button", { name: "正在处理" })).toHaveCount(1);
});

test("keeps an active semantic action expanded beside a safe failure final", async ({ page }) => {
  const session = sessionRecord("session-active-action", "安全失败回归");
  const task = navigationTask("DP-ACTIVE1", "active", "提取并同步");
  const snapshot = sessionSnapshot(session, {
    messages: [
      message("message-user", "user", "执行导航数据处理", "turn-active-action"),
      message(
        "message-safe-final",
        "assistant",
        "本轮处理已结束，但未能生成可安全展示的回复。请重试。",
        "turn-active-action",
      ),
    ],
    turns: [{
      id: "turn-active-action",
      web_session_id: session.id,
      origin: "user",
      status: "failed",
      started_at: now,
      finished_at: "2026-07-20T08:00:10.000Z",
      final_message_id: "message-safe-final",
    }],
    tasks: [task],
    events: [
      timelineEvent("event-progress", 1, "progress_start", "turn-active-action", {
        phase: "extract_sync",
        summary: "已完成数据准备，正在提取并同步导航数据。",
      }),
      timelineEvent("event-action", 2, "action_start", "turn-active-action", {
        action_ref: "extract-sync",
        action_code: "navigation.extract_sync",
        phase_instance_id: "extract-sync-1",
        display_name: "提取并同步导航数据",
      }),
      timelineEvent("event-final", 3, "final", "turn-active-action", {
        reply_id: "reply-safe-final",
        message_id: "message-safe-final",
        text: "本轮处理已结束，但未能生成可安全展示的回复。请重试。",
      }),
    ],
  });

  await openActiveSnapshot(page, session, snapshot);

  const disclosure = page.getByRole("button", { name: "正在处理" });
  await expect(disclosure).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("正在提取并同步导航数据", { exact: true })).toBeVisible();
  await expect(page.getByText("本轮处理已结束，但未能生成可安全展示的回复。请重试。", {
    exact: true,
  })).toHaveCount(1);
  const capsule = page.getByLabel(/导航任务 DP-ACTIVE1，提取并同步，处理中/);
  const tooltip = page.locator('[role="tooltip"]');
  await expect(capsule).toBeVisible();
  await expect(capsule.getByText("处理中", { exact: true })).toBeVisible();
  await expect(tooltip).toBeHidden();
  await capsule.focus();
  await expect(tooltip).toBeVisible();
  await expect(tooltip.getByText("导航任务 DP-ACTIVE1", { exact: true })).toBeVisible();
});

test("renders public ReAct progress text with its semantic action", async ({ page }) => {
  const session = sessionRecord("session-react", "公开 ReAct 回归");
  const snapshot = sessionSnapshot(session, {
    messages: [message("message-react", "user", "检查并处理导航数据", "turn-react")],
    turns: [{
      id: "turn-react",
      web_session_id: session.id,
      origin: "user",
      status: "running",
      started_at: now,
      finished_at: null,
      final_message_id: null,
    }],
    tasks: [navigationTask("DP-REACT1", "active", "检查数据")],
    events: [
      timelineEvent("event-progress-1", 1, "progress_delta", "turn-react", {
        progress_id: "progress-react",
        delta: "已核对现有产物，",
      }),
      timelineEvent("event-progress-2", 2, "progress_delta", "turn-react", {
        progress_id: "progress-react",
        delta: "接下来提取并同步导航数据。",
      }),
      timelineEvent("event-action", 3, "action_start", "turn-react", {
        action_ref: "extract-sync",
        action_code: "navigation.extract_sync",
        phase_instance_id: "extract-sync-1",
        display_name: "提取并同步导航数据",
      }),
    ],
  });

  await openActiveSnapshot(page, session, snapshot);

  await expect(page.getByRole("button", { name: "正在处理" })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await expect(page.getByText("已核对现有产物，接下来提取并同步导航数据。", {
    exact: true,
  })).toBeVisible();
  await expect(page.getByText("正在提取并同步导航数据", { exact: true })).toBeVisible();
  await expect(page.getByText(/agent_start|tool_start|call_id|navigation\.extract_sync/i)).toHaveCount(0);
});

test("a same-tab reload restores the active session and its pending confirmation", async ({ page }) => {
  const session = sessionRecord("session-reload", "刷新恢复");
  const snapshot = {
    ...sessionSnapshot(session, {
      messages: [{
        id: "message-reload",
        session_id: session.id,
        turn_id: null,
        role: "assistant",
        content: "请确认当天处理标定。",
        created_at: now,
      }],
      events: [],
      turns: [],
      tasks: [navigationTask("DP-RELOAD1", "waiting_user", "确认标定")],
    }),
    snapshot_seq: 0,
    pending_interaction: {
      interaction_id: "interaction-reload",
      task_ref: "DP-RELOAD1",
      kind: "calibration_confirmation",
      blocking: true,
      risk: "medium",
      title: "确认当天处理标定",
      summary: "确认后继续执行当前计划。",
      options: [
        { option_id: "confirm", label: "确认并继续", tone: "primary" },
        { option_id: "stop", label: "暂不处理" },
      ],
      interaction_revision: 1,
      expected_task_revision: 2,
      expires_at: null,
    },
  };

  await openActiveSnapshot(page, session, snapshot);
  await expect(page.getByRole("heading", { name: "确认当天处理标定" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => (
    window.sessionStorage.getItem("datapilot.session-view.v1")
  ))).toContain(session.id);

  await page.reload();
  await page.getByRole("button", { name: "Open DataPilot" }).click();

  await expect(page.getByText("请确认当天处理标定。", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "确认当天处理标定" })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认并继续" })).toBeVisible();
});

async function installWebSocketStub(page: Page) {
  await page.addInitScript(() => {
    type EventEmitterWindow = Window & {
      __datapilotSockets?: MockWebSocket[];
      __datapilotPendingEvents?: unknown[];
      __emitDataPilotEvent?: (event: unknown) => void;
    };

    class MockWebSocket extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSING = 2;
      static readonly CLOSED = 3;
      readonly url: string;
      readyState = MockWebSocket.OPEN;

      constructor(url: string | URL) {
        super();
        this.url = String(url);
        const target = window as EventEmitterWindow;
        target.__datapilotSockets ??= [];
        target.__datapilotSockets.push(this);
        const queued = target.__datapilotPendingEvents?.splice(0) ?? [];
        queueMicrotask(() => {
          this.dispatchEvent(new Event("open"));
          for (const event of queued) this.emit(event);
        });
      }

      close() {
        if (this.readyState === MockWebSocket.CLOSED) return;
        this.readyState = MockWebSocket.CLOSED;
        this.dispatchEvent(new CloseEvent("close"));
      }

      send() {}

      emit(event: unknown) {
        this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(event) }));
      }
    }

    const target = window as EventEmitterWindow;
    target.__datapilotSockets = [];
    target.__datapilotPendingEvents = [];
    target.__emitDataPilotEvent = (event: unknown) => {
      const sockets = target.__datapilotSockets?.filter(
        (socket) => socket.readyState === MockWebSocket.OPEN,
      ) ?? [];
      if (sockets.length === 0) {
        target.__datapilotPendingEvents?.push(event);
        return;
      }
      for (const socket of sockets) socket.emit(event);
    };
    Object.defineProperty(window, "WebSocket", { configurable: true, value: MockWebSocket });
  });
}

async function emitPublicEvent(page: Page, event: Record<string, unknown>) {
  await page.evaluate((nextEvent) => {
    const emit = (window as Window & { __emitDataPilotEvent?: (value: unknown) => void })
      .__emitDataPilotEvent;
    emit?.(nextEvent);
  }, event);
}

async function openActiveSnapshot(
  page: Page,
  session: ReturnType<typeof sessionRecord>,
  snapshot: ReturnType<typeof sessionSnapshot>,
) {
  await page.route("**/api/sessions", async (route) => {
    await route.fulfill({ json: { sessions: [session] } });
  });
  await page.route(`**/api/sessions/${session.id}`, async (route) => {
    await route.fulfill({ json: { session: snapshot } });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Open DataPilot" }).click();
  await page.getByRole("button", { name: "History" }).click();
  await page.getByRole("button", { name: new RegExp(session.title) }).click();
}

function sessionRecord(id: string, title: string) {
  return {
    id,
    title,
    status: "active" as const,
    contract_version: 1 as const,
    created_at: now,
    updated_at: now,
  };
}

function sessionSnapshot(
  session: ReturnType<typeof sessionRecord>,
  data: {
    messages: Array<Record<string, unknown>>;
    events: Array<Record<string, unknown>>;
    turns: Array<Record<string, unknown>>;
    tasks: Array<Record<string, unknown>>;
  },
) {
  return {
    ...session,
    ...data,
    pending_interaction: null,
  };
}

function message(id: string, role: "user" | "assistant", content: string, turnId: string) {
  return {
    id,
    session_id: turnId.startsWith("turn-react") ? "session-react" : "session-active-action",
    turn_id: turnId,
    role,
    content,
    created_at: now,
  };
}

function timelineEvent(
  id: string,
  seq: number,
  type: string,
  turnId: string,
  payload: Record<string, unknown>,
) {
  return {
    id,
    session_id: turnId === "turn-react" ? "session-react" : "session-active-action",
    seq,
    type,
    contract_version: 1,
    timestamp: now,
    turn_id: turnId,
    payload,
    created_at: now,
  };
}

function navigationTask(taskRef: string, status: string, phase: string) {
  return {
    task_ref: taskRef,
    domain: "navigation",
    dataset_date: "20270605",
    selection: { kind: "all_clips" },
    scene_mode: null,
    status,
    phase,
    wait_cause: null,
    latest_public_update: null,
    available_actions: ["stop", "cancel"],
    state_revision: 1,
    started_at: now,
    updated_at: now,
  };
}
