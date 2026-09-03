import random

random.seed(7)
lines = []
for i in range(4000):
    kind = random.random()
    if kind < 0.003:
        lines.append(f"2026-09-03 10:{i // 60:02d}:{i % 60:02d} ERROR worker-{i % 5}: request {i} failed: connection reset by peer")
    elif kind < 0.01:
        lines.append(f"2026-09-03 10:{i // 60:02d}:{i % 60:02d} WARN worker-{i % 5}: retrying request {i} (attempt 2)")
    elif kind < 0.05:
        lines.append(f"2026-09-03 10:{i // 60:02d}:{i % 60:02d} INFO worker-{i % 5}: processed request {i} in {random.randint(1, 900)}ms")
    else:
        lines.append(f"2026-09-03 10:{i // 60:02d}:{i % 60:02d} DEBUG worker-{i % 5}: request {i} headers={{accept: */*}}")
print("\n".join(lines))
