import json

sev = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
records = [
    {
        "key": f"AZ{i:08d}",
        "severity": sev[i % len(sev)],
        "component": f"src/main/java/com/acme/Module{i % 7}.java",
        "line": i % 300,
        "status": "OPEN",
    }
    for i in range(180)
]
print(json.dumps(records, indent=2))
