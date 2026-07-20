export function formatNumber(value, digits = 2) {
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value || 0));
}

export function formatMoney(value) {
  const amount = Number(value || 0);
  if (Math.abs(amount) >= 1e12) return `${formatNumber(amount / 1e12, 2)}万亿`;
  if (Math.abs(amount) >= 1e8) return `${formatNumber(amount / 1e8, 1)}亿`;
  if (Math.abs(amount) >= 1e4) return `${formatNumber(amount / 1e4, 1)}万`;
  return formatNumber(amount, 0);
}

export function trendClass(value) {
  if (Number(value) > 0) return "trend-up";
  if (Number(value) < 0) return "trend-down";
  return "trend-flat";
}

export function signed(value, digits = 2) {
  const number = Number(value || 0);
  return `${number > 0 ? "+" : ""}${formatNumber(number, digits)}`;
}

