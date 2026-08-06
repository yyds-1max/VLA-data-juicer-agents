import { createStore } from "zustand/vanilla";

import {
  getAnnotationCapabilities,
  getAnnotationJob,
  getAnnotationSegment,
  getTrajectoryReview,
  listAnnotationJobs,
  listTrajectoryReviews,
} from "./api";
import type { AnnotationDomainEvent } from "./events";
import type {
  AnnotationCapability,
  AnnotationJobDetail,
  AnnotationJobSummary,
  AnnotationSegmentDetail,
  TrajectoryReview,
} from "./types";

type AnnotationProjectionState = {
  jobs: AnnotationJobSummary[];
  jobsLoaded: boolean;
  capability: AnnotationCapability | null;
  capabilityLoaded: boolean;
  jobDetails: Record<string, AnnotationJobDetail>;
  segmentDetails: Record<string, AnnotationSegmentDetail>;
  reviews: TrajectoryReview[];
  reviewsLoaded: boolean;
  reviewDetails: Record<string, TrajectoryReview>;
};

const initialState: AnnotationProjectionState = {
  jobs: [],
  jobsLoaded: false,
  capability: null,
  capabilityLoaded: false,
  jobDetails: {},
  segmentDetails: {},
  reviews: [],
  reviewsLoaded: false,
  reviewDetails: {},
};

export const annotationProjectionStore = createStore<AnnotationProjectionState>(
  () => ({ ...initialState }),
);

type RequestGate<T> = {
  pending: Promise<T> | null;
  dirty: boolean;
};

const jobsGate: RequestGate<AnnotationJobSummary[]> = { pending: null, dirty: false };
const capabilityGate: RequestGate<AnnotationCapability> = { pending: null, dirty: false };
const reviewsGate: RequestGate<TrajectoryReview[]> = { pending: null, dirty: false };
const jobGates = new Map<string, RequestGate<AnnotationJobDetail>>();
const segmentGates = new Map<string, RequestGate<AnnotationSegmentDetail>>();
const reviewGates = new Map<string, RequestGate<TrajectoryReview>>();
const activeJobRefs = new Map<string, number>();
const activeSegmentRefs = new Map<string, number>();
const activeReviewRefs = new Map<string, number>();

function gateFor<T>(
  gates: Map<string, RequestGate<T>>,
  key: string,
): RequestGate<T> {
  const current = gates.get(key);
  if (current) return current;
  const created: RequestGate<T> = { pending: null, dirty: false };
  gates.set(key, created);
  return created;
}

function runRequest<T>(
  gate: RequestGate<T>,
  force: boolean,
  request: () => Promise<T>,
): Promise<T> {
  // 同一资源的并发请求共用一个 Promise。请求进行中再次 force 刷新时只标记 dirty，
  // 当前请求结束后立即补拉一次，既避免请求风暴，也保证旧响应不会成为最终状态。
  if (force) gate.dirty = true;
  if (gate.pending) return gate.pending;

  const pending = (async () => {
    while (true) {
      gate.dirty = false;
      try {
        const result = await request();
        if (!gate.dirty) return result;
      } catch (error) {
        if (!gate.dirty) throw error;
      }
    }
  })().finally(() => {
    if (gate.pending === pending) gate.pending = null;
  });
  gate.pending = pending;
  return pending;
}

function retainRef(refs: Map<string, number>, ref: string): () => void {
  // 只对当前页面正在观察的实体保持引用；SSE 到达时据此决定是否加载完整详情。
  refs.set(ref, (refs.get(ref) ?? 0) + 1);
  return () => {
    const count = refs.get(ref) ?? 0;
    if (count <= 1) refs.delete(ref);
    else refs.set(ref, count - 1);
  };
}

export function retainAnnotationJobProjection(jobRef: string): () => void {
  return retainRef(activeJobRefs, jobRef);
}

export function retainAnnotationSegmentProjection(
  jobRef: string,
  segmentRef: string,
): () => void {
  return retainRef(activeSegmentRefs, segmentKey(jobRef, segmentRef));
}

export function retainTrajectoryReviewProjection(reviewRef: string): () => void {
  return retainRef(activeReviewRefs, reviewRef);
}

function segmentKey(jobRef: string, segmentRef: string): string {
  return `${jobRef}:${segmentRef}`;
}

function preferRevision<T extends { state_revision: number }>(
  current: T | undefined,
  next: T,
): T {
  // 所有投影合并都以服务端单调递增的 state_revision 为准，拒绝乱序旧响应覆盖新状态。
  return current && current.state_revision > next.state_revision ? current : next;
}

function sortByUpdatedAt<T extends { updated_at: string }>(items: T[]): T[] {
  return [...items].sort((left, right) => right.updated_at.localeCompare(left.updated_at));
}

function mergeJobs(
  current: AnnotationJobSummary[],
  incoming: AnnotationJobSummary[],
): AnnotationJobSummary[] {
  const byRef = new Map(current.map((job) => [job.job_ref, job]));
  for (const job of incoming) {
    byRef.set(job.job_ref, preferRevision(byRef.get(job.job_ref), job));
  }
  return sortByUpdatedAt([...byRef.values()]);
}

