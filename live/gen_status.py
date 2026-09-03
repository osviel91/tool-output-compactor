import random

random.seed(11)
files = ["src/main/java/com/acme/{mod}/service/{svc}.java" for mod in ("auth", "core", "api", "billing", "search")
         for svc in ("Registry", "Factory", "Client", "Handler", "Validator", "Provider")]
print(" M  src/main/java/com/acme/core/service/Client.java")
print(" M  src/main/java/com/acme/api/service/Handler.java")
print(" ?? untracked.py")
for i in range(300):
    status = random.choice([" M", "A ", " D", "M ", " M", "?? "])
    print(f"{status} {random.choice(files).replace('{mod}', random.choice(['auth', 'core', 'api', 'billing', 'search'])).replace('{svc}', random.choice(['Registry', 'Factory', 'Client', 'Handler', 'Validator', 'Provider']))}")
