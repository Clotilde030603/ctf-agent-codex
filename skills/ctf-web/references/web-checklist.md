# Web Checklist

- Identify base URL, allowed hosts, redirects, cookies, and CSRF behavior.
- Record discovered endpoints and parameters.
- Separate authentication bugs from application logic bugs.
- Replay requests with sanitized headers.
- Avoid destructive actions outside the challenge.
- Prove the final exploit with `solve.py` and a fresh session when possible.