function mergeReviews(
  current: TrajectoryReview[],
  incoming: TrajectoryReview[],
): TrajectoryReview[] {
  const byRef = new Map(current.map((review) => [review.review_ref, review]));
  for (const review of incoming) {
    byRef.set(review.review_ref, preferRevision(byRef.get(review.review_ref), review));
  }
  return sortByUpdatedAt([...byRef.values()]);
}

export function cacheAnnotationJob(job: AnnotationJobDetail): AnnotationJobDetail {
  annotationProjectionStore.setState((state) => {
    const current = state.jobDetails[job.job_ref];
    const selected = preferRevision(current, job);
    return {
      jobs: mergeJobs(state.jobs, [selected]),
      jobDetails: {
        ...state.jobDetails,
        [job.job_ref]: selected,
      },
    };
  });
  return annotationProjectionStore.getState().jobDetails[job.job_ref];
}

function cacheAnnotationJobSummary(job: AnnotationJobSummary): AnnotationJobSummary {
  annotationProjectionStore.setState((state) => ({
    jobs: mergeJobs(state.jobs, [job]),
  }));
  return annotationProjectionStore.getState().jobs.find(
    (current) => current.job_ref === job.job_ref,
  ) ?? job;
}

export function cacheAnnotationSegment(
  jobRef: string,
  segment: AnnotationSegmentDetail,
): AnnotationSegmentDetail {
  const key = segmentKey(jobRef, segment.segment_ref);
  annotationProjectionStore.setState((state) => ({
    segmentDetails: {
      ...state.segmentDetails,
      [key]: preferRevision(state.segmentDetails[key], segment),
    },
  }));
  return annotationProjectionStore.getState().segmentDetails[key];
}

export function cacheTrajectoryReview(review: TrajectoryReview): TrajectoryReview {
  annotationProjectionStore.setState((state) => {
    const current = state.reviewDetails[review.review_ref];
    const selected = preferRevision(current, review);
    return {
      reviews: mergeReviews(state.reviews, [selected]),
      reviewDetails: {
        ...state.reviewDetails,
        [review.review_ref]: selected,
      },
    };
  });
  return annotationProjectionStore.getState().reviewDetails[review.review_ref];
}

function cacheTrajectoryReviewSummary(review: TrajectoryReview): TrajectoryReview {
  annotationProjectionStore.setState((state) => ({
    reviews: mergeReviews(state.reviews, [review]),
  }));
  return annotationProjectionStore.getState().reviews.find(
    (current) => current.review_ref === review.review_ref,
  ) ?? review;
}

function requestAnnotationJob(
  jobRef: string,
  force: boolean,
): Promise<AnnotationJobDetail> {
  return runRequest(
    gateFor(jobGates, jobRef),
    force,
    () => getAnnotationJob(jobRef),
  );
}

function requestTrajectoryReview(
  reviewRef: string,
  force: boolean,
): Promise<TrajectoryReview> {
  return runRequest(
    gateFor(reviewGates, reviewRef),
    force,
    () => getTrajectoryReview(reviewRef),
  );
}

export async function loadAnnotationJobs(options: {
  force?: boolean;
} = {}): Promise<AnnotationJobSummary[]> {
  const state = annotationProjectionStore.getState();
  if (!options.force && state.jobsLoaded) return state.jobs;
  return runRequest(
    jobsGate,
    Boolean(options.force),
    () => listAnnotationJobs().then((jobs) => {
      annotationProjectionStore.setState((current) => ({
        jobs: mergeJobs(current.jobs, jobs),
        jobsLoaded: true,
      }));
      return annotationProjectionStore.getState().jobs;
    }),
  );
}

export async function loadAnnotationCapability(options: {
  force?: boolean;
} = {}): Promise<AnnotationCapability> {
  const state = annotationProjectionStore.getState();
  if (!options.force && state.capabilityLoaded && state.capability) {
    return state.capability;
  }
  return runRequest(
    capabilityGate,
    Boolean(options.force),
    () => getAnnotationCapabilities().then((capability) => {
      annotationProjectionStore.setState({
        capability,
        capabilityLoaded: true,
      });
      return capability;
    })
    .catch((error: unknown) => {
      annotationProjectionStore.setState({
        capability: {
          available: false,
          runtime_id: "navigation_odom_v1",
          reason: {
            code: "capability_unavailable",
            message: "暂时无法确认处理环境状态。",
          },
        },
        capabilityLoaded: true,
      });
      throw error;
    }),
  );
}

export async function loadAnnotationJob(
  jobRef: string,
  options: { force?: boolean } = {},
): Promise<AnnotationJobDetail> {
  const cached = annotationProjectionStore.getState().jobDetails[jobRef];
  if (!options.force && cached) return cached;
  return requestAnnotationJob(jobRef, Boolean(options.force)).then(cacheAnnotationJob);
}

