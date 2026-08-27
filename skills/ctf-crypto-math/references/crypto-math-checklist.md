# Crypto Math Checklist

- Record modulus sizes, curves, exponents, leaks, nonces, and relations.
- Check for reused values, small parameters, bad randomness, and partial leaks.
- Keep derivations executable in `solve.py`.
- Include enough intermediate checks to distinguish the real solution from accidental text.
- Verify final plaintext or flag against the challenge policy.
