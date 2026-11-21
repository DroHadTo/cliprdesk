"""Usage: python verify_sha256.py FILE EXPECTED_SHA256  -> exit 0 if it matches, 1 if not."""
import hashlib, sys
h = hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()
ok = h.lower() == sys.argv[2].lower()
print(("OK " if ok else "MISMATCH ") + h); sys.exit(0 if ok else 1)