export async function loadAnnotationSegment(
  jobRef: string,
  segmentRef: string,
  options: { force?: boolean } = {},
): Promise<AnnotationSegmentDetail> {
  const key = segmentKey(jobRef, segmentRef);
  const cached = annotationProjectionStore.getState().segmentDetails[key];
  if (!options.force && cached) return cached;
  return runRequest(
    gateFor(segmentGates, key),
    Boolean(options.force),
    () => getAnnotationSegment(jobRef, segmentRef),
  ).then((segment) => cacheAnnotationSegment(jobRef, segment));
}

export async function loadTrajectoryReviews(options: {
  force?: boolean;
} = {}): Promise<TrajectoryReview[]> {
  const state = annotationProjectionStore.getState();
  if (!options.force && state.reviewsLoaded) return state.reviews;
  return runRequest(
    reviewsGate,
    Boolean(options.force),
    () => listTrajectoryReviews().then((reviews) => {
      annotationProjectionStore.setState((current) => ({
        reviews: mergeReviews(current.reviews, reviews),
        reviewsLoaded: true,
      }));
      return annotationProjectionStore.getState().reviews;
    }),
  );
}

export async function loadTrajectoryReview(
  reviewRef: string,
  options: { force?: boolean } = {},
): Promise<TrajectoryReview> {
  const cached = annotationProjectionStore.getState().reviewDetails[reviewRef];
  if (!options.force && cached) return cached;
  return requestTrajectoryReview(reviewRef, Boolean(options.force))
    .then(cacheTrajectoryReview);
}

export async function refreshAnnotationProjectionForEvent(
  event: AnnotationDomainEvent,
): Promise<void> {
  // 实时事件只携带定位信息：已打开的实体刷新完整详情，列表中的实体只刷新摘要，
  // 避免每条 SSE 都把所有任务和 Segment 详情重新拉取一遍。
  const state = annotationProjectionStore.getState();
  if (event.aggregate_kind === "review" && event.review_ref) {
    if (
      state.reviewDetails[event.review_ref]
      || activeReviewRefs.has(event.review_ref)
    ) {
      await loadTrajectoryReview(event.review_ref, { force: true });
    } else {
      await requestTrajectoryReview(event.review_ref, true)
        .then(cacheTrajectoryReviewSummary);
    }
    return;
  }
  if (!event.job_ref) return;
  const requests: Promise<unknown>[] = [];
  if (
    state.jobDetails[event.job_ref]
    || activeJobRefs.has(event.job_ref)
  ) {
    requests.push(loadAnnotationJob(event.job_ref, { force: true }));
  } else if (state.jobsLoaded) {
    requests.push(
      requestAnnotationJob(event.job_ref, true).then(cacheAnnotationJobSummary),
    );
  }
  if (event.aggregate_kind === "segment" && event.segment_ref) {
    const key = segmentKey(event.job_ref, event.segment_ref);
    if (state.segmentDetails[key] || activeSegmentRefs.has(key)) {
      requests.push(loadAnnotationSegment(
        event.job_ref,
        event.segment_ref,
        { force: true },
      ));
    }
  }
  await Promise.all(requests);
}

export async function reconcileLoadedAnnotationProjections(): Promise<void> {
  // SSE 重连后只对已加载列表和仍被页面引用的详情做一次对账。
  // 单个接口失败不应阻断其他投影刷新，因此使用 allSettled。
  const state = annotationProjectionStore.getState();
  const requests: Promise<unknown>[] = [];
  if (state.jobsLoaded) requests.push(loadAnnotationJobs({ force: true }));
  if (state.reviewsLoaded) requests.push(loadTrajectoryReviews({ force: true }));
  if (state.capabilityLoaded) {
    requests.push(loadAnnotationCapability({ force: true }));
  }
  for (const jobRef of activeJobRefs.keys()) {
    requests.push(loadAnnotationJob(jobRef, { force: true }));
  }
  for (const key of activeSegmentRefs.keys()) {
    const separator = key.indexOf(":");
    if (separator <= 0 || separator === key.length - 1) continue;
    requests.push(loadAnnotationSegment(
      key.slice(0, separator),
      key.slice(separator + 1),
      { force: true },
    ));
  }
  for (const reviewRef of activeReviewRefs.keys()) {
    requests.push(loadTrajectoryReview(reviewRef, { force: true }));
  }
  await Promise.allSettled(requests);
}

export function resetAnnotationProjectionStore() {
  jobsGate.pending = null;
  jobsGate.dirty = false;
  capabilityGate.pending = null;
  capabilityGate.dirty = false;
  reviewsGate.pending = null;
  reviewsGate.dirty = false;
  jobGates.clear();
  segmentGates.clear();
  reviewGates.clear();
  activeJobRefs.clear();
  activeSegmentRefs.clear();
  activeReviewRefs.clear();
  annotationProjectionStore.setState({ ...initialState }, true);
}
