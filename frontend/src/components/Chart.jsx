import { useEffect, useRef } from "react";
import * as echarts from "echarts";

export default function Chart({ option, className = "", ariaLabel }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return undefined;
    chartRef.current = echarts.init(containerRef.current, null, { renderer: "canvas" });
    const observer = new ResizeObserver(() => chartRef.current?.resize());
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      chartRef.current?.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, { notMerge: true, lazyUpdate: true });
  }, [option]);

  return (
    <div
      ref={containerRef}
      className={`chart ${className}`}
      aria-label={ariaLabel}
    />
  );
}

