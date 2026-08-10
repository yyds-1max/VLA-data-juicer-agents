import * as React from "react";
import { ArrowLeft, CheckCircle2, RefreshCw } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";

import { Button } from "../../components/ui/button";
import { Card, CardContent, CardHeader } from "../../components/ui/card";
import { getHistoricalVerifiedAsset } from "./api";
import type { HistoricalVerifiedAsset } from "./types";

function displayTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

export function HistoricalVerifiedAssetPage() {
  const navigate = useNavigate();
  const { assetRef = "" } = useParams();
  const [asset, setAsset] = React.useState<HistoricalVerifiedAsset | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState("");

  const refresh = React.useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setAsset(await getHistoricalVerifiedAsset(assetRef));
    } catch {
      setError("读取历史已验证资产失败");
    } finally {
      setLoading(false);
    }
  }, [assetRef]);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <section className="mx-auto max-w-4xl space-y-4 px-4 py-6" aria-busy={loading || undefined}>
      <div className="flex items-center justify-between gap-3">
        <Button type="button" variant="outline" onClick={() => navigate(-1)}>
          <ArrowLeft aria-hidden="true" />
          返回
        </Button>
        <Button type="button" variant="outline" disabled={loading} onClick={() => void refresh()}>
          <RefreshCw aria-hidden="true" className={loading ? "animate-spin motion-reduce:animate-none" : ""} />
          {loading ? "刷新中" : "刷新"}
        </Button>
      </div>

      {error ? (
        <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          {error}
        </div>
      ) : null}

      {asset ? (
        <Card className="rounded-2xl border-slate-200 shadow-sm">
          <CardHeader>
            <div className="flex items-center gap-3">
              <span className="flex size-10 items-center justify-center rounded-full bg-emerald-50 text-emerald-600">
                <CheckCircle2 aria-hidden="true" className="size-5" />
              </span>
              <div>
                <h1 className="text-base font-medium leading-snug">历史已验证版本</h1>
                <p className="mt-1 text-sm text-slate-500">由受控导入清单登记，不改写原业务产物。</p>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-4 text-sm sm:grid-cols-2">
              <div><dt className="text-slate-500">数据日期</dt><dd className="mt-1 font-medium text-slate-900">{asset.dataset_date}</dd></div>
              <div><dt className="text-slate-500">外层 clip</dt><dd className="mt-1 font-medium text-slate-900">{asset.source_clip}</dd></div>
              <div><dt className="text-slate-500">内部单元</dt><dd className="mt-1 font-medium text-slate-900">Segment {String(asset.segment_ordinal).padStart(2, "0")} / {asset.segment_total}</dd></div>
              <div><dt className="text-slate-500">导入时间</dt><dd className="mt-1 font-medium text-slate-900">{displayTime(asset.imported_at)}</dd></div>
              <div className="sm:col-span-2"><dt className="text-slate-500">内容 SHA-256</dt><dd className="mt-1 break-all font-mono text-xs text-slate-700">{asset.content_sha256}</dd></div>
            </dl>
          </CardContent>
        </Card>
      ) : null}
    </section>
  );
}
