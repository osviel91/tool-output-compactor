import sys

print("== build started ==")
for i in range(3000):
    print(f"compiling target{i}: linking module_{i % 20}.o ok")
print("== build failed ==")
sys.stderr.write("ERROR: src/main.c:412: fatal error: undefined reference to `missing_symbol'\n")
sys.exit(1)
