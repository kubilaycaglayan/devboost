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

  return {usagePercent, usageGrade};
}));
