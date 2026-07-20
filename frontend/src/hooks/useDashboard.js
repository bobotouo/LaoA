import { useCallback, useEffect, useRef, useState } from "react";

const DEFAULT_POLL_MS = 8000;

export function useDashboard() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const requestRef = useRef(0);
  const pollMsRef = useRef(DEFAULT_POLL_MS);
  const dataRef = useRef(null);

  const load = useCallback(async (force = false) => {
    const requestId = ++requestRef.current;
    if (force) {
      setRefreshing(true);
    } else if (!dataRef.current) {
      setLoading(true);
    }
    try {
      const response = await fetch(
        `/api/market/dashboard?refresh=${force ? "true" : "false"}`,
        { cache: "no-store" },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `请求失败（${response.status}）`);
      }
      const payload = await response.json();
      if (requestId === requestRef.current) {
        dataRef.current = payload;
        setData(payload);
        setError("");
        const nextPoll = Number(payload?.meta?.pollIntervalMs);
        if (Number.isFinite(nextPoll) && nextPoll >= 5000) {
          pollMsRef.current = nextPoll;
        }
      }
    } catch (requestError) {
      if (requestId === requestRef.current) {
        setError(requestError.message || "行情数据暂时不可用");
      }
    } finally {
      if (requestId === requestRef.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timerId = 0;

    const schedule = () => {
      window.clearTimeout(timerId);
      timerId = window.setTimeout(async () => {
        if (cancelled) return;
        await load(false);
        if (!cancelled) schedule();
      }, pollMsRef.current);
    };

    load(false).finally(() => {
      if (!cancelled) schedule();
    });

    return () => {
      cancelled = true;
      window.clearTimeout(timerId);
    };
  }, [load]);

  return { data, loading, refreshing, error, refresh: () => load(true) };
}
