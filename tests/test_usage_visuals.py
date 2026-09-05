import json
import os
import subprocess
import unittest


class TestUsageVisualMetrics(unittest.TestCase):
    def test_percentage_clamps_and_formats_reset_times(self):
        helper = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dashboard", "usage_metrics.js"))
        script = r'''const metrics = require(process.argv[1]);
const result = {
  half: metrics.usagePercent({used: 5, limit: 10}),
  fromRemaining: metrics.usagePercent({remaining: 2, limit: 10}),
  clamped: metrics.usagePercent({used: 20, limit: 10}),
  unknown: metrics.usagePercent({used: 5}),
  grades: [50, 51, 75, 76, 90, 91].map(metrics.usageGrade),
  resetEpoch: metrics.formatResetTime(1788663840, "UTC"),
  resetIso: metrics.formatResetTime("2026-09-06T03:04:00Z", "UTC")
};
process.stdout.write(JSON.stringify(result));'''
        result = subprocess.run(["node", "-e", script, helper], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {
            "half": 50, "fromRemaining": 80, "clamped": 100, "unknown": None,
            "grades": ["usage-grade-green", "usage-grade-yellow", "usage-grade-yellow",
                        "usage-grade-orange", "usage-grade-orange", "usage-grade-red"],
            "resetEpoch": "Resets Sep 6, 2026 3:04 AM",
            "resetIso": "Resets Sep 6, 2026 3:04 AM",
        })


if __name__ == "__main__":
    unittest.main()
