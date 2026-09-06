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

  function formatResetTime(value, _timeZone, now = Date.now()) {
    if (value == null || value === "") return null;
    const raw = String(value).trim();
    const numeric = /^\d+(?:\.\d+)?$/.test(raw) ? Number(raw) : NaN;
    const date = new Date(Number.isFinite(numeric) ? (numeric < 1e12 ? numeric * 1000 : numeric) : raw);
    if (Number.isNaN(date.getTime())) return null;
    const minutes = Math.ceil((date.getTime() - now) / 60_000);
    if (minutes <= 0) return "Resets now";
    if (minutes < 60) return `Resets in ${minutes}m`;
    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;
    if (hours < 24) return `Resets in ${hours}h${remainingMinutes ? `${remainingMinutes}m` : ""}`;
    const days = Math.floor(hours / 24);
    const remainingHours = hours % 24;
    if (days < 7) return `Resets in ${days}d${remainingHours ? `${remainingHours}h` : ""}`;
    const weeks = Math.floor(days / 7);
    const remainingDays = days % 7;
    return `Resets in ${weeks}w${remainingDays ? `${remainingDays}d` : ""}`;
  }

  return {usagePercent, usageGrade, formatResetTime};
}));
