import random

random.seed(17)
words = ("segment processing pipeline recovered the full working set and promoted the "
         "intermediate results into the shared pool for the next phase which follows "
         "the staged handoff contract established at startup").split()
lines = []
for i in range(220):
    if i % 4 == 0:
        n = random.randint(30, 70)
        lines.append(" ".join(random.choice(words) for _ in range(n)))
    else:
        lines.append(" ".join(random.choice(words) for _ in range(20, 45)))
print("\n".join(lines))
