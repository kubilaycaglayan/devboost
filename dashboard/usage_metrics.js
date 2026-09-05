(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.DevBoostUsageMetrics = factory();
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function usagePercent(quota) {
    const limit = Number(quota && quota.limit);
    if (!Number.isFinite(limit) || limit <= 0) return null;
    const used = Number(quota.used);
    if (Number.isFinite(used)) return Math.max(0, Math.min(100, used / limit * 100));
    const remaining = Number(quota.remaining);
    if (Number.isFinite(remaining)) return Math.max(0, Math.min(100, (limit - remaining) / limit * 100));
    return null;
  }

  function usageGrade(percent) {
    if (percent <= 50) return "usage-grade-green";
    if (percent <= 75) return "usage-grade-yellow";
    if (percent <= 90) return "usage-grade-orange";
    return "usage-grade-red";
  }

  function formatResetTime(value, timeZone) {
    if (value == null || value === "") return null;
    const raw = String(value).trim();
    const numeric = /^\d+(?:\.\d+)?$/.test(raw) ? Number(raw) : NaN;
    const date = new Date(Number.isFinite(numeric) ? (numeric < 1e12 ? numeric * 1000 : numeric) : raw);
    if (Number.isNaN(date.getTime())) return null;
    const options = {month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit"};
    if (timeZone) options.timeZone = timeZone;
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", options).formatToParts(date).map(part => [part.type, part.value]));
    return `Resets ${parts.month} ${parts.day}, ${parts.year} ${parts.hour}:${parts.minute} ${parts.dayPeriod}`;
  }

  function countReadableResets(quotas) {
    return (quotas || []).filter(quota => formatResetTime(quota && quota.reset_at)).length;
  }

  return {usagePercent, usageGrade, formatResetTime, countReadableResets};
}));
